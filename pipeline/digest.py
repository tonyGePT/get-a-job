#!/usr/bin/env python
"""Step 7: one summary a day saying what the pipeline did, and what needs a human.

Reads whatever the earlier steps left in state/, writes state/digest-<date>.md and posts
the same text to the Discord channel configured in lib/notify.py. No email.

Decisions are grouped by their final outcome, most useful first and SKIPPED last, so the
long tail can be scrolled past. Each row is: company, role, location, published salary.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import state as S  # noqa: E402
import notify  # noqa: E402

# Display order of the final outcomes. SKIPPED is last on purpose.
GROUPS = ("APPLIED", "UNVERIFIED", "NEEDS CODE", "FAILED", "ESCALATED", "QUEUED", "SKIPPED")
SESSIONS = os.path.expanduser("~/.omp/agent/sessions")


def final(status, verdict):
    """One word for where a posting ended up: the application outcome beats the verdict."""
    by_status = {"submitted": "APPLIED", "unverified": "UNVERIFIED", "needs_code": "NEEDS CODE",
                 "failed": "FAILED", "escalated": "ESCALATED"}
    if status in by_status:
        return by_status[status]
    if verdict == "ESCALATE":
        return "ESCALATED"
    if verdict == "APPLY":
        return "QUEUED"  # decided to apply, no application outcome yet
    return "SKIPPED"


def place(p):
    loc = (p.get("location") or "").strip()
    remote = (p.get("remote") or "").strip()
    if remote and remote != "unknown" and "remote" not in loc.lower():
        loc = "%s (%s)" % (loc, remote) if loc else remote
    return loc[:48] or "\u2014"


def row(p):
    return "- **%s** \u2014 %s \u00b7 %s \u00b7 %s" % (
        p.get("company") or "?", (p.get("title") or "?")[:70], place(p),
        (p.get("salary") or "").strip()[:40] or "\u2014")


def _human(n):
    for unit, size in (("M", 1e6), ("k", 1e3)):
        if n >= size:
            return "%.1f%s" % (n / size, unit)
    return str(int(n))


def agent_usage(day):
    """Tokens and cost of every omp session started from this project on `day` (local time),
    including the qualify/apply subagent sessions nested under each one. None if there are none."""
    root = os.path.join(SESSIONS, "--%s--" % re.sub(r"[:/\\]", "-", S.ROOT.strip("/\\")))
    if not os.path.isdir(root):
        return None
    tot = {"in": 0, "cached": 0, "out": 0, "cost": 0.0}
    files = 0
    for top in os.listdir(root):
        m = re.match(r"(\d{4}-\d\d-\d\d)T(\d\d)-(\d\d)-(\d\d)", top)
        if not m:
            continue
        started = datetime.strptime("%sT%s:%s:%s" % m.groups(), "%Y-%m-%dT%H:%M:%S")
        if started.replace(tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d") != day:
            continue
        full = os.path.join(root, top)
        paths = [full] if full.endswith(".jsonl") else []
        if os.path.isdir(full):
            for dirpath, _dirs, names in os.walk(full):
                paths += [os.path.join(dirpath, n) for n in names if n.endswith(".jsonl")]
        for pth in paths:
            files += 1
            with open(pth, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"usage"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    u = d.get("usage") if d.get("type") == "model_usage" else \
                        (d.get("message") or {}).get("usage") if d.get("type") == "message" else None
                    if not u:
                        continue
                    tot["in"] += u.get("input", 0) + u.get("cacheRead", 0) + u.get("cacheWrite", 0)
                    tot["cached"] += u.get("cacheRead", 0)
                    tot["out"] += u.get("output", 0)
                    tot["cost"] += (u.get("cost") or {}).get("total", 0) or 0
    if not files:
        return None
    line = "**Agent usage today:** %d session file(s) \u00b7 in %s (cache-read %s) \u00b7 out %s" % (
        files, _human(tot["in"]), _human(tot["cached"]), _human(tot["out"]))
    if tot["cost"]:
        line += " \u00b7 $%.2f" % tot["cost"]
    return line


def build():
    """Everything in the digest comes from state.db: the postings decided today, the postings
    whose application was made today (applied_at, not an evidence file mtime) and the runs
    touched today."""
    day = S.TODAY
    cand = S.read_json(S.path("candidates-latest.json"), {}) or {}
    conn = S.db()
    try:
        S.ingest_verdicts(conn=conn)  # backstop: today's verdicts reach state.db even if prepare.py crashed
        S.sync_evidence(conn)
        posts = {r["url_key"]: dict(r) for r in conn.execute(
            "SELECT * FROM postings WHERE substr(COALESCE(decided_at, ''), 1, 10)=?", (day,))}
        apps = [dict(r) for r in conn.execute("SELECT * FROM applications").fetchall()]
        # An application made today for a posting decided on an earlier day still belongs here.
        for a in apps:
            if (a.get("applied_at") or "")[:10] != day:
                continue
            k = a.get("url_key")
            if k and k not in posts:
                r = conn.execute("SELECT * FROM postings WHERE url_key=?", (k,)).fetchone()
                if r:
                    posts[k] = dict(r)
        runs = [dict(r) for r in S.runs(
            conn, "substr(COALESCE(started, ''), 1, 10)=? OR substr(COALESCE(finished, ''), 1, 10)=?",
            (day, day))]
    finally:
        conn.close()

    carried = [a for a in apps if a["status"] == "submitted"
               and (a.get("applied_at") or "")[:10] != day]
    groups = {g: [] for g in GROUPS}
    for p in posts.values():
        groups[final(p.get("app_status"), p.get("verdict"))].append(p)

    L = ["# Job pipeline \u2014 %s\n" % day]
    L.append("**Surfaced:** %d new postings (%d returned, %d dropped as decided/seen/agency/dupe)  "
             % (len(cand.get("candidates", [])), cand.get("returned", 0),
                sum((cand.get("dropped") or {}).values())))
    L.append("**Decided:** %d \u00b7 %s" % (
        len(posts), " \u00b7 ".join("%s %d" % (g.lower(), len(groups[g])) for g in GROUPS if groups[g])))
    usage = agent_usage(day)
    if usage:
        L.append(usage)
    if carried:
        L.append("_(%d earlier submission(s) re-verified, not new today)_" % len(carried))

    for g in GROUPS:
        if not groups[g]:
            continue
        L.append("\n## %s (%d)\n" % (g, len(groups[g])))
        for p in sorted(groups[g], key=lambda x: ((x.get("company") or "").lower(), x.get("title") or "")):
            L.append(row(p))

    if runs:
        L.append("\n## Outreach\n")
        for r in runs:
            L.append("- %s \u2014 %s (%s) \u2014 draft %s" % (
                r.get("company"), r.get("recipient") or "no contact",
                r.get("email_verdict") or r.get("status"), r.get("draft_id") or "none"))

    L.append("\n---\nState: `%s`" % S.STATE)
    return "\n".join(L)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-discord", action="store_true", help="write the file, post nothing")
    args = ap.parse_args()
    S.ensure_dirs()
    md = build()
    p = S.path("digest-%s.md" % S.TODAY)
    open(p, "w", encoding="utf-8").write(md)
    d = None if args.no_discord else notify.post_long(md)[1]
    print(md)
    print("\n[digest] %s | discord=%s" % (p, d))
    return 0


if __name__ == "__main__":
    sys.exit(main())
