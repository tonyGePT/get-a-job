#!/usr/bin/env python3
"""Deterministic evidence collector for the post-run troubleshooter (retro.py).

Why this exists: the retro agent must not read raw logs, session transcripts or the
database itself. Those are megabytes, and an agent that re-derives them every night
costs more than the pipeline it is supposed to optimise. Everything here is cheap
Python; the agent gets one compact JSON packet (state/retro-<date>-evidence.json)
plus a handful of file paths.

Nothing in this module writes pipeline state. It only reads, and the caller decides
what to persist. Every function is defensive: a missing artefact is a finding, not a
crash, because the retro runs on days when the pipeline itself broke.

Public entry point: collect(day) -> dict (also written by retro.py to state/).
"""

import glob
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import state as S

SESSIONS = os.path.expanduser("~/.omp/agent/sessions")

# A stage-start line written by daily.py's stage():  === [08:00:01] qualify (144 candidates) ===
STAGE_RE = re.compile(r"^=== \[(\d\d):(\d\d):(\d\d)\] (.+?) ===$")
# Failure markers daily.py and the stages emit on stdout.
FAIL_RE = re.compile(
    r"(pipeline stopped|exited [1-9]\d*|returned [1-9]\d*; continuing|traceback \(most recent call last\)|"
    r"heartbeat failed|exit code [1-9]|SystemExit|solver failed|quota exceeded|provider unavailable)",
    re.I)

# What a token costs when the provider reports no cost (muskapi2 reports $0 for gpt-6.1-terra).
# Rates are per million tokens; only used when usage.cost.total is missing or zero.
FALLBACK_RATES = {
    "gpt-6.1-terra": {"in": 1.25, "cached": 0.125, "out": 10.0},
    "deepseek-v4.1-flash": {"in": 0.25, "cached": 0.025, "out": 1.0},
    "claude-sonnet-5-5": {"in": 3.0, "cached": 0.30, "out": 15.0},
}

