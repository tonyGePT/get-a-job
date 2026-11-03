#!/usr/bin/env python3
"""Discord notifications for the daily job pipeline.

Transport (first that is configured wins), from config.json `notify`:
  1. bot:     `discord_channel_id` + token in `discord_bot_token_file`
              -> POST /channels/<id>/messages with `Authorization: Bot <token>`
  2. webhook: URL in `discord_webhook_file` (posts to whatever channel that webhook belongs to)

CLI:
  notify.py captcha --key <evidence key>   announce one CAPTCHA-blocked application
  notify.py scan [--since-hours 24]        announce every recent evidence file blocked by a CAPTCHA
  notify.py test                           post a one-line connectivity message

Each CAPTCHA is announced once per (key, day): `state/discord-sent.json` dedupes, so the apply
agent's live `captcha` call and the post-apply `scan` sweep never double-post.
Nothing is marked sent unless Discord accepted the message, so a later `scan` retries.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import state as S  # noqa: E402

N = S.CONFIG.get("notify", {})
API = "https://discord.com/api/v10"
# Discord (Cloudflare) rejects the default urllib User-Agent.
UA = "DiscordBot (https://github.com/tonyGePT/job-harness, 1.0)"
SENT = "discord-sent.json"


def _read(path):
    return open(path, encoding="utf-8").read().strip() if path and os.path.exists(path) else ""


def post(content):
    """Send one message. Returns (ok, detail); never raises."""
    content = content[:1900]
    token, channel = _read(N.get("discord_bot_token_file")), N.get("discord_channel_id")
    if token and channel:
        url, auth = "%s/channels/%s/messages" % (API, channel), {"Authorization": "Bot " + token}
    else:
        url, auth = _read(N.get("discord_webhook_file")), {}
        if not url.startswith("http"):
            return False, "discord not configured (no bot token file / webhook file)"
    req = urllib.request.Request(
        url, data=json.dumps({"content": content}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": UA, **auth})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return True, r.status
    except urllib.error.HTTPError as e:
        return False, "HTTP %s: %s" % (e.code, e.read()[:200].decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        return False, "discord error: %s" % e


def post_long(text, limit=1900):
    """Post text as consecutive messages, each <= limit chars, split on line boundaries."""
    chunks, cur = [], ""
    for line in text.split("\n"):
        line = line[:limit]
        if cur and len(cur) + len(line) + 1 > limit:
            chunks.append(cur)
            cur = ""
        cur = cur + "\n" + line if cur else line
    if cur.strip():
        chunks.append(cur)
    for c in chunks:
        ok, detail = post(c)
        if not ok:
            return ok, detail
    return True, "%d message(s)" % len(chunks)


def is_captcha(ev):
    text = " ".join(str(ev.get(k, "")) for k in ("blocking_question", "note")).lower()
    return "captcha" in text


def announce_captcha(ev):
    """Post one CAPTCHA alert for an evidence dict, once per key per day."""
    sent = S.read_json(S.path(SENT), {})
    mark = "captcha:%s:%s" % (ev["key"], S.TODAY)
    if mark in sent:
        return "already sent"
    msg = ("\U0001F9E9 **CAPTCHA blocked an application**\n"
           "**%s** \u2014 %s\n%s\n"
           "Form was filled; a human has to solve the CAPTCHA and submit."
           % (ev.get("company", "?"), ev.get("role", "?"), ev.get("final_url", "")))
    ok, detail = post(msg)
    if ok:
        sent[mark] = time.strftime("%Y-%m-%dT%H:%M:%S")
        S.write_json(S.path(SENT), sent)
    return "sent" if ok else "NOT sent: %s" % detail


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("captcha")
    c.add_argument("--key", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--since-hours", type=float, default=24)
    sub.add_parser("test")
    args = ap.parse_args()

    if args.cmd == "test":
        ok, detail = post("jobhunt notifications are connected.")
        print("ok" if ok else "FAILED", detail)
        return 0 if ok else 1

    if args.cmd == "captcha":
        ev = S.read_json(S.path("evidence", args.key + ".json"), None)
        if ev is None:
            print("no evidence file for %s" % args.key)
            return 1
        ev.setdefault("key", args.key)
        result = announce_captcha(ev)
        print("%s: %s" % (args.key, result))
        return 0 if result in ("sent", "already sent") else 1

    cutoff, failed = time.time() - args.since_hours * 3600, 0
    edir = S.path("evidence")
    for name in sorted(os.listdir(edir)):
        p = os.path.join(edir, name)
        if not name.endswith(".json") or os.path.getmtime(p) < cutoff:
            continue
        ev = S.read_json(p, {})
        if ev.get("status") != "submitted" and is_captcha(ev):
            ev.setdefault("key", name[:-5])
            result = announce_captcha(ev)
            print("%s: %s" % (ev["key"], result))
            failed += result.startswith("NOT")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
