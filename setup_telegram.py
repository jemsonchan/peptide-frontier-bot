#!/usr/bin/env python3
"""
Telegram setup — everything except the one step that needs your account.

    python setup_telegram.py

What you do (2 minutes, requires your Telegram):
    1. Open Telegram, message @BotFather, send /newbot, pick a name and a
       username ending in "bot". It replies with a token like
       8123456789:AAH...  <- that is TELEGRAM_BOT_TOKEN.
    2. Open a chat with your new bot and send it any message. A bot cannot
       start a conversation with you; you have to speak first. This is also
       what makes your chat id discoverable.

What this script does:
    - validates the token against the Telegram API
    - discovers TELEGRAM_CHAT_ID automatically from your message
    - sends a test message with live Approve/Reject buttons and confirms the
      callback round-trip actually works end to end
    - writes both secrets to your GitHub repo via `gh` if it's installed,
      otherwise prints the exact commands

Nothing is written to disk. The token is never logged.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = "https://api.telegram.org"


def call(token: str, method: str, **payload):
    req = urllib.request.Request(
        f"{API}/bot{token}/{method}",
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"ok": False, "description": f"HTTP {e.code}"}
    except urllib.error.URLError as e:
        return {"ok": False, "description": str(e)}


def die(msg: str) -> None:
    print(f"\n  ✗ {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> int:
    print(__doc__.split("What you do")[0].strip())
    print("\n" + "─" * 66)
    print("STEP 1 — create the bot")
    print("─" * 66)
    print("  Open Telegram → message @BotFather → /newbot")
    print("  Pick any name, then a username ending in 'bot'.")
    print("  BotFather replies with a token like 8123456789:AAH...\n")

    token = input("  Paste the token here: ").strip()
    if not token or ":" not in token:
        die("that doesn't look like a bot token (expected digits:letters)")

    me = call(token, "getMe")
    if not me.get("ok"):
        die(f"Telegram rejected the token: {me.get('description', '?')}")
    bot = me["result"]
    print(f"\n  ✓ token valid — @{bot.get('username')} ({bot.get('first_name')})")

    print("\n" + "─" * 66)
    print("STEP 2 — say hello to your bot")
    print("─" * 66)
    print(f"  Open  https://t.me/{bot.get('username')}  and send it any message.")
    print("  (A bot can't message you first — you have to speak first.)")
    print("\n  Waiting", end="", flush=True)

    chat_id = None
    sender = ""
    for _ in range(60):                      # up to ~2 minutes
        updates = call(token, "getUpdates", timeout=0)
        for u in reversed(updates.get("result") or []):
            msg = u.get("message") or u.get("edited_message")
            if msg and msg.get("chat"):
                chat_id = msg["chat"]["id"]
                sender = msg["chat"].get("username") or msg["chat"].get("first_name", "")
                break
        if chat_id:
            break
        print(".", end="", flush=True)
        time.sleep(2)

    if not chat_id:
        die("no message received. Send your bot a message, then re-run this.")

    print(f"\n\n  ✓ chat id discovered: {chat_id}  (@{sender})")

    print("\n" + "─" * 66)
    print("STEP 3 — verify the approval round-trip")
    print("─" * 66)
    sent = call(
        token, "sendMessage",
        chat_id=chat_id,
        text="Peptide Frontier approval channel is live.\n\nTap a button to "
             "confirm callbacks reach the workflow.",
        reply_markup={"inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": "a:setuptest"},
            {"text": "❌ Reject", "callback_data": "r:setuptest"},
        ]]},
    )
    if not sent.get("ok"):
        die(f"could not send test message: {sent.get('description', '?')}")
    print("  Sent. Tap either button in Telegram", end="", flush=True)

    tapped = None
    offset = 0
    for _ in range(45):
        updates = call(token, "getUpdates", offset=offset, timeout=0)
        for u in updates.get("result") or []:
            offset = max(offset, u.get("update_id", 0) + 1)
            cq = u.get("callback_query")
            if cq and cq.get("data", "").endswith("setuptest"):
                tapped = cq
                call(token, "answerCallbackQuery",
                     callback_query_id=cq["id"], text="Setup confirmed")
                break
        if tapped:
            break
        print(".", end="", flush=True)
        time.sleep(2)

    if not tapped:
        print("\n\n  ! no button tap detected. Secrets below are still correct —")
        print("    the approval flow just wasn't confirmed end to end.")
    else:
        verdict = "approve" if tapped["data"].startswith("a:") else "reject"
        print(f"\n\n  ✓ callback received: {verdict} from id "
              f"{tapped['from']['id']}")
        if str(tapped["from"]["id"]) != str(chat_id):
            print(f"  ! sender id {tapped['from']['id']} != chat id {chat_id};"
                  f" use TELEGRAM_CHAT_ID={tapped['from']['id']}")
            chat_id = tapped["from"]["id"]

    print("\n" + "─" * 66)
    print("STEP 4 — store the secrets")
    print("─" * 66)

    if shutil.which("gh"):
        try:
            repo = subprocess.run(
                ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
                capture_output=True, text=True, timeout=30,
            ).stdout.strip()
        except Exception:
            repo = ""
        if repo and input(f"  Set secrets on {repo} now? [Y/n] ").strip().lower() in ("", "y"):
            for name, value in (("TELEGRAM_BOT_TOKEN", token),
                                ("TELEGRAM_CHAT_ID", str(chat_id))):
                r = subprocess.run(["gh", "secret", "set", name, "--body", str(value)],
                                   capture_output=True, text=True)
                mark = "✓" if r.returncode == 0 else "✗"
                print(f"    {mark} {name}"
                      + ("" if r.returncode == 0 else f"  {r.stderr.strip()[:80]}"))
            print("\n  Done. The next drafting run will ping you.")
            return 0
    else:
        print("  (`gh` not installed — run these yourself)\n")

    print("\n  gh secret set TELEGRAM_BOT_TOKEN --body '<the token you pasted>'")
    print(f"  gh secret set TELEGRAM_CHAT_ID  --body '{chat_id}'")
    print("\n  …or paste them into Settings → Secrets and variables → Actions.")
    print(f"\n  TELEGRAM_CHAT_ID = {chat_id}")
    print("  TELEGRAM_BOT_TOKEN = (the token you pasted — not echoed here on purpose)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\naborted")
        raise SystemExit(130)
