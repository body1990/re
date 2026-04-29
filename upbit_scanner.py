#!/usr/bin/env python3
"""
Korean Exchange New Listing Scanner
=====================================
Monitors Upbit and Bithumb for NEW coin listings.
Sends Telegram alert the moment a new market appears.

Detection:
  - Upbit:   compares current KRW market list to stored baseline
  - Bithumb: compares current KRW market list to stored baseline

Timing: detects at TRADING START (2-4h after announcement).
Run every 5 minutes via cron-job.org.

State: listing_state.json
"""

import json
import os
import time
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ==================== CONFIG ====================
NOTIFY_COOLDOWN_HOURS = 24
REQUEST_TIMEOUT       = 15
KRW_USD               = 0.00072

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
STATE_PATH       = Path(__file__).parent / "listing_state.json"


# ==================== STATE ====================
def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"upbit_markets": [], "bithumb_markets": [], "alerted": {}}
    try:
        with open(STATE_PATH) as f:
            s = json.load(f)
        s.setdefault("upbit_markets",   [])
        s.setdefault("bithumb_markets", [])
        s.setdefault("alerted",         {})
        return s
    except Exception as e:
        print(f"State load error ({e}), starting fresh")
        return {"upbit_markets": [], "bithumb_markets": [], "alerted": {}}

def save_state(state: dict) -> None:
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, separators=(",", ":"))

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))

def alerted_recently(state: dict, key: str) -> bool:
    ts = state["alerted"].get(key)
    if not ts:
        return False
    return (datetime.now(timezone.utc) - parse_iso(ts)).total_seconds() \
           < NOTIFY_COOLDOWN_HOURS * 3600

def mark_alerted(state: dict, key: str) -> None:
    state["alerted"][key] = now_iso()

def cleanup_alerted(state: dict) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=NOTIFY_COOLDOWN_HOURS * 2)
    state["alerted"] = {
        k: v for k, v in state["alerted"].items()
        if parse_iso(v) >= cutoff
    }


# ==================== TELEGRAM ====================
def send_telegram(text: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("!! Telegram env missing")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id":    TELEGRAM_CHAT_ID,
                "text":       text,
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


# ==================== COINGECKO ENRICHMENT ====================
def get_coin_info(symbol: str) -> dict:
    """Search CoinGecko for basic coin info by symbol."""
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/search",
            params={"query": symbol},
            headers={"User-Agent": "listing-scanner/1.0"},
            timeout=15,
        )
        if r.status_code == 429:
            return {}
        r.raise_for_status()
        coins = r.json().get("coins", [])
        match = next(
            (c for c in coins if c.get("symbol", "").upper() == symbol.upper()),
            None
        )
        if not match:
            return {}
        time.sleep(1.5)
        r2 = requests.get(
            f"https://api.coingecko.com/api/v3/coins/{match['id']}",
            params={
                "localization":    "false",
                "tickers":         "false",
                "market_data":     "true",
                "community_data":  "false",
                "developer_data":  "false",
                "sparkline":       "false",
            },
            headers={"User-Agent": "listing-scanner/1.0"},
            timeout=15,
        )
        if r2.status_code != 200:
            return {"id": match["id"], "name": match.get("name", symbol)}
        d  = r2.json()
        md = d.get("market_data") or {}
        platforms = d.get("platforms") or {}
        contract, chain = "N/A", ""
        for ck, cl in [
            ("ethereum",            "ETH"),
            ("binance-smart-chain", "BSC"),
            ("solana",              "SOL"),
            ("arbitrum-one",        "ARB"),
            ("base",                "BASE"),
            ("polygon-pos",         "MATIC"),
        ]:
            addr = platforms.get(ck, "")
            if addr:
                contract = addr[:6] + "..." + addr[-4:]
                chain = cl
                break
        return {
            "id":       match["id"],
            "name":     d.get("name", symbol),
            "mcap":     (md.get("market_cap") or {}).get("usd"),
            "price":    (md.get("current_price") or {}).get("usd"),
            "chg_24h":  (md.get("price_change_percentage_24h_in_currency") or {}).get("usd"),
            "contract": contract,
            "chain":    chain,
        }
    except Exception as e:
        print(f"CoinGecko error for {symbol}: {e}")
        return {}


# ==================== FORMATTING ====================
def fmt_usd(x) -> str:
    if not x: return "?"
    if x >= 1e9:  return f"${x/1e9:.2f}B"
    if x >= 1e6:  return f"${x/1e6:.2f}M"
    if x >= 1e3:  return f"${x/1e3:.1f}K"
    return f"${x:,.6f}"