# Metrics tracked across days. `direction` says which way is better; the grader uses it
# to decide whether a retro action helped or hurt.
KPI_DIRECTION = {
    "cost_total_usd": "down",
    "cost_qualify_usd": "down",
    "cost_apply_usd": "down",
    "cost_per_returned_usd": "down",
    "cost_per_decided_usd": "down",
    "cost_per_application_usd": "down",
    "qualify_skip_pct": "down",             # LLM work that deterministic filters should have done
    "qualify_unprocessed": "down",          # candidates surfaced but never verdicted
    "apply_failed_pct": "down",
    "apply_escalated_pct": "down",
    "apply_submitted_pct": "up",
    "verify_receipt_pct": "up",
    "trace_success_pct": "up",
    "tool_calls_per_candidate": "down",
    "prompt_tokens_per_candidate": "down",
}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _i(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return 0


def _ratio(a, b):
    return round(100.0 * a / b, 1) if b else None


def _cost(usage):
    """(tokens, cached, out, billed_usd, estimated_usd) for one usage record.

    Cost is estimated when the provider reports zero (muskapi2 reports $0 for gpt-6.1-terra),
    so a model whose price is simply unrecorded is never mistaken for an optimised one.
    """
    u = usage or {}
    inp = _i(u.get("input"))
    cached = _i(u.get("cacheRead"))
    written = _i(u.get("cacheWrite"))
    out = _i(u.get("output"))
    billed = _f(((u.get("cost") or {}).get("total")))
    est = 0.0
    if billed <= 0 and (inp or cached or out or written):
        model = str(u.get("model") or "")
        for name, rate in FALLBACK_RATES.items():
            if name in model:
                est = (inp * rate["in"] + (cached + written) * rate["cached"]
                       + out * rate["out"]) / 1e6
                break
    return inp + cached + written, cached, out, billed, est


def _read_json(p, default=None):
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _tail(path, limit=400_000):
    """Last `limit` bytes of a text file, as lines. Logs can grow without bound; only the
    tail of today's run matters to the retro."""
    try:
        size = os.path.getsize(path)
        with open(path, encoding="utf-8", errors="replace") as fh:
            if size > limit:
                fh.seek(size - limit)
                fh.readline()          # drop the partial first line
            return fh.read().splitlines()
    except OSError:
        return []


# ---------------------------------------------------------------------------
# session store: tokens, cost, tool calls, per stage and per worker
# ---------------------------------------------------------------------------

def session_root(root=None):
    """The session directory omp uses for this project (same rule as digest.py)."""
    root = root or S.ROOT
    return os.path.join(SESSIONS, "--%s--" % re.sub(r"[:/\\]", "-", root.strip("/\\")))


def _session_started(name):
    m = re.match(r"(\d{4}-\d\d-\d\d)T(\d\d)-(\d\d)-(\d\d)", name)
    if not m:
        return None
    return datetime.strptime("%sT%s:%s:%s" % m.groups(), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def _scan_jsonl(path, bucket):
    """Fold one transcript's usage and tool calls into `bucket` (mutated).

    Usage records exist only as `model_usage` (auxiliary calls) and as assistant `message`
    records, so one `json.loads` per line carrying `"usage"` covers both. Tool calls are
    counted on the same assistant lines: the first version of this function used an
    `elif`, which silently reported zero tool calls for every session.
    """
    tool_calls = 0
    first_prompt = ""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"usage"' in line:
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    msg = d.get("message") or {}
                    u = d.get("usage") if d.get("type") == "model_usage" else msg.get("usage")
                    if u:
                        u = dict(u, model=d.get("model") or msg.get("model"))
                        inp, cached, out, billed, est = _cost(u)
                        bucket["in"] += inp
                        bucket["cached"] += cached
                        bucket["out"] += out
                        bucket["cost_billed"] += billed
                        bucket["cost_est"] += est
                        bucket["calls"] += 1
                    tool_calls += line.count('"toolCall"')
                    continue
                if '"toolCall"' in line:
                    tool_calls += line.count('"toolCall"')
                if not first_prompt and '"role": "user"' in line or (not first_prompt and '"role":"user"' in line):
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    msg = d.get("message") or {}
                    content = msg.get("content")
                    if isinstance(content, list) and content:
                        first_prompt = str(content[0].get("text") or "")[:400]
                    elif isinstance(content, str):
                        first_prompt = content[:400]
    except OSError:
        return bucket
    bucket["tool_calls"] += tool_calls
    if first_prompt and not bucket.get("first_prompt"):
        bucket["first_prompt"] = first_prompt
    return bucket


def _stage_of(prompt):
    """Which pipeline stage started this session, from the prompt text qualify.md/apply.md."""
    p = (prompt or "").lower()
    if "daily qualification pass" in p:
        return "qualify"
    if "daily apply pass" in p:
        return "apply"
    if "post-run troubleshooter" in p:
        return "retro"
    return "other"


def session_usage(day):
    """Per-stage token/cost/tool-call profile for the local day `day`, plus per-worker detail.

    Returns {"stages": {...}, "top_workers": [...], "files": n}. Stage attribution uses the
    top-level session's first user prompt (daily.py passes prompts/<stage>.md verbatim), never
    the clock, so a run that crosses midnight is still attributed correctly.
    """
    root = session_root()
    stages = {}
    workers = []
    files = 0
    if not os.path.isdir(root):
        return {"stages": {}, "top_workers": [], "files": 0}

    def bucket(name):
        return stages.setdefault(name, {"in": 0, "cached": 0, "out": 0, "cost_billed": 0.0,
                                        "cost_est": 0.0, "calls": 0, "tool_calls": 0,
                                        "sessions": 0, "workers": 0, "first_prompt": ""})

    fields = ("in", "cached", "out", "cost_billed", "cost_est", "calls", "tool_calls")
    for name in sorted(os.listdir(root)):
        started = _session_started(name)
        if started is None:
            continue
        if started.astimezone().strftime("%Y-%m-%d") != day:
            continue
        full = os.path.join(root, name)
        if not full.endswith(".jsonl"):
            continue          # the sibling directory: folded in through its own .jsonl below
        files += 1
        top = {"in": 0, "cached": 0, "out": 0, "cost_billed": 0.0, "cost_est": 0.0,
               "calls": 0, "tool_calls": 0, "sessions": 1, "workers": 0, "first_prompt": ""}
        _scan_jsonl(full, top)
        wdir = full[:-len(".jsonl")]
        if os.path.isdir(wdir):
            for f in sorted(os.listdir(wdir)):
                if not f.endswith(".jsonl"):
                    continue
                w = {"in": 0, "cached": 0, "out": 0, "cost_billed": 0.0, "cost_est": 0.0,
                     "calls": 0, "tool_calls": 0, "sessions": 1, "workers": 0,
                     "first_prompt": "", "name": f[:-5], "session": name}
                _scan_jsonl(os.path.join(wdir, f), w)
                workers.append(w)
                for k in fields:
                    top[k] += w.get(k, 0)
                top["workers"] += 1
        stage = _stage_of(top.get("first_prompt"))
        b = bucket(stage)
        for k in fields + ("sessions", "workers"):
            b[k] += top.get(k, 0)
        if not b["first_prompt"]:
            b["first_prompt"] = top.get("first_prompt", "")
    for s in stages.values():
        s["cost_billed"] = round(s["cost_billed"], 4)
        s["cost_est"] = round(s["cost_est"], 4)
    workers.sort(key=lambda w: (w["in"] + w["out"]), reverse=True)
    return {"stages": stages, "top_workers": workers[:8], "files": files}


# ---------------------------------------------------------------------------
# run log: stage timings, failures, and the lines a troubleshooter needs
# ---------------------------------------------------------------------------

def log_scan(day):
    """Stage timings and failure lines from state/logs/<day>.log."""
    path = S.path("logs", "%s.log" % day)
    lines = _tail(path)
    stages, marks, cur = [], [], None
    for idx, line in enumerate(lines):
        m = STAGE_RE.match(line.strip())
        if m:
            if cur:
                cur["lines"] = idx - cur["_idx"]
                stages.append({k: v for k, v in cur.items() if not k.startswith("_")})
            cur = {"at": "%s:%s:%s" % m.groups()[:3], "stage": m.group(4), "_idx": idx}
            continue
        s = line.strip()
        if not s:
            continue
        if FAIL_RE.search(s):
            marks.append({"line": idx + 1, "text": s[:300]})
    if cur:
        cur["lines"] = len(lines) - cur["_idx"]
        stages.append({k: v for k, v in cur.items() if not k.startswith("_")})
    return {"path": path, "exists": bool(lines), "lines": len(lines),
            "stages": stages, "marks": marks[:40],
            "first": lines[0][:200] if lines else "", "last": lines[-1][:200] if lines else ""}


def stage_durations(log):
    """Wall-clock seconds between consecutive stage starts (the last stage has no end marker)."""
    out = {}
    prev = None
    for s in log.get("stages") or []:
        if prev:
            try:
                a = datetime.strptime(prev["at"], "%H:%M:%S")
                b = datetime.strptime(s["at"], "%H:%M:%S")
                out[prev["stage"].split()[0]] = int((b - a).total_seconds())
            except ValueError:
                pass
        prev = s
    return out


# ---------------------------------------------------------------------------
# artefacts: candidates, verdicts, queue, evidence, verification, trace
# ---------------------------------------------------------------------------

def funnel(day, conn):
    """The day's counts.

    Careful: `applications.updated_at` is rewritten on every verify pass for every row, so it
    says "touched", not "happened". Today's application outcomes come from today's evidence
    files (what the apply agent actually produced); `applied_at` is only a first-submission
    stamp. Mixing the two produced a 307% escalation rate in the first version.
    """
    cand = _read_json(S.path("candidates-latest.json"), {}) or {}
    q = conn.execute(
        "SELECT COUNT(*) n FROM postings WHERE substr(COALESCE(decided_at,''),1,10)=?", (day,)
    ).fetchone()["n"]
    by_verdict = {}
    for r in conn.execute(
            "SELECT verdict, COUNT(*) n FROM postings WHERE substr(COALESCE(decided_at,''),1,10)=? "
            "GROUP BY verdict", (day,)):
        by_verdict[r["verdict"] or "NONE"] = r["n"]
    queue = (_read_json(S.path("apply-queue.json"), {}) or {}).get("queue") or []
    ev = apply_outcomes(day, conn)
    trace = _read_json(S.path("trace-results-%s.json" % day), {}) or {}
    runs = trace.get("runs") or []
    submitted_today = conn.execute(
        "SELECT COUNT(*) n FROM applications WHERE substr(COALESCE(applied_at,''),1,10)=?",
        (day,)).fetchone()["n"]
    receipts_today = conn.execute(
        "SELECT COUNT(*) n FROM receipts WHERE substr(COALESCE(found_at,''),1,10)=?", (day,)
    ).fetchone()["n"]
    return {
        "returned": _i(cand.get("returned")),
        "surfaced": len(cand.get("candidates") or []),
        "dropped": {k: _i(v) for k, v in (cand.get("dropped") or {}).items()},
        "decided": q,
        "verdicts": by_verdict,
        "applications": ev["counts"],
        "submitted_today": submitted_today,
        "receipts_today": receipts_today,
        "queued": len(queue),
        "queue_skipped": len((_read_json(S.path("apply-queue.json"), {}) or {}).get("skipped") or []),
        "trace_runs": len(runs),
        "trace_status": _counts(r.get("status") for r in runs),
        "trace_drafts": sum(1 for r in runs if r.get("draft_id")),
    }



def skip_reasons(day, conn):
    """Why the LLM said SKIP, grouped. These are the deterministic-filter candidates:
    every one of them could have been dropped at discover.py for free."""
    rows = [dict(r) for r in conn.execute(
        "SELECT company, company_key, title, reason, blockers, location, salary FROM postings "
        "WHERE substr(COALESCE(decided_at,''),1,10)=? AND verdict='SKIP'", (day,)).fetchall()]
    by_company, by_reason, companies = {}, {}, {}
    for r in rows:
        by_company[r["company_key"]] = by_company.get(r["company_key"], 0) + 1
        companies[r["company_key"]] = r["company"] or r["company_key"]
        text = " ".join(str(r.get(k) or "") for k in ("reason", "blockers")).lower()
        cats = []
        if re.search(r"staffing|agency|recruit|contract house|consultanc|repost", text):
            cats.append("staffing/agency")
        if re.search(r"on-?site|hybrid|relocat|in-?office|days (a|per) week", text):
            cats.append("location")
        if re.search(r"years|senior|principal|staff|lead|director|manager", text):
            cats.append("seniority")
        if re.search(r"\$|salary|compensation|hour|/hr|rate", text):
            cats.append("compensation")
        if re.search(r"not (an? )?(engineering|relevant|target)|unrelated|different (role|discipline)", text):
            cats.append("role-mismatch")
        if not cats:
            cats.append("other")
        for c in cats:
            by_reason[c] = by_reason.get(c, 0) + 1
    repeat = [{"company_key": k, "company": companies[k], "skips": n,
               "prior_skips": _prior_skips(conn, k, day)}
              for k, n in sorted(by_company.items(), key=lambda kv: -kv[1]) if n >= 1]
    return {
        "count": len(rows),
        "by_reason": by_reason,
        "companies_skipped_twice_plus": [r for r in repeat
                                         if r["skips"] + r["prior_skips"] >= 2 and r["company_key"]],
        "titles": sorted({(r["title"] or "").strip() for r in rows if r["title"]})[:40],
        "examples": [{"company": r["company"], "title": r["title"] or "", "reason": r["reason"] or ""}
                     for r in rows[:60]],
    }

def _prior_skips(conn, company_key, day):
    if not company_key:
        return 0
    return conn.execute(
        "SELECT COUNT(*) n FROM postings WHERE company_key=? AND verdict='SKIP' "
        "AND substr(COALESCE(decided_at,''),1,10)<?", (company_key, day)).fetchone()["n"]


def drop_detail(day):
    """What discover.py already filtered, with reasons (the other side of the ledger)."""
    doc = _read_json(S.path("dropped-%s.json" % day), {}) or {}
    return {"dropped": doc.get("dropped") or {}, "detail": (doc.get("detail") or [])[:25]}


def apply_outcomes(day, conn):
    """Per-application outcomes with the blocking question, so failures can be classified."""
    queue = {q["key"]: q for q in ((_read_json(S.path("apply-queue.json"), {}) or {}).get("queue") or [])}
    items = []
    for name in sorted(os.listdir(S.path("evidence")) if os.path.isdir(S.path("evidence")) else []):
        if not name.endswith(".json"):
            continue
        p = S.path("evidence", name)
        try:
            if time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(p))) != day:
                continue
        except OSError:
            continue
        ev = _read_json(p, {}) or {}
        q = queue.get(ev.get("key")) or {}
        items.append({
            "key": ev.get("key"), "company": ev.get("company"), "role": ev.get("role"),
            "ats": q.get("ats"), "variant": q.get("variant"),
            "status": ev.get("status"), "final_url": ev.get("final_url"),
            "blocking_question": (ev.get("blocking_question") or "")[:300],
            "note": (ev.get("note") or "")[:300],
        })
    counts = {}
    for i in items:
        counts[i["status"]] = counts.get(i["status"], 0) + 1
    return {"counts": counts, "items": items[:60]}


