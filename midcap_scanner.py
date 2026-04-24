#!/usr/bin/env python3
"""
Mid-Cap Early Pump Scanner
===========================
Scans top 1500 coins and alerts when ALL conditions are met:
  - Market cap $10M - $400M
  - Volume 24h >= $5M (absolute floor)
  - Volume 24h / Market Cap >= 0.5 (dynamic ratio)
  - 1h price change >= +10%
  - 24h price change <= +50%
  - Not alerted for same coin in last 6 hours

Volume condition is DYNAMIC via ratio:
  - $20M mcap coin needs $10M volume (50% of mcap)
  - $100M mcap coin needs $50M volume (50% of mcap)
  - $400M mcap coin needs $200M volume (50% of mcap)
  Much more meaningful than a fixed floor alone.
"""

import json
import os
import time
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ==================== CONFIG ====================
COINGECKO_API          = "https://api.coingecko.com/api/v3"
TOP_N_COINS            = 1500

MCAP_MIN               = 10_000_000    # $10M
MCAP_MAX               = 400_000_000   # $400M

VOLUME_FLOOR           = 5_000_000     # $5M absolute minimum
VOLUME_MCAP_RATIO      = 0.01           # volume must be >= 50% of market cap

PRICE_CHANGE_1H_MIN    = 8.0          # 1h price gain %
PRICE_CHANGE_24H_MAX   = 50.0          # skip exhausted pumps

NOTIFY_COOLDOWN_HOURS  = 6
REQUEST_PAUSE_SEC      = 2.5

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

STATE_PATH = Path(__file__).parent / "midcap_state.json"

# DexScreener chain mapping
DEXSCREENER_CHAINS = {
    "ethereum":             "ethereum",
    "binance-smart-chain":  "bsc",
    "solana":               "solana",
    "arbitrum-one":         "arbitrum",
    "base":                 "base",
    "polygon-pos":          "polygon",
    "avalanche":            "avalanche",
    "optimistic-ethereum":  "optimism",
    "sui":                  "sui",
    "fantom":               "fantom",
    "cronos":               "cronos",
    "tron":                 "tron",
    "near":                 "near",
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
    except Exception as e:
        print(f"Warning: couldn't parse state ({e}); starting fresh.")
        return {"notified": {}}

def save_state(state: dict) -> None:
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, separators=(",", ":"))

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))

def notified_recently(state: dict, coin_id: str) -> bool:
    ts = state["notified"].get(coin_id)
    if not ts:
        return False
    return (datetime.now(timezone.utc) - parse_iso(ts)).total_seconds() \
           < NOTIFY_COOLDOWN_HOURS * 3600

def mark_notified(state: dict, coin_id: str) -> None:
    state["notified"][coin_id] = now_iso()

def cleanup_notified(state: dict) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=NOTIFY_COOLDOWN_HOURS * 2)
    state["notified"] = {
        k: v for k, v in state["notified"].items()
        if parse_iso(v) >= cutoff
    }


# ==================== API ====================
def fetch_top_coins() -> list:
    all_coins = []
    per_page = 250
    pages = (TOP_N_COINS + per_page - 1) // per_page
    for page in range(1, pages + 1):
        try:
            r = requests.get(
                f"{COINGECKO_API}/coins/markets",
                params={
                    "vs_currency": "usd",
                    "order": "market_cap_desc",
                    "per_page": per_page,
                    "page": page,
                    "price_change_percentage": "1h,24h",
                },
                headers={"User-Agent": "gh-actions-midcap-scanner/2.0"},
                timeout=30,
            )
            if r.status_code == 429:
                print("Rate limited, sleeping 60s...")
                time.sleep(60)
                continue
            r.raise_for_status()
            all_coins.extend(r.json())
            time.sleep(REQUEST_PAUSE_SEC)
        except Exception as e:
            print(f"Error fetching page {page}: {e}")
    return all_coins


