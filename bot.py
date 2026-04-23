#!/usr/bin/env python3
"""
Telegram command bot for the crypto scanner.
Runs every minute via GitHub Actions, polls Telegram for new messages,
handles commands, and triggers the scanner workflow via the GitHub API.

Commands:
    /scan     - trigger an immediate scanner run
    /status   - show the last few runs
    /help     - list commands
"""

import json
import os
import sys
import requests
from pathlib import Path

# ====== CONFIG ======
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")   # only this user can command the bot
GH_PAT           = os.getenv("GH_PAT", "")
GH_OWNER         = os.getenv("GH_OWNER", "")           # your github username
GH_REPO          = os.getenv("GH_REPO", "")            # your repo name
GH_WORKFLOW_FILE = "scanner.yml"                       # the workflow to dispatch

OFFSET_PATH = Path(__file__).parent / "bot_offset.json"

TG_BASE = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
GH_API  = f"https://api.github.com/repos/{GH_OWNER}/{GH_REPO}"
GH_HEADERS = {
    "Authorization": f"Bearer {GH_PAT}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


# ====== OFFSET (so we don't reprocess old messages) ======
def load_offset() -> int:
    if not OFFSET_PATH.exists():
        return 0
    try:
        return json.load(open(OFFSET_PATH)).get("offset", 0)
    except Exception:
        return 0

def save_offset(offset: int) -> None:
    json.dump({"offset": offset}, open(OFFSET_PATH, "w"))


# ====== TELEGRAM ======
def tg_send(text: str, chat_id: str = None) -> None:
    try:
        requests.post(f"{TG_BASE}/sendMessage", json={
            "chat_id": chat_id or TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }, timeout=15)
    except Exception as e:
        print(f"tg_send failed: {e}")

def tg_get_updates(offset: int):
    try:
        r = requests.get(f"{TG_BASE}/getUpdates", params={
            "offset": offset,
            "timeout": 0,      # short poll, we run on a schedule
            "allowed_updates": json.dumps(["message"]),
        }, timeout=20)
        r.raise_for_status()
        return r.json().get("result", [])
    except Exception as e:
        print(f"tg_get_updates failed: {e}")
        return []


# ====== GITHUB ======
def gh_dispatch_scan() -> bool:
    """Trigger the scanner workflow."""
    url = f"{GH_API}/actions/workflows/{GH_WORKFLOW_FILE}/dispatches"
    r = requests.post(url, headers=GH_HEADERS,
                      json={"ref": "main"}, timeout=15)
    return r.status_code == 204

def gh_recent_runs(limit: int = 5):
    url = f"{GH_API}/actions/workflows/{GH_WORKFLOW_FILE}/runs"
    r = requests.get(url, headers=GH_HEADERS,
                     params={"per_page": limit}, timeout=15)
    if r.status_code != 200:
        return []
    return r.json().get("workflow_runs", [])


# ====== COMMANDS ======
def cmd_help(chat_id):
    tg_send(
        "*Crypto Scanner Bot*\n"
        "/scan — run the scanner now\n"
        "/status — show last 5 scheduled runs\n"
        "/help — this message",
        chat_id
    )

def cmd_scan(chat_id):
    if gh_dispatch_scan():
        tg_send("🚀 Scan triggered. Results in ~1 minute if any coins match.", chat_id)
    else:
        tg_send("❌ Failed to trigger scan. Check GH_PAT permissions.", chat_id)

def cmd_status(chat_id):
    runs = gh_recent_runs(5)
    if not runs:
        tg_send("No runs found (or GH API error).", chat_id)
        return
    lines = ["*Recent scanner runs:*"]
    for run in runs:
        emoji = {"success": "✅", "failure": "❌", "in_progress": "⏳",
                 "queued": "⏸️", "cancelled": "⚪"}.get(
                 run.get("conclusion") or run.get("status"), "❓")
        trig = run.get("event", "?")                 # schedule / workflow_dispatch
        when = run.get("created_at", "").replace("T", " ").replace("Z", "")
        lines.append(f"{emoji} `{when}` _{trig}_")
    tg_send("\n".join(lines), chat_id)

HANDLERS = {
    "/scan":   cmd_scan,
    "/status": cmd_status,
    "/help":   cmd_help,
    "/start":  cmd_help,
}


# ====== MAIN ======
def main():
    if not all([TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, GH_PAT, GH_OWNER, GH_REPO]):
        print("Missing one or more env vars. Check workflow file.")
        sys.exit(1)

    offset = load_offset()
    updates = tg_get_updates(offset)
    if not updates:
        print("No new messages.")
        return

    new_offset = offset
    for upd in updates:
        new_offset = max(new_offset, upd["update_id"] + 1)
        msg = upd.get("message") or {}
        text = (msg.get("text") or "").strip().lower()
        chat_id = str((msg.get("chat") or {}).get("id", ""))

        # Only respond to the authorised user
        if chat_id != str(TELEGRAM_CHAT_ID):
            print(f"Ignoring message from unauthorised chat {chat_id}")
            continue

        cmd = text.split()[0] if text else ""
        handler = HANDLERS.get(cmd)
        if handler:
            print(f"Handling command: {cmd}")
            handler(chat_id)
        elif text.startswith("/"):
            tg_send(f"Unknown command: `{cmd}`. Try /help", chat_id)
        # non-command messages are ignored silently

    save_offset(new_offset)
    print(f"Processed {len(updates)} updates, new offset {new_offset}")


if __name__ == "__main__":
    main()