def verify_snapshot(day):
    doc = _read_json(S.path("verification-%s.json" % day), {}) or {}
    items = doc.get("items") or []
    return {"count": len(items),
            "confirmed": sum(1 for i in items if i.get("confirmed")),
            "unverified": [i.get("key") for i in items if i.get("status") == "unverified"][:20]}


def trace_snapshot(day, conn):
    doc = _read_json(S.path("trace-results-%s.json" % day), {}) or {}
    runs = doc.get("runs") or []
    return {"runs": len(runs), "status": {r.get("status"): 0 for r in runs} if False else
            _counts(r.get("status") for r in runs),
            "errors": [{"company": r.get("company"), "error": (r.get("error") or r.get("stop_reason") or "")[:160]}
                       for r in runs if r.get("status") in ("failed", "killed", "budget")][:10]}


def _counts(seq):
    out = {}
    for s in seq:
        out[s] = out.get(s, 0) + 1
    return out


def captcha_snapshot(day):
    doc = _read_json(S.path("captcha-solves.json"), {}) or {}
    used = doc.get(day) if isinstance(doc.get(day), (int, list)) else doc.get(day)
    return {"today": used if isinstance(used, int) else (len(used) if isinstance(used, list) else None)}


def evidence_hygiene(day):
    """Files the retro should flag for hygiene: plaintext credentials and oversized screenshots."""
    out = {"plaintext_secrets": [], "evidence_missing_png": []}
    d = S.path("evidence")
    if not os.path.isdir(d):
        return out
    for name in sorted(os.listdir(d)):
        if not name.endswith(".json"):
            continue
        p = os.path.join(d, name)
        try:
            if time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(p))) != day:
                continue
        except OSError:
            continue
        ev = _read_json(p, {}) or {}
        if ev.get("account_password"):
            out["plaintext_secrets"].append(ev.get("key") or name[:-5])
        if ev.get("status") in ("submitted", "escalated") and not os.path.exists(p[:-5] + ".png"):
            out["evidence_missing_png"].append(ev.get("key") or name[:-5])
    return out