def fetch_coin_detail(coin_id: str) -> dict:
    """Fetch extended info: 4h/7d price change, contract addresses."""
    try:
        r = requests.get(
            f"{COINGECKO_API}/coins/{coin_id}",
            params={
                "localization": "false",
                "tickers": "false",
                "market_data": "true",
                "community_data": "false",
                "developer_data": "false",
                "sparkline": "false",
            },
            headers={"User-Agent": "gh-actions-midcap-scanner/2.0"},
            timeout=30,
        )
        if r.status_code == 429:
            print(f"Rate limit on detail for {coin_id}, sleeping 30s")
            time.sleep(30)
            return {}
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"Detail fetch error for {coin_id}: {e}")
        return {}


def send_telegram(text: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("!! Telegram env vars missing; skipping send")
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
        print(f"!! Telegram send failed: {e}")
        return False


# ==================== FORMATTING ====================
def fmt_usd(x) -> str:
    if x is None: return "-"
    if x >= 1e9:  return f"${x/1e9:.2f}B"
    if x >= 1e6:  return f"${x/1e6:.2f}M"
    if x >= 1e3:  return f"${x/1e3:.1f}K"
    return f"${x:,.2f}"

def fmt_price(x) -> str:
    if x is None: return "-"
    if x >= 1:    return f"${x:,.4f}"
    return f"${x:.8f}".rstrip("0").rstrip(".")

def fmt_pct(x) -> str:
    if x is None: return "n/a"
    sign = "+" if x >= 0 else ""
    return f"*{sign}{x:.1f}%*"

def fmt_contract(detail: dict) -> str:
    platforms = detail.get("platforms") or {}
    priority = [
        ("ethereum",            "ETH"),
        ("binance-smart-chain", "BSC"),
        ("solana",              "SOL"),
        ("arbitrum-one",        "ARB"),
        ("base",                "BASE"),
        ("polygon-pos",         "MATIC"),
        ("avalanche",           "AVAX"),
        ("optimistic-ethereum", "OP"),
        ("sui",                 "SUI"),
        ("tron",                "TRX"),
        ("fantom",              "FTM"),
    ]
    for chain_key, label in priority:
        addr = platforms.get(chain_key, "")
        if addr:
            short = addr[:6] + "..." + addr[-4:]
            return f"`{short}` ({label})"
    for chain_key, addr in platforms.items():
        if addr:
            short = addr[:6] + "..." + addr[-4:]
            label = chain_key.replace("-", " ").title()
            return f"`{short}` ({label})"
    return "N/A"

def get_dexscreener_url(detail: dict, symbol: str) -> str:
    platforms = detail.get("platforms") or {}
    for chain_key, dex_chain in DEXSCREENER_CHAINS.items():
        addr = platforms.get(chain_key, "")
        if addr:
            return f"https://dexscreener.com/{dex_chain}/{addr}"
    return f"https://dexscreener.com/search?q={symbol}"


def build_alert(h: dict, detail: dict) -> str:
    md = detail.get("market_data") or {}

    p1h  = (md.get("price_change_percentage_1h_in_currency")  or {}).get("usd")
    p4h  = (md.get("price_change_percentage_4h_in_currency")  or {}).get("usd")
    p24h = (md.get("price_change_percentage_24h_in_currency") or {}).get("usd")
    p7d  = (md.get("price_change_percentage_7d_in_currency")  or {}).get("usd")

    vol_24h  = (md.get("total_volume") or {}).get("usd") or h["volume"]
    contract = fmt_contract(detail)
    dex_url  = get_dexscreener_url(detail, h["symbol"])
    symbol_up = h["symbol"].upper()

    name_slug = h["name"].lower().replace(" ", "-")
    cmc_url   = f"https://coinmarketcap.com/currencies/{name_slug}/"
    mexc_url  = f"https://www.mexc.com/exchange/{symbol_up}_USDT"
    tv_url    = f"https://www.tradingview.com/chart/?symbol=MEXC%3A{symbol_up}USDT"

    # Volume/MCap ratio for display
    ratio = h["volume"] / h["market_cap"] if h["market_cap"] > 0 else 0

    parts = []
    if p1h  is not None: parts.append(f"1h: {fmt_pct(p1h)}")
    if p4h  is not None: parts.append(f"4h: {fmt_pct(p4h)}")
    if p24h is not None: parts.append(f"24h: {fmt_pct(p24h)}")
    if p7d  is not None: parts.append(f"7d: {fmt_pct(p7d)}")
    changes_line = "   ".join(parts) if parts else "n/a"

    return (
        f"⚡ *MID-CAP PUMP*\n"
        f"*{h['name']}* (`{symbol_up}`)\n"
        f"\n"
        f"💰 Price: {fmt_price(h['price'])}\n"
        f"📈 {changes_line}\n"
        f"\n"
        f"📊 Volume 24h: {fmt_usd(vol_24h)} _(x{ratio:.1f} mcap)_\n"
        f"🏦 Market Cap: {fmt_usd(h['market_cap'])}   Rank: #{h.get('rank','?')}\n"
        f"\n"
        f"📝 Contract: {contract}\n"
        f"\n"
        f"[🟢 MEXC]({mexc_url}) · "
        f"[CoinGecko](https://www.coingecko.com/en/coins/{h['id']}) · "
        f"[CMC]({cmc_url}) · "
        f"[DexScreener]({dex_url}) · "
        f"[TradingView]({tv_url})"
    )


# ==================== MAIN ====================
def run_scan():
    state = load_state()
    coins = fetch_top_coins()
    print(f"Fetched {len(coins)} coins")
    if not coins:
        return state, []

    candidates = []
    for coin in coins:
        cid           = coin.get("id")
        if not cid: continue
        mcap          = coin.get("market_cap") or 0
        volume        = coin.get("total_volume") or 0
        price_chg_1h  = coin.get("price_change_percentage_1h_in_currency") or 0
        price_chg_24h = coin.get("price_change_percentage_24h_in_currency") or 0

        # Market cap range
        if not (MCAP_MIN <= mcap <= MCAP_MAX):          continue
        # Absolute volume floor
        if volume < VOLUME_FLOOR:                        continue
        # Dynamic volume/mcap ratio
        if mcap > 0 and volume / mcap < VOLUME_MCAP_RATIO: continue
        # Price filters
        if price_chg_1h  < PRICE_CHANGE_1H_MIN:         continue
        if price_chg_24h > PRICE_CHANGE_24H_MAX:         continue
        # Cooldown
        if notified_recently(state, cid):                continue

        candidates.append({
            "id":            cid,
            "symbol":        coin.get("symbol", "").upper(),
            "name":          coin.get("name", ""),
            "price":         coin.get("current_price", 0),
            "price_chg_1h":  price_chg_1h,
            "price_chg_24h": price_chg_24h,
            "volume":        volume,
            "market_cap":    mcap,
            "rank":          coin.get("market_cap_rank"),
        })

    candidates.sort(key=lambda x: x["price_chg_1h"], reverse=True)
    return state, candidates


def main():
    state, hits = run_scan()
    print(f"Mid-cap signals this run: {len(hits)}")

    for h in hits[:10]:
        ratio = h["volume"] / h["market_cap"] if h["market_cap"] > 0 else 0
        print(f"  ⚡ {h['symbol']:>8}  1h +{h['price_chg_1h']:5.1f}%  "
              f"mcap {fmt_usd(h['market_cap'])}  "
              f"vol {fmt_usd(h['volume'])} (x{ratio:.1f})")

        detail = fetch_coin_detail(h["id"])
        time.sleep(2)

        send_telegram(build_alert(h, detail))
        mark_notified(state, h["id"])
        time.sleep(1)

    if not hits:
        send_telegram("_Mid-cap scan done — no matches._")

    cleanup_notified(state)
    save_state(state)
    print("Done.")


if __name__ == "__main__":
    main()