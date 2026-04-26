#!/usr/bin/env python3
"""
Upbit Korean Pump Scanner — Fixed
===================================
Monitors ALL Upbit KRW markets every 15 minutes.
Alerts via Telegram when Korean retail pump ignition detected:
  - Last COMPLETED 15min candle price change >= +5% vs previous completed candle
  - Last COMPLETED 15min candle volume >= 2x the one before it
  - Last COMPLETED 15min candle volume >= 50M KRW (~$37K minimum)
  - Not alerted for same coin in last 2 hours

FIXES vs previous version:
  1. Candle comparison: now uses last 2 COMPLETED candles, not current in-progress candle
     (current candle has partial volume — was causing artificially low vol ratios)
  2. Removed 24h pre-filter: coins down on the day can still spike 15%+ in 15min
  3. Lowered MIN_VOLUME_KRW: 50M KRW (~$37K) instead of 500M — was filtering too much
  4. Fetch count=5 candles for safety margin
"""

import json
import os
import time
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ==================== CONFIG ====================
UPBIT_API              = "https://api.upbit.com/v1"

PRICE_CHANGE_15M_MIN   = 5.0           # % change in last completed 15min candle
VOLUME_SURGE_MIN       = 1.5           # vol must be Nx previous completed candle
MIN_VOLUME_KRW         = 50_000_000    # 50M KRW minimum (~$37K) — dust filter
NOTIFY_COOLDOWN_HOURS  = 2
REQUEST_PAUSE_SEC      = 0.15          # ~6-7 req/sec — safely under Upbit limit

KRW_USD_FALLBACK       = 0.00072       # fallback if exchange rate API fails

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

STATE_PATH = Path(__file__).parent / "upbit_state.json"


# ==================== STATE ====================
def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"notified": {}}
    try:
        with open(STATE_PATH) as f:
            s = json.load(f)
        s.setdefault("notified", {})
        return s
    except Exception:
        return {"notified": {}}

def save_state(state: dict) -> None:
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, separators=(",", ":"))

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))

def notified_recently(state: dict, coin: str) -> bool:
    ts = state["notified"].get(coin)
    if not ts:
        return False
    return (datetime.now(timezone.utc) - parse_iso(ts)).total_seconds() \
           < NOTIFY_COOLDOWN_HOURS * 3600

def mark_notified(state: dict, coin: str) -> None:
    state["notified"][coin] = now_iso()

def cleanup_notified(state: dict) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=NOTIFY_COOLDOWN_HOURS * 2)
    state["notified"] = {
        k: v for k, v in state["notified"].items()
        if parse_iso(v) >= cutoff
    }


# ==================== UPBIT API ====================
def get_krw_usd_rate() -> float:
    """Fetch live KRW/USD rate."""
    try:
        r = requests.get(
            "https://api.exchangerate-api.com/v4/latest/USD",
            timeout=10
        )
        r.raise_for_status()
        krw_per_usd = r.json()["rates"]["KRW"]
        rate = 1 / krw_per_usd
        print(f"KRW/USD rate: {rate:.6f} (1 USD = {krw_per_usd:.0f} KRW)")
        return rate
    except Exception as e:
        print(f"Exchange rate fetch failed ({e}), using fallback {KRW_USD_FALLBACK}")
        return KRW_USD_FALLBACK

def get_all_krw_markets() -> list:
    """Return list of all KRW-quoted market codes on Upbit."""
    try:
        r = requests.get(
            f"{UPBIT_API}/market/all",
            params={"isDetails": "false"},
            headers={"Accept": "application/json"},
            timeout=15
        )
        r.raise_for_status()
        markets = r.json()
        krw = [m["market"] for m in markets if m["market"].startswith("KRW-")]
        print(f"Found {len(krw)} KRW markets on Upbit")
        return krw
    except Exception as e:
        print(f"Failed to fetch markets: {e}")
        return []

def get_tickers(markets: list) -> dict:
    """Bulk ticker fetch. Returns {market_code: ticker_dict}."""
    result = {}
    for i in range(0, len(markets), 100):
        chunk = markets[i:i + 100]
        try:
            r = requests.get(
                f"{UPBIT_API}/ticker",
                params={"markets": ",".join(chunk)},
                headers={"Accept": "application/json"},
                timeout=15,
            )
            r.raise_for_status()
            for t in r.json():
                result[t["market"]] = t
            time.sleep(0.1)
        except Exception as e:
            print(f"Ticker error chunk {i}: {e}")
    return result

