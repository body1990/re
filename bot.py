#!/usr/bin/env python3
"""
Crypto Explosion Scanner — GitHub Actions edition
===================================================
Scans top coins on CoinGecko every hour and alerts via Telegram when:
  - 24h price change >= PRICE_CHANGE_MIN  (default +10%)
  - Current 24h volume >= VOLUME_MULTIPLIER × volume from ~24h ago (default 2x)

Uses state.json (committed back to the repo by the workflow) as its
"database" for volume history and notification cooldown.

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
COINGECKO_API         = "https://api.coingecko.com/api/v3"
TOP_N_COINS           = 500
PRICE_CHANGE_MIN      = 10.0
VOLUME_MULTIPLIER     = 2.0
MIN_VOLUME_USD        = 1_000_000
MAX_MARKET_CAP        = None
NOTIFY_COOLDOWN_HOURS = 12
HISTORY_RETENTION_H   = 30
SNAPSHOT_TARGET_H     = 24
SNAPSHOT_TOLERANCE_H  = 3
REQUEST_PAUSE_SEC     = 2.5

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

STATE_PATH = Path(__file__).parent / "state.json"


# ==================== STATE ====================
def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"snapshots": {}, "notified": {}}
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception as e:
        print(f"Warning: couldn't parse state.json ({e}); starting fresh.")
        return {"snapshots": {}, "notified": {}}


def save_state(state: dict) -> None:
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, separators=(",", ":"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def push_snapshot(state: dict, coin_id: str, volume: float) -> None:
    snaps = state["snapshots"].setdefault(coin_id, [])
    snaps.append({"ts": now_iso(), "vol": float(volume)})
    cutoff = datetime.now(timezone.utc) - timedelta(hours=HISTORY_RETENTION_H)
    state["snapshots"][coin_id] = [s for s in snaps if parse_iso(s["ts"]) >= cutoff]


def volume_around(state: dict, coin_id: str,
                  hours_ago: float, tolerance_h: float):
    snaps = state["snapshots"].get(coin_id, [])
    if not snaps:
        return None, None
    now = datetime.now(timezone.utc)
    target = now - timedelta(hours=hours_ago)
    lo = now - timedelta(hours=hours_ago + tolerance_h)
    hi = now - timedelta(hours=max(0, hours_ago - tolerance_h))

    best, best_diff = None, None
    for s in snaps:
        t = parse_iso(s["ts"])
        if not (lo <= t <= hi):
            continue
        diff = abs((t - target).total_seconds())
        if best_diff is None or diff < best_diff:
            best, best_diff = s, diff
    if not best:
        return None, None
    age_h = (now - parse_iso(best["ts"])).total_seconds() / 3600
    return best["vol"], age_h


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
                    "price_change_percentage": "24h",
                },
                headers={"User-Agent": "gh-actions-crypto-scanner/1.0"},
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
        f"🚀 *{h['name']}*  (`{h['symbol']}`)\n"
        f"Price: {fmt_price(h['price'])}   *{h['price_chg']:+.1f}%* 24h\n"
        f"Volume: {fmt_usd(h['volume'])}   *{h['volume_ratio']:.1f}x* vs ~24h ago\n"
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
        current_vol = coin.get("total_volume") or 0
        price_chg   = coin.get("price_change_percentage_24h") or 0
        mcap        = coin.get("market_cap") or 0

        push_snapshot(state, cid, current_vol)

        if current_vol < MIN_VOLUME_USD:       continue
        if price_chg   < PRICE_CHANGE_MIN:     continue
        if MAX_MARKET_CAP and mcap > MAX_MARKET_CAP: continue

        old_vol, age = volume_around(state, cid,
                                     hours_ago=SNAPSHOT_TARGET_H,
                                     tolerance_h=SNAPSHOT_TOLERANCE_H)
        if not old_vol or old_vol <= 0:
            continue

        ratio = current_vol / old_vol
        if ratio < VOLUME_MULTIPLIER:
            continue

        if notified_recently(state, cid):
            continue

        hits.append({
            "id":           cid,
            "symbol":       coin.get("symbol", "").upper(),
            "name":         coin.get("name", ""),
            "price":        coin.get("current_price", 0),
            "price_chg":    price_chg,
            "volume":       current_vol,
            "old_volume":   old_vol,
            "volume_ratio": ratio,
            "market_cap":   mcap,
            "rank":         coin.get("market_cap_rank"),
            "age_h":        age,
        })
        mark_notified(state, cid)

    cleanup_notified(state)
    return state, hits


def main():
    test_mode = "--test" in sys.argv
    if test_mode:
        ok = send_telegram("✅ *Crypto scanner test* — Telegram wiring OK.")
        print("Telegram test:", "OK" if ok else "FAILED")
        return

    state, hits = run_scan()
    print(f"Signals this run: {len(hits)}")

    hits.sort(key=lambda x: x["volume_ratio"], reverse=True)
    for h in hits[:10]:
        print(f"  🚀 {h['symbol']:>8}  +{h['price_chg']:5.1f}%  vol x{h['volume_ratio']:.1f}")
        send_telegram(build_alert(h))
        time.sleep(1)

    save_state(state)
    print("Done.")


if __name__ == "__main__":
    main()