# ---------------------------------------------------------------------------
# history: the week/month view the agent needs to optimise, not just today
# ---------------------------------------------------------------------------

def history_days(conn, days=30):
    rows = []
    for r in conn.execute("SELECT * FROM retro_runs ORDER BY date DESC LIMIT ?", (days,)).fetchall():
        d = dict(r)
        m = _read_json_text(d.get("metrics"))
        rows.append({"date": d["date"], "status": d.get("status"),
                     "agent_cost": d.get("agent_cost"), "actions": d.get("actions") or 0,
                     "findings": d.get("findings") or 0,
                     "kpi": {k: (m or {}).get("kpi", {}).get(k) for k in KPI_DIRECTION}})
    return rows


def _read_json_text(text):
    if not text:
        return {}
    try:
        return json.loads(text)
    except ValueError:
        return {}


def open_actions(conn):
    """Actions from earlier days whose claimed effect is due to be graded."""
    return S.retro_actions(conn, "status='applied' AND day < ?", (S.TODAY,))[:20]


def recent_promotions(conn, days=30):
    """Repeated findings promoted to 'latest' by the agent across days: candidates for a
    permanent rule. Anything here has already survived at least one repetition."""
    out = {}
    cutoff = (datetime.strptime(S.TODAY, "%Y-%m-%d") - timedelta(days=days)).strftime("%Y-%m-%d")
    for a in S.retro_actions(conn, "day >= ?", (cutoff,)):
        key = (a.get("kind"), a.get("target"))
        out[key] = out.get(key, 0) + 1
    return [{"kind": k[0], "target": k[1], "times": n} for k, n in
            sorted(out.items(), key=lambda kv: -kv[1])[:15]]


