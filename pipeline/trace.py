#!/usr/bin/env python
"""Step 6: turn confirmed applications into TraceHire runs and Gmail drafts.

TraceHire resolves the hiring contact for a posting and creates an unsent Gmail
draft. One run per posting URL, ever: state/runs in state.db is the dedupe source
and every write is one row in its own transaction, so a poll that waits 30
minutes can no longer overwrite a concurrent manual command.

Usage:
  python trace.py start            # queue runs for confirmed applications (staggered)
  python trace.py poll [--all]     # wait for terminal runs, record drafts
  python trace.py                  # start, then poll
  python trace.py retry [--dry-run]  # retry transient failures (cap includes retries)
  python trace.py status           # table of every run recorded
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import state as S  # noqa: E402

T = S.CONFIG["tracehire"]
KEY = open(S.CONFIG["tracehire"]["key_file"]).read().strip()

LIVE = ("queued", "running", None)
STALE_AFTER_H = 6

# TraceHire's own vocabulary (src/worker/workflow.ts, src/shared/dto.ts):
# statuses queued|running|done|skipped|failed|killed|budget, stop reasons
# no_people / no_confirmed_people / no_contact / phone_only / "killed …" /
# free text (router skip) / "error: <message>".
#
# Posting-read failures come in two wordings and BOTH are retryable:
#   2026-10-27 and earlier runs: "treg.web.extract returned no page markdown (<status>)"
#   the 2026-10-28 build onward: "could not read the job posting: <servedBy> returned N
#                                characters, crawl4ai.web.scrape returned M"
# After that build TraceHire already makes a second provider attempt itself, so one
# jobhunt-level retry (the cap below) is still the right ceiling: a third pass over
# the same URL almost never changes the outcome and costs ~$0.07.
DETERMINISTIC = {
    "no_people", "no_confirmed_people", "no_contact", "phone_only",
    "killed before start", "budget",
}
TRANSIENT = re.compile(
    r"(treg\.web\.extract|no page markdown|could not read the job posting|"
    r"crawl4ai\.web\.scrape returned|transport|connection|socket|econn|"
    r"time\w* ?out|timed out|fetch failed|network|rate limit|\b429\b|\b50[0234]\b|"
    r"http_error|upstream|unavailable|internal error|bad gateway|service unavailable)",
    re.I)


def api(path_, body=None):
    req = urllib.request.Request(
        T["base"] + path_,
        data=json.dumps(body).encode() if body is not None else None,
        method="POST" if body is not None else "GET",
        headers={"Authorization": "Bearer " + KEY, "User-Agent": "jobhunt/1.0",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"http_error": e.code, "body": e.read().decode()[:400]}
    except Exception as e:  # noqa: BLE001
        return {"transport_error": str(e)}


def confirmed_applications(conn):
    """Applications that verify.py accepted, as [{key,company,title,url,url_key}].

    TraceHire must read the job POSTING, so the URL is the posting's apply URL from the
    apply queue or from the posting's recorded verdict. Never the evidence `final_url`:
    that is the ATS confirmation page (/confirmation, /thanks/..., /jobTasks/completed/...),
    which has no job description, and its URL never matches the run already recorded for
    the posting — on 2026-10-28 that re-traced six applications and duplicated drafts.
    An application with no known posting URL (made by hand, outside this pipeline) is
    not traced here; the session that made it owns its outreach.
    """
    queue = {e["key"]: e for e in (S.read_json(S.path("apply-queue.json"), {}) or {}).get("queue", [])
             if e.get("key")}
    rows = conn.execute(
        "SELECT a.*, p.apply_url AS posting_apply_url FROM applications a "
        "LEFT JOIN postings p ON p.url_key = a.url_key "
        "WHERE a.status='submitted' ORDER BY COALESCE(a.applied_at, a.updated_at)"
    ).fetchall()
    out = []
    for r in rows:
        url = (queue.get(r["key"]) or {}).get("apply_url") or r["posting_apply_url"]
        if not url:
            continue
        out.append({"key": r["key"], "company": r["company"], "title": r["role"],
                    "url": url, "url_key": S.norm_url(url)})
    return out


def error_text(res):
    if not res:
        return None
    if res.get("transport_error"):
        return "transport: %s" % res["transport_error"]
    if res.get("http_error"):
        return "http %s: %s" % (res["http_error"], (res.get("body") or "")[:200])
    if res.get("error"):
        return str(res["error"])
    return json.dumps(res)[:200]


def retry_decision(rec):
    """(retry?, reason) for one recorded run.

    A start that never got a run id, a run with no terminal state in 6h, and a
    failure whose message carries a transient/upstream signature are retried.
    Deterministic outcomes are not: each retry costs ~$0.07 and cannot change
    the answer when TraceHire simply found nobody.
    """
    status = rec["status"] or ""
    stop = (rec["stop_reason"] or "").strip()
    err = rec["error"] or ""
    if status == "start_failed":
        return True, "start failed: %s" % (err or "no run id")[:60]
    if status == "stale":
        return True, "no terminal state within %dh" % STALE_AFTER_H
    if status != "failed":
        return False, "terminal status %s" % (status or "-")
    if stop in DETERMINISTIC or stop.startswith("killed"):
        return False, "deterministic: %s" % stop
    if stop.startswith("error:") and TRANSIENT.search(stop):
        return True, "transient: %s" % stop[:70]
    if err and TRANSIENT.search(err):
        return True, "transient: %s" % err[:70]
    if not stop and not err:
        return False, "failed without a transient signature"
    return False, "%s" % (stop or err)[:70]


def cmd_start(dry_run=False):
    conn = S.db()
    try:
        cap = S.CONFIG["limits"]["max_trace_runs_per_day"]
        budget = max(0, cap - S.runs_started_on(conn, S.TODAY))
        apps = confirmed_applications(conn)
        todo = [a for a in apps if S.run_get(conn, a["url_key"]) is None]
        if len(todo) > budget:
            print("daily cap %d reached: starting %d of %d" % (cap, budget, len(todo)))
            todo = todo[:budget]
        if not todo:
            print("no new confirmed applications to trace (%d known, %d app(s)/%d start(s) left)"
                  % (len(apps), budget, cap))
            return 0
        if dry_run:
            for a in todo:
                print("WOULD START %-14s %s" % (a["key"], a["url"]))
            print("dry run: %d start(s), nothing posted" % len(todo))
            return 0
        for i, a in enumerate(todo):
            res = api("/api/v1/runs", {"job_url": a["url"]})
            run = res.get("run") or {}
            rid = run.get("id") or res.get("id")
            rec = {"url_key": a["url_key"], "company": a["company"], "title": a["title"],
                   "url": a["url"], "run_id": rid, "started": S.now(),
                   "first_started": S.now(), "attempts": 1, "retries": 0}
            if rid:
                rec["status"] = run.get("status") or "queued"
            else:
                # never orphan a start: it is recorded as start_failed and retried
                rec["status"] = "start_failed"
                rec["error"] = error_text(res)
            S.run_upsert(conn, rec)
            print("%d/%d %-14s %s %s" % (i + 1, len(todo), a["key"], rid or "START-FAILED",
                                         "" if rid else rec["error"]))
            if i + 1 < len(todo):
                time.sleep(T["stagger_seconds"])  # Workers AI per-minute limits
    finally:
        conn.close()
    return 0


def cmd_poll(all_runs=False):
    conn = S.db()
    try:
        live = LIVE + (("stale",) if all_runs else ())
        pending = {r["url_key"]: r for r in S.runs(
            conn, "status IN (%s) AND run_id IS NOT NULL AND run_id<>''"
            % ",".join("?" for _ in live), live)}
        cutoff = time.strftime("%Y-%m-%dT%H:%M:%S",
                               time.localtime(time.time() - STALE_AFTER_H * 3600))
        for row in S.runs(conn, "status IN ('queued','running')"):
            last = row["restarted"] or row["started"] or ""
            if last and last < cutoff:
                S.run_update(conn, row["url_key"], status="stale",
                             note="no terminal state within %dh; retry with trace.py retry"
                             % STALE_AFTER_H)
                pending.pop(row["url_key"], None)
                print("marked stale: %s (%s since %s)" % (row["company"], row["url_key"], last))
        if not pending:
            print("no runs pending")
            return 0
        deadline = time.time() + T["poll_timeout_minutes"] * 60
        while pending and time.time() < deadline:
            for key, rec in list(pending.items()):
                body = api("/api/v1/runs/" + rec["run_id"])
                run = body.get("run") or {}
                if body.get("http_error") in (404, 410):
                    # TraceHire no longer knows this run (purged or never created):
                    # terminal, and not retryable as a transient error.
                    S.run_update(conn, key, status="failed",
                                 stop_reason="error: run not found (http %s)" % body["http_error"],
                                 finished=S.now())
                    pending.pop(key)
                    print("[%s] %-14s gone: http %s" % (time.strftime("%H:%M:%S"),
                                                        rec["company"], body["http_error"]), flush=True)
                    continue
                if body.get("transport_error") or body.get("http_error"):
                    print("  .. %s: %s" % (rec["company"], error_text(body)), flush=True)
                    continue
                st = run.get("status")
                pending[key] = dict(rec, **{"status": st, "run_id": rec["run_id"]})
                if st in LIVE:
                    continue
                rc = run.get("recipient") or {}
                fields = {"status": st, "stop_reason": run.get("stopReason"),
                          "recipient": rc.get("name"), "recipient_title": rc.get("title"),
                          "email_verdict": rc.get("emailVerdict"),
                          "draft_id": (run.get("draft") or {}).get("gmailDraftId"),
                          "finished": S.now()}
                S.run_update(conn, key, **fields)
                pending.pop(key)
                print("[%s] %-14s %-8s %-22s %s draft=%s" % (
                    time.strftime("%H:%M:%S"), rec["company"], st,
                    (fields["recipient"] or "-")[:22], fields["email_verdict"] or "-",
                    fields["draft_id"] or "-"), flush=True)
            if pending:
                print("  .. %d still running" % len(pending), flush=True)
                time.sleep(T["poll_seconds"])
        today = [dict(r) for r in S.runs(conn, "substr(started, 1, 10)=?", (S.TODAY,))]
        S.write_json(S.path("trace-results-%s.json" % S.TODAY),
                     {"generated": S.now(), "runs": today, "pending": sorted(pending)})
    finally:
        conn.close()
    return 0


def cmd_retry(dry_run=False):
    """Re-run transient failures, one retry each, inside the daily run cap."""
    conn = S.db()
    try:
        cap = S.CONFIG["limits"]["max_trace_runs_per_day"]
        budget = max(0, cap - S.runs_started_on(conn, S.TODAY))
        todo, skipped = [], []
        for rec in S.runs(conn, "1=1"):
            ok, why = retry_decision(rec)
            if ok and rec["retries"] >= 1:
                skipped.append((rec, "already retried once (%s)" % why))
            elif ok:
                todo.append((rec, why))
            else:
                skipped.append((rec, why))
        if dry_run:
            print("retryable: %d  (start/retry budget left today: %d of %d)"
                  % (len(todo), budget, cap))
            for rec, why in todo:
                print("  RETRY %-14s %-12s %s" % (rec["company"], rec["status"], why))
            print("not retryable: %d" % len(skipped))
            for rec, why in skipped:
                print("  skip  %-14s %-12s %s" % (rec["company"], rec["status"], why))
            return 0
        if len(todo) > budget:
            print("daily cap %d reached: retrying %d of %d" % (cap, budget, len(todo)))
            todo = todo[:budget]
        if not todo:
            print("nothing retryable (%d deterministic failure(s) left alone)" % len(skipped))
            return 0
        for rec, why in todo:
            res = api("/api/v1/runs", {"job_url": rec["url"], "allow_duplicate": True})
            run = res.get("run") or {}
            rid = run.get("id") or res.get("id")
            attempts = (rec["attempts"] or 0) + 1
            if not rid:
                S.run_update(conn, rec["url_key"], status="start_failed",
                             error=error_text(res), started=S.now(), attempts=attempts,
                             note="retry failed to start (%s)" % why)
                print("retry-start-failed %-14s %s" % (rec["company"], error_text(res)))
                continue
            S.run_update(conn, rec["url_key"], run_id=rid,
                         status=run.get("status") or "queued", attempts=attempts,
                         retries=(rec["retries"] or 0) + 1,
                         first_started=rec["first_started"] or rec["started"],
                         restarted=S.now(), started=S.now(), previous_run=rec["run_id"],
                         note="retried: %s" % why, stop_reason=None, error=None,
                         finished=None, draft_id=None, recipient=None,
                         recipient_title=None, email_verdict=None)
            print("retry %-14s %s (%s)" % (rec["company"], rid, why))
            time.sleep(T["stagger_seconds"])
        print("retried %d" % len(todo))
    finally:
        conn.close()
    return 0


def cmd_status():
    conn = S.db()
    try:
        rows = S.runs(conn, "1=1")
    finally:
        conn.close()
    rows = sorted(rows, key=lambda r: r["started"] or "", reverse=True)
    print("%-10s %-18s %-12s %-22s %-8s %-10s %s" % (
        "STARTED", "COMPANY", "STATUS", "RECIPIENT", "VERDICT", "DRAFT", "STOP/ERROR"))
    for r in rows[:60]:
        print("%-10s %-18s %-12s %-22s %-8s %-10s %s" % (
            (r["started"] or "").replace("T", " ")[5:16], (r["company"] or "")[:18],
            r["status"] or "-", (r["recipient"] or "-")[:22],
            r["email_verdict"] or "-", (r["draft_id"] or "-")[:10],
            (r["stop_reason"] or r["error"] or "")[:34]))
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd == "start":
        sys.exit(cmd_start("--dry-run" in sys.argv[2:]))
    if cmd == "poll":
        sys.exit(cmd_poll("--all" in sys.argv[2:]))
    if cmd == "status":
        sys.exit(cmd_status())
    if cmd == "retry":
        sys.exit(cmd_retry("--dry-run" in sys.argv[2:]))
    cmd_start()
    sys.exit(cmd_poll())
