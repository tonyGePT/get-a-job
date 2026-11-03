#!/usr/bin/env python3
"""Solve a token CAPTCHA through RapidAPI "Fast MultiSolver" (2captcha-style in.php/res.php).

Same backend as the `captcha-solver` MCP server in ~/.omp/agent/mcp.json (Create_Request /
Get_Response). The apply agent runs this from bash because it blocks until the token is
ready (the MCP tools need a hand-rolled 5 s poll loop) and enforces the daily cap. The API
key is read from that mcp.json entry at run time: one source, never copied into this repo.

  python captcha.py solve --method turnstile --sitekey 0x4AAA... --url https://job-boards.greenhouse.io/x/jobs/1
  python captcha.py solve --method userrecaptcha --sitekey 6Lc... --url <page> [--key <evidence key>]
  python captcha.py status                      # solves used today vs the cap

Methods: userrecaptcha (reCAPTCHA v2, also invisible), recaptchaV3 (needs --action),
hcaptcha, turnstile, captchafox. Image/slider/"click the cats" challenges are NOT supported.

stdout is the token only (so it can be captured); everything else goes to stderr.
Exit 0 token printed | 1 solver failed or timed out | 2 usage/config | 3 daily cap reached |
4 solver unavailable after every retry (do not re-run it: escalate).

Creating the request is retried with exponential backoff (5 s, 10 s, 20 s ... capped at 180 s
between attempts, 8 attempts, about 8 minutes worst case) when the provider reports no capacity
(NO_FREE_SERVERS_AVAILABLE, NO_SLOT), a network error, HTTP 429 or a 5xx. Nothing is billed until
a request is accepted, so these retries are free. Once accepted, a failed or slow solve is NOT
retried here (it is billed): the caller decides.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import state as S  # noqa: E402

HOST = "fast-multisolver.p.rapidapi.com"
MCP_JSON = os.path.expanduser("~/.omp/agent/mcp.json")
METHODS = ("userrecaptcha", "recaptchaV3", "hcaptcha", "turnstile", "captchafox")
LEDGER = "captcha-solves.json"
DEFAULT_CAP = 10
POLL_SECONDS = 5
TIMEOUT_SECONDS = 180
UA = "jobhunt-captcha/1.0"


def err(msg):
    print(msg, file=sys.stderr, flush=True)


def api_key():
    try:
        d = json.load(open(MCP_JSON, encoding="utf-8"))
        return d["mcpServers"]["captcha-solver"]["headers"]["x-api-key"]
    except (OSError, KeyError, ValueError):
        return None


def cap():
    return int(S.CONFIG.get("limits", {}).get("max_captcha_solves_per_day", DEFAULT_CAP))


def ledger():
    return S.read_json(S.path(LEDGER), {}) or {}


def used_today(led):
    return len(led.get(S.TODAY, []))


def call(path, params, key):
    url = "https://%s%s?%s" % (HOST, path, urllib.parse.urlencode(params))
    req = urllib.request.Request(url, headers={
        "x-rapidapi-host": HOST, "x-rapidapi-key": key, "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read().decode("utf-8", "replace").strip()
    except urllib.error.HTTPError as e:
        return "HTTP_%d %s" % (e.code, e.read().decode("utf-8", "replace")[:300])
    except (urllib.error.URLError, TimeoutError) as e:
        return "NETWORK_ERROR %s" % e


BACKOFF_START = 5
BACKOFF_CAP = 180
MAX_CREATE_ATTEMPTS = 8
TRANSIENT = ("NO_FREE_SERVERS", "NO_SLOT", "NETWORK_ERROR", "HTTP_429", "HTTP_500", "HTTP_502",
             "HTTP_503", "HTTP_504")


def backoff(attempt):
    """Seconds to wait after failed attempt number `attempt` (1-based): 5, 10, 20, ... <= 180."""
    return min(BACKOFF_CAP, BACKOFF_START * 2 ** (attempt - 1))


def create(params, key, attempts):
    """POST in.php, retrying transient failures. Returns (reply, transient_exhausted)."""
    for n in range(1, attempts + 1):
        reply = call("/in.php", params, key)
        if reply.startswith("OK|"):
            return reply, False
        if not any(t in reply for t in TRANSIENT):
            return reply, False
        if n == attempts:
            return reply, True
        wait = backoff(n)
        err("create attempt %d/%d failed (%s); retrying in %ds" % (n, attempts, reply[:80], wait))
        time.sleep(wait)
    return reply, True


def solve(a):
    key = api_key()
    if not key:
        err("no captcha-solver entry in %s" % MCP_JSON)
        return 2
    led = ledger()
    if used_today(led) >= cap():
        err("daily CAPTCHA cap reached (%d); escalate instead" % cap())
        return 3

    params = {"method": a.method, "sitekey": a.sitekey, "pageurl": a.url}
    for field in ("action", "data", "pagedata", "useragent"):
        if getattr(a, field):
            params[field] = getattr(a, field)
    if a.method == "recaptchaV3" and not a.action:
        err("recaptchaV3 needs --action (the grecaptcha.execute action string)")
        return 2

    created, exhausted = create(params, key, max(1, a.max_attempts))
    if not created.startswith("OK|"):
        err("create failed: %s" % created)
        return 4 if exhausted else 1
    rid = created.split("|", 1)[1]

    # Count the attempt as soon as the request is accepted: a failed solve is still billed.
    entry = {"at": S.now(), "method": a.method, "url": a.url, "key": a.key, "id": rid}
    led.setdefault(S.TODAY, []).append(entry)
    S.write_json(S.path(LEDGER), led)

    deadline = time.time() + TIMEOUT_SECONDS
    while time.time() < deadline:
        time.sleep(POLL_SECONDS)
        res = call("/res.php", {"id": rid}, key)
        if res.startswith("OK|"):
            print(res.split("|", 1)[1], flush=True)
            return 0
        if res != "CAPCHA_NOT_READY":
            err("solver failed: %s" % res)
            return 1
    err("timed out after %ds waiting for id %s" % (TIMEOUT_SECONDS, rid))
    return 1


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("solve")
    s.add_argument("--method", required=True, choices=METHODS)
    s.add_argument("--sitekey", required=True)
    s.add_argument("--url", required=True, help="page URL the CAPTCHA is on")
    s.add_argument("--action", help="recaptchaV3 only")
    s.add_argument("--data", help="hcaptcha/turnstile rqdata, optional")
    s.add_argument("--pagedata", help="turnstile chlPageData, optional")
    s.add_argument("--useragent", help="browser UA, optional")
    s.add_argument("--key", help="evidence key, for the ledger only")
    s.add_argument("--max-attempts", type=int, default=MAX_CREATE_ATTEMPTS,
                   help="create attempts incl. the first (default %d)" % MAX_CREATE_ATTEMPTS)
    sub.add_parser("status")
    a = ap.parse_args()
    if a.cmd == "status":
        print("%d/%d solves used today" % (used_today(ledger()), cap()))
        return 0
    return solve(a)


if __name__ == "__main__":
    sys.exit(main())