# ---------------------------------------------------------------------------
# KPIs
# ---------------------------------------------------------------------------

def _cost_split(stages):
    """Billed vs estimated cost. A provider that reports $0 (muskapi2) is estimated from
    FALLBACK_RATES; keeping the two apart stops a pricing gap from looking like an optimisation."""
    billed = est = 0.0
    for s in stages.values():
        billed += s.get("cost_billed", 0.0)
        est += s.get("cost_est", 0.0)
    return round(billed, 4), round(est, 4)


def kpi_block(f, usage, log, durations):
    """Pipeline KPIs. The retro stage is excluded on purpose: it is the reviewer, not the work
    being reviewed, and counting its own cost would make every review look like a regression."""
    stages = {k: v for k, v in (usage.get("stages") or {}).items() if k != "retro"}
    qual = stages.get("qualify") or {}
    appl = stages.get("apply") or {}
    billed, estimated = _cost_split(stages)
    total_cost = round(billed + estimated, 4)
    total_in = sum(s.get("in", 0) for s in stages.values())
    total_out = sum(s.get("out", 0) for s in stages.values())
    tool_calls = sum(s.get("tool_calls", 0) for s in stages.values())
    apps = f["applications"] or {}
    attempted = sum(v for k, v in apps.items() if k in ("submitted", "escalated", "failed", "needs_code"))
    decided = f["decided"] or 0
    verdicted = sum(v for k, v in f["verdicts"].items() if k in ("APPLY", "SKIP", "ESCALATE"))
    kpi = {
        "cost_total_usd": total_cost,
        "cost_billed_usd": billed,
        "cost_estimated_usd": estimated,
        "cost_qualify_usd": qual.get("cost_billed", 0) + qual.get("cost_est", 0),
        "cost_apply_usd": appl.get("cost_billed", 0) + appl.get("cost_est", 0),
        "tokens_in": total_in,
        "tokens_out": total_out,
        "cache_hit_pct": _ratio(sum(s.get("cached", 0) for s in stages.values()), total_in),
        "tool_calls": tool_calls,
        "cost_per_returned_usd": round(total_cost / f["returned"], 4) if f["returned"] else None,
        "cost_per_decided_usd": round(total_cost / decided, 4) if decided else None,
        "cost_per_application_usd": round(total_cost / apps["submitted"], 4) if apps.get("submitted") else None,
        "qualify_skip_pct": _ratio(f["verdicts"].get("SKIP", 0), decided),
        "qualify_unprocessed": max(0, f["surfaced"] - verdicted),
        "apply_failed_pct": _ratio(apps.get("failed", 0), attempted),
        "apply_escalated_pct": _ratio(apps.get("escalated", 0), attempted),
        "apply_submitted_pct": _ratio(apps.get("submitted", 0), attempted),
        "verify_receipt_pct": _ratio(f["receipts_today"], max(1, f["submitted_today"])),
        "trace_success_pct": _ratio(f["trace_status"].get("done", 0), max(1, f["trace_runs"])),
        "tool_calls_per_candidate": round(tool_calls / f["surfaced"], 2) if f["surfaced"] else None,
        "prompt_tokens_per_candidate": round(total_in / f["surfaced"]) if f["surfaced"] else None,
        # omp prunes old session files, so a day without them has no cost data at all. Trends and
        # grading must skip those days instead of reading a $0 that never happened.
        "cost_data_complete": bool(usage.get("files")),
        # Reported separately so the reviewer's own bill is visible without distorting the KPI.
        "retro_cost_usd": round(sum(s.get("cost_billed", 0) + s.get("cost_est", 0)
                                    for k, s in (usage.get("stages") or {}).items() if k == "retro"), 4),
        "stage_seconds": durations,
        "log_lines": log.get("lines"),
    }
    return kpi