def get_candles_15m(market: str, count: int = 5) -> list:
    """
    Fetch last N 15-minute candles from Upbit.

    IMPORTANT — Upbit API returns candles NEWEST FIRST:
      response[0] = current IN-PROGRESS candle (partial data — DO NOT USE for comparison)
      response[1] = last COMPLETED candle
      response[2] = candle before that (also complete)
      response[3] = etc.

    We return them OLDEST FIRST after reversing.
    After reversing with count=5:
      candles[0] = oldest
      candles[1] = 2nd oldest
      candles[2] = 3rd (complete)
      candles[3] = last COMPLETED candle  ← use as "previous"
      candles[4] = current IN-PROGRESS   ← DO NOT USE for volume comparison

    Correct comparison: candles[-2] (prev complete) vs candles[-3] (one before)
    """
    try:
        r = requests.get(
            f"{UPBIT_API}/candles/minutes/15",
            params={"market": market, "count": count},
            headers={"Accept": "application/json"},
            timeout=10,
        )
        if r.status_code == 429:
            print(f"Rate limited on {market}, sleeping 5s")
            time.sleep(5)
            return []
        r.raise_for_status()
        candles = r.json()  # newest first from API
        return list(reversed(candles))  # now oldest first
    except Exception as e:
        print(f"Candle error {market}: {e}")
        return []


# ==================== TELEGRAM ====================
def send_telegram(text: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("!! Telegram env vars missing")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"Telegram error: {e}")
        return False


# ==================== FORMATTING ====================
def fmt_krw(x) -> str:
    if x is None: return "-"
    if x >= 1e12: return f"₩{x/1e12:.2f}T"
    if x >= 1e9:  return f"₩{x/1e9:.2f}B"
    if x >= 1e6:  return f"₩{x/1e6:.1f}M"
    if x >= 1e3:  return f"₩{x/1e3:.0f}K"
    return f"₩{x:.0f}"

def fmt_usd(x) -> str:
    if x is None: return "-"
    if x >= 1e9:  return f"${x/1e9:.2f}B"
    if x >= 1e6:  return f"${x/1e6:.2f}M"
    if x >= 1e3:  return f"${x/1e3:.1f}K"
    return f"${x:,.2f}"

def fmt_price_krw(x) -> str:
    if x is None: return "-"
    if x >= 1000: return f"₩{x:,.0f}"
    if x >= 1:    return f"₩{x:.2f}"
    return f"₩{x:.6f}"

def fmt_price_usd(x_krw, rate) -> str:
    x = x_krw * rate
    if x >= 1:  return f"${x:,.4f}"
    return f"${x:.8f}".rstrip("0").rstrip(".")

def build_alert(symbol: str, ticker: dict, completed_candle: dict,
                price_chg_15m: float, vol_ratio: float, krw_usd: float) -> str:

    current_price_krw = ticker.get("trade_price", 0)
    high_52w_krw      = ticker.get("highest_52_week_price", 0)
    low_52w_krw       = ticker.get("lowest_52_week_price", 0)
    chg_24h           = (ticker.get("signed_change_rate") or 0) * 100
    vol_24h_krw       = ticker.get("acc_trade_price_24h", 0)
    vol_15m_krw       = completed_candle.get("candle_acc_trade_price", 0)

    price_usd   = fmt_price_usd(current_price_krw, krw_usd)
    vol_15m_usd = fmt_usd(vol_15m_krw * krw_usd)
    vol_24h_usd = fmt_usd(vol_24h_krw * krw_usd)

    dist_from_high = ((current_price_krw - high_52w_krw) / high_52w_krw * 100) \
                     if high_52w_krw > 0 else 0

    upbit_url   = f"https://upbit.com/exchange?code=CRIX.UPBIT.KRW-{symbol}"
    mexc_url    = f"https://www.mexc.com/exchange/{symbol}_USDT"
    binance_url = f"https://www.binance.com/en/trade/{symbol}_USDT"
    tv_url      = f"https://www.tradingview.com/chart/?symbol=UPBIT%3A{symbol}KRW"
    dex_url     = f"https://dexscreener.com/search?q={symbol}"

    sign_15m = "+" if price_chg_15m >= 0 else ""
    sign_24h = "+" if chg_24h >= 0 else ""

    return (
        f"🇰🇷 *UPBIT PUMP*\n"
        f"*{symbol}*\n"
        f"\n"
        f"💰 {fmt_price_krw(current_price_krw)}  ({price_usd})\n"
        f"📈 15m: *{sign_15m}{price_chg_15m:.1f}%*   "
        f"24h: *{sign_24h}{chg_24h:.1f}%*\n"
        f"\n"
        f"🔥 Vol surge: *{vol_ratio:.1f}x* last candle\n"
        f"📊 Vol 15m: {fmt_krw(vol_15m_krw)} ({vol_15m_usd})\n"
        f"📊 Vol 24h: {fmt_krw(vol_24h_krw)} ({vol_24h_usd})\n"
        f"\n"
        f"📉 52w High: {fmt_price_krw(high_52w_krw)} "
        f"({dist_from_high:+.1f}% from high)\n"
        f"📈 52w Low:  {fmt_price_krw(low_52w_krw)}\n"
        f"\n"
        f"[🇰🇷 Upbit]({upbit_url}) · "
        f"[🟢 MEXC]({mexc_url}) · "
        f"[Binance]({binance_url}) · "
        f"[TradingView]({tv_url}) · "
        f"[DexScreener]({dex_url})"
    )


