#!/usr/bin/env python3
"""
Mid-Cap Early Pump Scanner
===========================
Filters for coins that look like early-stage pumps on small/mid-caps:
  - Market cap between MCAP_MIN and MCAP_MAX (default 20M - 100M USD)
  - 24h volume >= VOLUME_MIN (default 10M USD)
  - 1h price change >= PRICE_CHANGE_1H_MIN (default +10%)   ← "fresh" move
  - 24h price change <= PRICE_CHANGE_24H_MAX (default +50%) ← skip exhausted

Sends a Telegram "scan done" confirmation at the end of every run,
so you always know it ran (even when zero matches).
"""

import json
import os
import sys
import time
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ==================== CONFIG ====================
COINGECKO_API          = "https://api.coingecko.com/api/v3"
TOP_N_COINS            = 1000

MCAP_MIN               = 20_000_000
MCAP_MAX               = 100_000_000
VOLUME_MIN             = 10_000_000
PRICE_CHANGE_1H_MIN    = 10.0
PRICE_CHANGE_24H_MAX   = 50.0
NOTIFY_COOLDOWN_HOURS  = 6
REQUEST_PAUSE_SEC      = 2.5

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

STATE_PATH = Path(__file__).parent / "midcap_state.json"


# ==================== STATE ====================
def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"notified": {}}
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception as e:
        print(f"Warning: couldn't parse {STATE_PATH.name} ({e}); starting fresh.")
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
                headers={"User-Agent": "gh-actions-midcap-scanner/1.0"},
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
    s = f"${x:.8f}"
    return s.rstrip("0").rstrip(".")


def build_alert(h: dict) -> str:
    return (
        f"⚡ *MID-CAP PUMP* — *{h['name']}* (`{h['symbol']}`)\n"
        f"Price: {fmt_price(h['price'])}\n"
        f"1h: *{h['price_chg_1h']:+.1f}%*   24h: *{h['price_chg_24h']:+.1f}%*\n"
        f"Volume 24h: {fmt_usd(h['volume'])}\n"
        f"Market Cap: {fmt_usd(h['market_cap'])}   Rank: #{h.get('rank','?')}\n"
        f"[CoinGecko](https://www.coingecko.com/en/coins/{h['id']}) · "
        f"[DexScreener](https://dexscreener.com/search?q={h['symbol']}) · "
        f"[TradingView](https://www.tradingview.com/symbols/{h['symbol']}USDT/)"
    )


# ==================== MAIN ====================
def run_scan():
    state = load_state()
    coins = fetch_top_coins()
    print(f"Fetched {len(coins)} coins")
    if not coins:
        return state, []

    hits = []
    for coin in coins:
        cid = coin.get("id")
        if not cid:
            continue

        mcap          = coin.get("market_cap") or 0
        volume        = coin.get("total_volume") or 0
        price_chg_1h  = coin.get("price_change_percentage_1h_in_currency") or 0
        price_chg_24h = coin.get("price_change_percentage_24h_in_currency") or 0

        if not (MCAP_MIN <= mcap <= MCAP_MAX):      continue
        if volume       < VOLUME_MIN:               continue
        if price_chg_1h < PRICE_CHANGE_1H_MIN:      continue
        if price_chg_24h > PRICE_CHANGE_24H_MAX:    continue

        if notified_recently(state, cid):
            continue

        hits.append({
            "id":             cid,
            "symbol":         coin.get("symbol", "").upper(),
            "name":           coin.get("name", ""),
            "price":          coin.get("current_price", 0),
            "price_chg_1h":   price_chg_1h,
            "price_chg_24h":  price_chg_24h,
            "volume":         volume,
            "market_cap":     mcap,
            "rank":           coin.get("market_cap_rank"),
        })
        mark_notified(state, cid)

    cleanup_notified(state)
    return state, hits


def main():
    state, hits = run_scan()
    print(f"Mid-cap signals this run: {len(hits)}")

    hits.sort(key=lambda x: x["price_chg_1h"], reverse=True)
    for h in hits[:10]:
        print(f"  ⚡ {h['symbol']:>8}  1h +{h['price_chg_1h']:5.1f}%  "
              f"mcap {fmt_usd(h['market_cap'])}  vol {fmt_usd(h['volume'])}")
        send_telegram(build_alert(h))
        time.sleep(1)

    if not hits:
        send_telegram("_Mid-cap scan done — no matches._")

    save_state(state)
    print("Done.")


if __name__ == "__main__":
    main()
