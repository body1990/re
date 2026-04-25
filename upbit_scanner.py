#!/usr/bin/env python3
"""
Upbit Korean Pump Scanner
==========================
Monitors ALL Upbit KRW markets every 15 minutes.
Alerts via Telegram when Korean retail pump ignition detected:
  - 15min price change >= +5%
  - 15min volume >= 2x previous 15min candle (surge)
  - Not alerted for same coin in last 2 hours

Why Upbit? Korean retail FOMO drives some of the fastest pumps in crypto.
When Upbit KRW volume doubles + price spikes 5%+ in 15min, the move
typically has another 15-40% remaining. This catches it at ignition.

No API key needed — Upbit public endpoints are free and open.
"""

import json
import os
import time
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ==================== CONFIG ====================
UPBIT_API              = "https://api.upbit.com/v1"

PRICE_CHANGE_15M_MIN   = 5.0      # % price change in last 15min
VOLUME_SURGE_MIN       = 2.0      # current 15m vol must be Nx previous candle
MIN_VOLUME_KRW         = 500_000_000  # minimum 15min volume in KRW (~$370K) — filters dust
NOTIFY_COOLDOWN_HOURS  = 2        # don't re-alert same coin within 2 hours
REQUEST_PAUSE_SEC      = 0.12     # ~8 req/sec — safely under Upbit limit of 10/sec

# KRW to USD approximate rate (used for display only)
# Updated manually or fetched dynamically below
KRW_USD_RATE           = 0.00072  # 1 KRW ≈ $0.00072 (update if stale)

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

STATE_PATH = Path(__file__).parent / "upbit_state.json"

# DexScreener chain mapping for links
DEXSCREENER_CHAINS = {
    "ethereum":             "ethereum",
    "binance-smart-chain":  "bsc",
    "solana":               "solana",
    "arbitrum-one":         "arbitrum",
    "base":                 "base",
    "polygon-pos":          "polygon",
}


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
    """Fetch live KRW/USD rate from a free public API."""
    try:
        r = requests.get(
            "https://api.exchangerate-api.com/v4/latest/USD",
            timeout=10
        )
        r.raise_for_status()
        krw_per_usd = r.json()["rates"]["KRW"]
        return 1 / krw_per_usd
    except Exception:
        return KRW_USD_RATE  # fallback to config value

def get_all_krw_markets() -> list:
    """Return list of all KRW-quoted market codes on Upbit."""
    try:
        r = requests.get(f"{UPBIT_API}/market/all",
                         params={"isDetails": "false"}, timeout=15)
        r.raise_for_status()
        markets = r.json()
        return [m["market"] for m in markets if m["market"].startswith("KRW-")]
    except Exception as e:
        print(f"Failed to fetch markets: {e}")
        return []