def fmt_price_krw(x) -> str:
    if not x: return "?"
    try:
        f = float(x)
        if f >= 1000: return f"₩{f:,.0f}"
        if f >= 1:    return f"₩{f:.2f}"
        return        f"₩{f:.6f}"
    except:
        return f"₩{x}"


# ==================== UPBIT ====================
def fetch_upbit_markets() -> list:
    """Return sorted list of all KRW market codes."""
    try:
        r = requests.get(
            "https://api.upbit.com/v1/market/all",
            params={"isDetails": "false"},
            headers={"Accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        return sorted([
            m["market"] for m in r.json()
            if m["market"].startswith("KRW-")
        ])
    except Exception as e:
        print(f"Upbit market fetch error: {e}")
        return []

def get_upbit_ticker(market: str) -> dict:
    try:
        r = requests.get(
            "https://api.upbit.com/v1/ticker",
            params={"markets": market},
            headers={"Accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        return data[0] if data else {}
    except Exception as e:
        print(f"Upbit ticker error {market}: {e}")
        return {}

def build_upbit_alert(symbol: str, ticker: dict, cg: dict) -> str:
    price_krw   = ticker.get("trade_price", 0)
    chg_24h     = (ticker.get("signed_change_rate") or 0) * 100
    vol_24h_krw = ticker.get("acc_trade_price_24h", 0)
    name        = cg.get("name", symbol)
    mcap        = cg.get("mcap")
    contract    = cg.get("contract", "N/A")
    chain       = cg.get("chain", "")
    cg_id       = cg.get("id", "")

    contract_str = f"`{contract}` ({chain})" if chain else "N/A"
    sign         = "+" if chg_24h >= 0 else ""

    upbit_url   = f"https://upbit.com/exchange?code=CRIX.UPBIT.KRW-{symbol}"
    mexc_url    = f"https://www.mexc.com/exchange/{symbol}_USDT"
    binance_url = f"https://www.binance.com/en/trade/{symbol}_USDT"
    tv_url      = f"https://www.tradingview.com/chart/?symbol=UPBIT%3A{symbol}KRW"
    cg_url      = f"https://www.coingecko.com/en/coins/{cg_id}" if cg_id else f"https://www.coingecko.com/en/search?query={symbol}"
    dex_url     = f"https://dexscreener.com/search?q={symbol}"

    return (
        f"🚨 *NEW UPBIT LISTING*\n"
        f"*{name}* (`{symbol}`)\n"
        f"\n"
        f"⏰ Trading just opened on Upbit KRW\n"
        f"\n"
        f"💰 Price: {fmt_price_krw(price_krw)}"
        f"  (~${price_krw * KRW_USD:.6f})\n"
        f"📈 24h: *{sign}{chg_24h:.1f}%*\n"
        f"📊 Vol 24h: ~{fmt_usd(vol_24h_krw * KRW_USD)}\n"
        f"🏦 MCap: {fmt_usd(mcap)}\n"
        f"\n"
        f"📝 Contract: {contract_str}\n"
        f"\n"
        f"⚡ *Second wave incoming — buy on other exchanges NOW*\n"
        f"\n"
        f"[🇰🇷 Upbit]({upbit_url}) · "
        f"[🟢 MEXC]({mexc_url}) · "
        f"[Binance]({binance_url}) · "
        f"[TradingView]({tv_url}) · "
        f"[DexScreener]({dex_url}) · "
        f"[CoinGecko]({cg_url})"
    )


# ==================== BITHUMB ====================
def fetch_bithumb_markets() -> list:
    """Return sorted list of all KRW symbols on Bithumb."""
    try:
        r = requests.get(
            "https://api.bithumb.com/public/ticker/ALL_KRW",
            headers={"Accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("status") != "0000":
            print(f"Bithumb API error: {data.get('message')}")
            return []
        return sorted([k for k in data["data"].keys() if k != "date"])
    except Exception as e:
        print(f"Bithumb market fetch error: {e}")
        return []

def get_bithumb_ticker(symbol: str) -> dict:
    try:
        r = requests.get(
            f"https://api.bithumb.com/public/ticker/{symbol}_KRW",
            headers={"Accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("status") == "0000":
            return data.get("data", {})
        return {}
    except Exception as e:
        print(f"Bithumb ticker error {symbol}: {e}")
        return {}

def build_bithumb_alert(symbol: str, ticker: dict, cg: dict) -> str:
    price_krw   = ticker.get("closing_price", 0)
    chg_24h     = float(ticker.get("fluctate_rate_24H", 0))
    vol_24h_krw = float(ticker.get("acc_trade_value_24H", 0))
    name        = cg.get("name", symbol)
    mcap        = cg.get("mcap")
    contract    = cg.get("contract", "N/A")
    chain       = cg.get("chain", "")
    cg_id       = cg.get("id", "")

    contract_str = f"`{contract}` ({chain})" if chain else "N/A"
    sign         = "+" if chg_24h >= 0 else ""

    bithumb_url = f"https://www.bithumb.com/react/trade/order/{symbol}-KRW"
    mexc_url    = f"https://www.mexc.com/exchange/{symbol}_USDT"
    binance_url = f"https://www.binance.com/en/trade/{symbol}_USDT"
    tv_url      = f"https://www.tradingview.com/chart/?symbol=BITHUMB%3A{symbol}KRW"
    cg_url      = f"https://www.coingecko.com/en/coins/{cg_id}" if cg_id else f"https://www.coingecko.com/en/search?query={symbol}"
    dex_url     = f"https://dexscreener.com/search?q={symbol}"

    return (
        f"🚨 *NEW BITHUMB LISTING*\n"
        f"*{name}* (`{symbol}`)\n"
        f"\n"
        f"⏰ Trading just opened on Bithumb KRW\n"
        f"\n"
        f"💰 Price: {fmt_price_krw(price_krw)}"
        f"  (~${float(price_krw or 0) * KRW_USD:.6f})\n"
        f"📈 24h: *{sign}{chg_24h:.1f}%*\n"
        f"📊 Vol 24h: ~{fmt_usd(vol_24h_krw * KRW_USD)}\n"
        f"🏦 MCap: {fmt_usd(mcap)}\n"
        f"\n"
        f"📝 Contract: {contract_str}\n"
        f"\n"
        f"⚡ *Second wave incoming — buy on other exchanges NOW*\n"
        f"\n"
        f"[🇰🇷 Bithumb]({bithumb_url}) · "
        f"[🟢 MEXC]({mexc_url}) · "
        f"[Binance]({binance_url}) · "
        f"[TradingView]({tv_url}) · "
        f"[DexScreener]({dex_url}) · "
        f"[CoinGecko]({cg_url})"
    )


# ==================== MAIN ====================
def main():
    state = load_state()

    # ---- UPBIT ----
    print("Fetching Upbit markets...")
    current_upbit = fetch_upbit_markets()

    if current_upbit:
        first_run = len(state["upbit_markets"]) == 0
        if first_run:
            print(f"Upbit first run: baseline = {len(current_upbit)} markets")
            state["upbit_markets"] = current_upbit
        else:
            known  = set(state["upbit_markets"])
            new    = [m for m in current_upbit if m not in known]
            if new:
                print(f"NEW Upbit markets: {new}")
                for market in new:
                    symbol = market.replace("KRW-", "")
                    key    = f"upbit_{symbol}"
                    if alerted_recently(state, key):
                        continue
                    ticker = get_upbit_ticker(market)
                    time.sleep(0.5)
                    cg = get_coin_info(symbol)
                    send_telegram(build_upbit_alert(symbol, ticker, cg))
                    mark_alerted(state, key)
                    print(f"  Alerted: {symbol}")
                    time.sleep(2)
            else:
                print(f"Upbit: no new listings ({len(current_upbit)} markets)")
            state["upbit_markets"] = current_upbit
    else:
        print("Upbit: fetch failed, keeping old state")

    # ---- BITHUMB ----
    print("Fetching Bithumb markets...")
    current_bithumb = fetch_bithumb_markets()

    if current_bithumb:
        first_run = len(state["bithumb_markets"]) == 0
        if first_run:
            print(f"Bithumb first run: baseline = {len(current_bithumb)} markets")
            state["bithumb_markets"] = current_bithumb
        else:
            known  = set(state["bithumb_markets"])
            new    = [s for s in current_bithumb if s not in known]
            if new:
                print(f"NEW Bithumb markets: {new}")
                for symbol in new:
                    key = f"bithumb_{symbol}"
                    if alerted_recently(state, key):
                        continue
                    ticker = get_bithumb_ticker(symbol)
                    time.sleep(0.5)
                    cg = get_coin_info(symbol)
                    send_telegram(build_bithumb_alert(symbol, ticker, cg))
                    mark_alerted(state, key)
                    print(f"  Alerted: {symbol}")
                    time.sleep(2)
            else:
                print(f"Bithumb: no new listings ({len(current_bithumb)} markets)")
            state["bithumb_markets"] = current_bithumb
    else:
        print("Bithumb: fetch failed, keeping old state")

    cleanup_alerted(state)
    save_state(state)
    print("Done.")


if __name__ == "__main__":
    main()