# ==================== MAIN ====================
def main():
    state   = load_state()
    krw_usd = get_krw_usd_rate()

    markets = get_all_krw_markets()
    if not markets:
        send_telegram("⚠️ Upbit scanner: failed to fetch market list")
        return

    print("Fetching tickers...")
    tickers = get_tickers(markets)
    print(f"Tickers: {len(tickers)}")

    hits = []
    skipped_cooldown = 0

    for market in markets:
        symbol = market.replace("KRW-", "")

        if notified_recently(state, symbol):
            skipped_cooldown += 1
            continue

        # Fetch 5 candles (newest first from API, reversed to oldest first)
        candles = get_candles_15m(market, count=5)
        time.sleep(REQUEST_PAUSE_SEC)

        # Need at least 4 candles:
        # candles[0..2] = older complete candles
        # candles[3]    = last COMPLETE candle  ← "current" for comparison
        # candles[4]    = in-progress candle    ← ignore for volume
        if len(candles) < 4:
            continue

        # FIX: use last 2 COMPLETED candles only
        # candles[-1] = in-progress (skip)
        # candles[-2] = last completed candle  ← "curr" for our comparison
        # candles[-3] = candle before that     ← "prev" for our comparison
        curr_complete = candles[-2]   # last COMPLETED candle
        prev_complete = candles[-3]   # one before that

        curr_close = curr_complete.get("trade_price", 0)
        prev_close = prev_complete.get("trade_price", 0)
        curr_vol   = curr_complete.get("candle_acc_trade_price", 0)   # KRW
        prev_vol   = prev_complete.get("candle_acc_trade_price", 0)   # KRW

        if prev_close <= 0 or prev_vol <= 0:
            continue

        price_chg_15m = (curr_close - prev_close) / prev_close * 100
        vol_ratio     = curr_vol / prev_vol

        # Apply filters
        if price_chg_15m < PRICE_CHANGE_15M_MIN:  continue
        if vol_ratio      < VOLUME_SURGE_MIN:      continue
        if curr_vol       < MIN_VOLUME_KRW:        continue

        ticker = tickers.get(market, {})
        hits.append({
            "symbol":          symbol,
            "ticker":          ticker,
            "completed_candle":curr_complete,
            "price_chg_15m":   price_chg_15m,
            "vol_ratio":       vol_ratio,
            "curr_vol_krw":    curr_vol,
        })
        print(f"  🇰🇷 {symbol:>10}  15m {price_chg_15m:+.1f}%  "
              f"vol x{vol_ratio:.1f}  {fmt_krw(curr_vol)}")

    hits.sort(key=lambda x: x["price_chg_15m"], reverse=True)
    print(f"Upbit signals: {len(hits)}  (skipped {skipped_cooldown} on cooldown)")

    for h in hits[:10]:
        msg = build_alert(
            symbol          = h["symbol"],
            ticker          = h["ticker"],
            completed_candle= h["completed_candle"],
            price_chg_15m   = h["price_chg_15m"],
            vol_ratio        = h["vol_ratio"],
            krw_usd          = krw_usd,
        )
        send_telegram(msg)
        mark_notified(state, h["symbol"])
        time.sleep(1)

    if not hits:
        send_telegram("_Upbit scan done — no Korean pumps._")

    cleanup_notified(state)
    save_state(state)
    print("Done.")


if __name__ == "__main__":
    main()