def get_candles_15m(market: str, count: int = 3) -> list:
    """
    Fetch last N completed 15-minute candles for a market.
    Returns list ordered oldest → newest.
    Each candle: {open, high, low, trade_price (close), candle_acc_trade_price (volume KRW)}
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
        candles = r.json()
        # API returns newest first — reverse to get oldest first
        return list(reversed(candles))
    except Exception as e:
        print(f"Candle error {market}: {e}")
        return []

def get_ticker(markets: list) -> dict:
    """
    Fetch current ticker for multiple markets at once.
    Returns dict: {market_code: ticker_data}
    Max 100 markets per call.
    """
    result = {}
    chunk_size = 100
    for i in range(0, len(markets), chunk_size):
        chunk = markets[i:i + chunk_size]
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
    if x >= 1:    return f"${x:,.4f}"
    return f"${x:.8f}".rstrip("0").rstrip(".")

def build_alert(symbol: str, ticker: dict, candles: list,
                price_chg_15m: float, vol_ratio: float,
                krw_usd: float) -> str:
    current_price_krw = ticker.get("trade_price", 0)
    high_52w_krw      = ticker.get("highest_52_week_price", 0)
    low_52w_krw       = ticker.get("lowest_52_week_price", 0)
    chg_24h           = ticker.get("signed_change_rate", 0) * 100
    vol_24h_krw       = ticker.get("acc_trade_price_24h", 0)
    vol_15m_krw       = candles[-1].get("candle_acc_trade_price", 0) if candles else 0

    # USD equivalents
    price_usd   = fmt_price_usd(current_price_krw, krw_usd)
    vol_15m_usd = fmt_usd(vol_15m_krw * krw_usd)
    vol_24h_usd = fmt_usd(vol_24h_krw * krw_usd)

    # Distance from 52w high (useful context)
    dist_from_high = ((current_price_krw - high_52w_krw) / high_52w_krw * 100) \
                     if high_52w_krw > 0 else 0

    # Links — Upbit direct, MEXC, Binance, TradingView, DexScreener
    upbit_url = f"https://upbit.com/exchange?code=CRIX.UPBIT.KRW-{symbol}"
    mexc_url  = f"https://www.mexc.com/exchange/{symbol}_USDT"
    binance_url = f"https://www.binance.com/en/trade/{symbol}_USDT"
    tv_url    = f"https://www.tradingview.com/chart/?symbol=UPBIT%3A{symbol}KRW"
    dex_url   = f"https://dexscreener.com/search?q={symbol}"

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
        f"🔥 Vol surge: *{vol_ratio:.1f}x* (15m candle)\n"
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
    print(f"KRW/USD rate: {krw_usd:.6f}")

    # Step 1: get all KRW markets
    markets = get_all_krw_markets()
    print(f"Found {len(markets)} KRW markets on Upbit")
    if not markets:
        send_telegram("⚠️ Upbit scanner: failed to fetch market list")
        return

    # Step 2: bulk ticker for quick 24h change pre-filter
    print("Fetching tickers...")
    tickers = get_ticker(markets)

    # Step 3: scan each market
    hits = []
    for market in markets:
        symbol = market.replace("KRW-", "")

        if notified_recently(state, symbol):
            continue

        ticker = tickers.get(market, {})

        # Quick pre-filter: 24h change must be positive (skip obvious dumps)
        chg_24h = (ticker.get("signed_change_rate") or 0) * 100
        if chg_24h < 0:
            continue

        # Fetch 15min candles
        candles = get_candles_15m(market, count=3)
        time.sleep(REQUEST_PAUSE_SEC)

        if len(candles) < 2:
            continue

        prev_candle = candles[-2]
        curr_candle = candles[-1]

        prev_close  = prev_candle.get("trade_price", 0)
        curr_close  = curr_candle.get("trade_price", 0)
        prev_vol    = prev_candle.get("candle_acc_trade_price", 0)  # KRW
        curr_vol    = curr_candle.get("candle_acc_trade_price", 0)  # KRW

        if prev_close <= 0 or prev_vol <= 0:
            continue

        # Calculate 15min metrics
        price_chg_15m = (curr_close - prev_close) / prev_close * 100
        vol_ratio     = curr_vol / prev_vol if prev_vol > 0 else 0

        # Apply filters
        if price_chg_15m < PRICE_CHANGE_15M_MIN:  continue
        if vol_ratio      < VOLUME_SURGE_MIN:      continue
        if curr_vol       < MIN_VOLUME_KRW:        continue

        hits.append({
            "symbol":        symbol,
            "market":        market,
            "ticker":        ticker,
            "candles":       candles,
            "price_chg_15m": price_chg_15m,
            "vol_ratio":     vol_ratio,
            "curr_vol_krw":  curr_vol,
        })
        print(f"  🇰🇷 {symbol:>10}  15m {price_chg_15m:+.1f}%  "
              f"vol x{vol_ratio:.1f}  {fmt_krw(curr_vol)}")

    # Sort by 15min price change descending
    hits.sort(key=lambda x: x["price_chg_15m"], reverse=True)
    print(f"Upbit signals: {len(hits)}")

    for h in hits[:10]:
        msg = build_alert(
            symbol       = h["symbol"],
            ticker       = h["ticker"],
            candles      = h["candles"],
            price_chg_15m= h["price_chg_15m"],
            vol_ratio    = h["vol_ratio"],
            krw_usd      = krw_usd,
        )
        send_telegram(msg)
        mark_notified(state, h["symbol"])
        time.sleep(1)

    if not hits:
        send_telegram("_Upbit scan done — no Korean pumps detected._")

    cleanup_notified(state)
    save_state(state)
    print("Done.")


if __name__ == "__main__":
    main()