# ---------------------------------------------------------------------------
# deterministic findings
# ---------------------------------------------------------------------------

def findings(f, kpi, usage, log, apply_out, verify, trace, skip, hygiene, durations,
             overrides):
    """Findings Python can prove. The agent adds judgement on top; anything here is a fact."""
    out = []

    def add(sev, code, what, why, evidence):
        out.append({"sev": sev, "code": code, "what": what, "why": why, "evidence": evidence,
                    "kind": "python"})

    if not log["exists"]:
        add("high", "no_log", "No run log for %s" % S.TODAY,
            "daily.py appends one log per day; its absence means the run never started.",
            log["path"])
    for m in log["marks"]:
        sev = "high" if re.search(r"pipeline stopped|exited [1-9]|traceback", m["text"], re.I) else "med"
        add(sev, "log_mark", "Log line %s: %s" % (m["line"], m["text"][:140]),
            "Stage or child process reported a problem.", m["text"])

    if kpi["qualify_unprocessed"]:
        add("high", "qualify_unprocessed",
            "%d candidate(s) surfaced but never verdicted" % kpi["qualify_unprocessed"],
            "Workers were cancelled before writing a verdict part; the posting stays eligible but "
            "today's qualification was incomplete and tomorrow re-pays for it.",
            {"surfaced": f["surfaced"], "decided": f["decided"]})
    if (kpi["qualify_skip_pct"] or 0) >= 60:
        add("high", "skip_ratio",
            "LLM skipped %.1f%% of decided postings" % kpi["qualify_skip_pct"],
            "Every SKIP that a deterministic rule could have produced was paid for at qualify rates. "
            "Shift the repeated categories into discover.py.",
            skip["by_reason"])
    if f["queued"] and (kpi["apply_escalated_pct"] or 0) >= 25:
        add("med", "escalation_rate",
            "%.1f%% of queued applications escalated" % kpi["apply_escalated_pct"],
            "Escalations consume a browser session and still need a human; repeated blockers belong in "
            "the standing answers file or a skip rule.",
            apply_out["counts"])
    if f["queued"] and (kpi["apply_failed_pct"] or 0) >= 10:
        add("high", "apply_failed",
            "%.1f%% of queued applications failed" % kpi["apply_failed_pct"],
            "A failure means a form was driven and no submission happened: wasted tokens.",
            [i for i in apply_out["items"] if i.get("status") == "failed"][:6])
    for i in apply_out["items"]:
        b = (i.get("blocking_question") or "")
        if b.upper().startswith("CAPTCHA"):
            add("med", "captcha_block", "%s blocked by %s" % (i.get("key"), b[:80]),
                "Token CAPTCHAs should be solved; image/slider challenges are a known gap.",
                i)
    if verify["unverified"]:
        add("low", "unverified", "%d submission(s) without proof" % len(verify["unverified"]),
            "No receipt and no confirmation text; verify.py will re-check, but the apply agent "
            "should have captured proof.", verify["unverified"])
    if trace["runs"] and (kpi["trace_success_pct"] or 0) < 70:
        add("med", "trace_failures",
            "Only %.1f%% of trace runs completed" % (kpi["trace_success_pct"] or 0),
            "Deterministic trace failures cost nothing to classify: a broken scraper is a code fix, "
            "a missing contact is expected.", trace["errors"])
    for k, v in (f["applications"] or {}).items():
        if k in ("failed", "needs_code"):
            add("high", "status_" + k, "%d application(s) in status %s" % (v, k),
                "Terminal-but-unsuccessful statuses are the expensive failures.", f["applications"])
    if hygiene["plaintext_secrets"]:
        add("med", "plaintext_secret",
            "Plaintext account password stored in evidence: %s" % ", ".join(hygiene["plaintext_secrets"][:5]),
            "Evidence JSON is a proof artefact, not a credential store.", hygiene["plaintext_secrets"])
    if hygiene["evidence_missing_png"]:
        add("low", "missing_screenshot",
            "%d evidence JSON(s) without a screenshot" % len(hygiene["evidence_missing_png"]),
            "apply.md requires a PNG for submitted/escalated results.",
            hygiene["evidence_missing_png"][:8])
    slow = {k: v for k, v in durations.items() if v and v > 3600}
    if slow:
        add("med", "slow_stage", "Stage(s) over an hour: %s" % ", ".join("%s %dm" % (k, v // 60)
                                                                       for k, v in slow.items()),
            "A stage that long is either provider congestion or a loop; both are worth a look.",
            slow)
    repeats = skip["companies_skipped_twice_plus"]
    if repeats:
        add("med", "repeated_skips",
            "%d company(ies) skipped 2+ times" % len(repeats),
            "Repeated skips are only a filtering opportunity when the reason is structural "
            "(staffing agency, wrong discipline, wrong metro). Read the names and reasons: a "
            "well-known employer that keeps posting unsuitable roles must NOT be blocklisted.",
            repeats[:10])
    return out


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def collect(day=None):
    """Build the evidence packet for one day. Never raises on missing artefacts."""
    day = day or S.TODAY
    conn = S.db()
    try:
        f = funnel(day, conn)
        skip = skip_reasons(day, conn)
        usage = session_usage(day)
        log = log_scan(day)
        durations = stage_durations(log)
        apply_out = apply_outcomes(day, conn)
        verify = verify_snapshot(day)
        trace = trace_snapshot(day, conn)
        hygiene = evidence_hygiene(day)
        overrides = _read_json(S.path("discover-overrides.json"), {}) or {}
        kpi = kpi_block(f, usage, log, durations)
        found = findings(f, kpi, usage, log, apply_out, verify, trace, skip, hygiene,
                         durations, {"source": "file" if overrides else "none"})
        return {
            "generated": S.now(),
            "day": day,
            "funnel": f,
            "kpi": kpi,
            "skip": skip,
            "discover": drop_detail(day),
            "apply": apply_out,
            "verify": verify,
            "trace": trace,
            "hygiene": hygiene,
            "log": {"exists": log["exists"], "lines": log["lines"], "stages": log["stages"],
                    "first": log["first"], "last": log["last"]},
            "usage": {"stages": usage["stages"], "top_workers": usage["top_workers"],
                      "files": usage["files"]},
            "findings": found,
            "kpi_directions": KPI_DIRECTION,
            "history": history_days(conn, 30),
            "open_actions": open_actions(conn),
            "repeated": recent_promotions(conn),
        }
    finally:
        conn.close()


def collect_and_write(day=None):
    """collect(), plus the one-page brief the agent actually reads: state/retro-<day>-brief.md."""
    packet = collect(day)
    S.write_json(S.path("retro-%s-evidence.json" % packet["day"]), packet)
    open(S.path("retro-%s-brief.md" % packet["day"]), "w", encoding="utf-8").write(
        render_markdown(packet))
    return packet

def _d(v):
    """Percent or em dash."""
    return "—" if v is None else ("%.1f%%" % v)


def render_markdown(packet, report=None):
    """One-page Markdown brief of the packet.

    This is what the agent reads instead of the JSON: paging the raw packet cost 798 KB of
    input for a 41 KB file on 2026-11-04, because every slice re-sent the whole conversation.
    A brief that fits in one read is the cheapest possible form of the same evidence.
    """
    k, f, u = packet["kpi"], packet["funnel"], packet["usage"]
    L = ["# Retro %s — evidence brief" % packet["day"], ""]
    L.append("## Funnel")
    L.append("returned %d → surfaced %d → decided %d → queued %d → submitted %d · receipts %d"
             % (f["returned"], f["surfaced"], f["decided"], f["queued"], f["submitted_today"],
                f["receipts_today"]))
    L.append("")
    L.append("## Cost and quality")
    L.append("| metric | value |")
    L.append("|---|---|")
    for key, label in (("cost_total_usd", "total $"), ("cost_billed_usd", "billed $"),
                       ("cost_estimated_usd", "estimated $"), ("cost_qualify_usd", "qualify $"),
                       ("cost_apply_usd", "apply $"), ("cost_per_decided_usd", "$/decided"),
                       ("cost_per_application_usd", "$/submitted"), ("tokens_in", "tokens in"),
                       ("tokens_out", "tokens out"), ("cache_hit_pct", "cache hit"),
                       ("tool_calls", "tool calls"),
                       ("tool_calls_per_candidate", "tool calls/candidate"),
                       ("retro_cost_usd", "retro own $"),
                       ("qualify_skip_pct", "LLM skip rate"),
                       ("qualify_unprocessed", "unverdicted"),
                       ("apply_submitted_pct", "submitted of attempted"),
                       ("apply_escalated_pct", "escalated of attempted"),
                       ("apply_failed_pct", "failed of attempted"),
                       ("verify_receipt_pct", "receipts/submission"),
                       ("trace_success_pct", "trace success")):
        v = k.get(key)
        if key.endswith("_pct"):
            shown = _d(v)
        elif key.endswith("_usd") and v is not None:
            shown = "%.2f" % v
        elif key in ("tokens_in", "tokens_out", "tool_calls") and v is not None:
            shown = "{:,}".format(v)
        else:
            shown = "—" if v is None else v
        L.append("| %s | %s |" % (label, shown))
    L.append("")
    if k.get("stage_seconds"):
        L.append("**Stage seconds:** " + " · ".join("%s %d" % (s, n)
                                                    for s, n in k["stage_seconds"].items()))
        L.append("")
    L.append("## Per-stage agents")
    L.append("| stage | $ billed | $ est | in | out | tool calls | workers |")
    L.append("|---|---|---|---|---|---|---|")
    for name, s in sorted(u.get("stages", {}).items()):
        L.append("| %s | %.2f | %.2f | %s | %s | %d | %d |"
                 % (name, s.get("cost_billed", 0), s.get("cost_est", 0),
                    "{:,}".format(s.get("in", 0)), "{:,}".format(s.get("out", 0)),
                    s.get("tool_calls", 0), s.get("workers", 0)))
    if u.get("top_workers"):
        L.append("")
        L.append("**Most expensive workers:** " + " · ".join(
            "%s %s" % (w["name"], "{:,}".format(w["in"] + w["out"]))
            for w in u["top_workers"][:5]))
    L.append("")
    L.append("## Findings (%d)" % len(packet["findings"]))
    for x in packet["findings"]:
        L.append("- **[%s]** `%s` %s" % (x["sev"], x["code"], x["what"]))
        L.append("  - why: %s" % x["why"])
    L.append("")
    sk = packet["skip"]
    L.append("## Skip reasons (deterministic-filter candidates)")
    L.append("categories: " + ", ".join("%s %d" % (a, b) for a, b in
                                        sorted(sk["by_reason"].items(), key=lambda kv: -kv[1])))
    if sk["companies_skipped_twice_plus"]:
        L.append("")
        L.append("companies skipped 2+ times (company | this day | prior):")
        for c in sk["companies_skipped_twice_plus"][:20]:
            L.append("- %s (%d + %d)" % (c["company"], c["skips"], c["prior_skips"]))
    if sk.get("examples"):
        L.append("")
        L.append("skipped postings, one line each (title — first 90 chars of the reason):")
        for e in sk["examples"]:
            L.append("- %s — %s" % (e["title"][:80], (e["reason"] or "")[:90]))
    L.append("")
    L.append("## Apply outcomes")
    L.append("counts: " + ", ".join("%s %d" % (a, b) for a, b in packet["apply"]["counts"].items()))
    for i in packet["apply"]["items"]:
        if i.get("status") != "submitted":
            L.append("- %s · %s · %s · %s" % (i.get("key"), i.get("ats") or "?",
                                              i.get("status"), (i.get("blocking_question") or "")[:160]))
    L.append("")
    L.append("## Trace runs")
    L.append("runs %d · " % packet["trace"]["runs"] +
             ", ".join("%s %d" % (a, b) for a, b in packet["trace"]["status"].items()))
    for e in packet["trace"]["errors"]:
        L.append("- %s — %s" % (e.get("company"), e.get("error")))
    if u.get("top_workers"):
        L.append("")
        L.append("## Worker cost detail (call volume is the loop signal)")
        for w in u["top_workers"][:6]:
            L.append("- %s: in %s · out %s · %d tool calls · $%.2f"
                     % (w["name"], "{:,}".format(w["in"]), "{:,}".format(w["out"]),
                        w.get("tool_calls", 0), w.get("cost_billed", 0) + w.get("cost_est", 0)))
    L.append("")
    L.append("## Discover drops")
    L.append(", ".join("%s %d" % (a, b) for a, b in packet["discover"]["dropped"].items()))
    L.append("")
    L.append("## History (recent retro runs)")
    if packet["history"]:
        L.append("| day | status | $ | skip% | submitted% | findings | actions |")
        L.append("|---|---|---|---|---|---|---|")
        for h in packet["history"][:14]:
            kp = h.get("kpi") or {}
            L.append("| %s | %s | %s | %s | %s | %d | %d |"
                     % (h["date"], h.get("status") or "",
                        "n/a" if kp.get("cost_data_complete") is False else
                        ("%.2f" % (kp.get("cost_total_usd") or 0)),
                        _d(kp.get("qualify_skip_pct")), _d(kp.get("apply_submitted_pct")),
                        h.get("findings") or 0, h.get("actions") or 0))
    else:
        L.append("no earlier retro runs recorded")
    if packet["open_actions"]:
        L.append("")
        L.append("## Changes awaiting grading")
        for a in packet["open_actions"]:
            L.append("- #%d %s %s — %s (expects %s %s from %.3f)"
                     % (a["id"], a["day"], a["target"], a["summary"], a.get("metric"),
                        a.get("direction"), a.get("baseline") or 0))
    if packet["repeated"]:
        L.append("")
        L.append("## Repeatedly touched targets")
        L.append(", ".join("%s:%s ×%d" % (r["kind"], r["target"], r["times"]) for r in packet["repeated"]))
    if report:
        L += ["", report]
    return "\n".join(L)
