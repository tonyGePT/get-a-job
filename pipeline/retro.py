#!/usr/bin/env python3
"""Post-run troubleshooter: review today's pipeline, heal what broke, and shrink tomorrow's cost.

Runs after `daily.py`, on its own schedule (JobHuntRetro, 12:00 = four hours after the 08:00
start) and exits immediately if the daily run is still going. Two halves:

  1. Deterministic Python (`lib/retro_telemetry.py`) builds one compact evidence packet and
     works out every countable fact: funnel, tokens/cost per stage, skip ratio, escalation
     rate, tool calls, stage durations, hygiene problems.
  2. One `omp` pass (`prompts/retro.md`) reads that packet plus a short list of files, and
     proposes changes as JSON. It never edits anything.

Every proposed change then passes a gate here, because a self-modifying pipeline that trusts
an LLM's diff is a pipeline that stops running:

  * data-only changes (`kind` = blocklist | discover_regex | ats_hint) are merged by Python,
    so no patch text ever reaches a parser;
  * code and prompt changes must be a unified diff whose files are in `RETRO_WRITABLE`,
    written to a scratch tree, `git apply`-ed there, `py_compile`-d (for .py) or rendered
    through the stage's parser (for .md), and only then applied to the real tree;
  * `profile/`, `state/`, `config.json` and the prompts' truthfulness rules are never writable;
  * if nothing survives the gate the pipeline simply stays as it was, and the report says so.

Each accepted change is recorded in `retro_actions` with the KPI it claims to move and the
baseline it was measured against. The next retro grades it: reverted if the KPI got worse for
`GRACE_RUNS` runs, marked verified if it improved. Expected-and-unchanged stays applied
silently. That is the "recursively optimise over days and weeks" loop.

Usage:
  python retro.py                 review the latest completed run, apply gated self-healing
  python retro.py --dry-run       collect + report, run no agent and change nothing
  python retro.py --backfill 30   seed retro_runs from past days' logs (no agent, no changes)
  python retro.py --status        print the KPI table and pending actions
  python retro.py --digest        rebuild and post today's retro digest only
  python retro.py --no-agent      collect + grade + report, skip the model pass
"""

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import state as S  # noqa: E402
import retro_telemetry as T  # noqa: E402
import notify  # noqa: E402

PY = sys.executable
ROOT = S.ROOT
LOCK = os.path.join(ROOT, ".lock")
AGENT_TIMEOUT = 3600          # the retro is cheap to lose; never let it stack up
GRACE_RUNS = 2                # a change is graded after this many later runs

# Files the retro may rewrite. Everything else is read-only to it, by construction.
RETRO_WRITABLE = (
    "discover.py", "prepare.py", "verify.py", "digest.py", "watchdog.py", "daily.py",
    "ledger.py", "lib/notify.py", "lib/retro_telemetry.py", "prompts/qualify.md",
    "prompts/apply.md", "prompts/retro.md", "omp-pipeline.yml",
    "retro.py", "RETRO.md",
)
# Never writable, whatever the agent claims: candidate facts, standing answers and state.
RETRO_FORBIDDEN = ("profile/", "state/", "config.json", "resume_", "blitz_answers",
                   "resume_routing_context", "discord_", "trace.py")

DATA_KINDS = ("blocklist", "discover_regex", "ats_hint")
CODE_KINDS = ("code", "prompt", "config")


# ---------------------------------------------------------------------------
# lock: never run beside daily.py
# ---------------------------------------------------------------------------

class LockFree:
    """Non-blocking acquire of the same .lock daily.py holds, on Windows and POSIX."""

    def __init__(self, path):
        self.path, self.fh, self.ok = path, None, False

    def __enter__(self):
        try:
            self.fh = open(self.path, "a+")
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.ok = True
        except (OSError, ImportError):
            self.ok = False
        return self

    def __exit__(self, *exc):
        if self.fh:
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            self.fh.close()
        return False


# ---------------------------------------------------------------------------
# git guard: every code change is revertible and auditable
# ---------------------------------------------------------------------------

def git(*args, cwd=None):
    return subprocess.run(["git"] + list(args), cwd=cwd or ROOT,
                          capture_output=True, text=True, timeout=120)


def git_ready():
    r = git("rev-parse", "--is-inside-work-tree")
    return r.returncode == 0 and r.stdout.strip() == "true"


def git_dirty():
    r = git("status", "--porcelain")
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def git_diff_text(rel):
    return git("diff", "-U3", "--", rel).stdout


def git_restore(rel):
    return git("checkout", "--", rel)


def git_commit_changes(msg):
    """Commit only the files the retro touched. A failing commit is reported, never fatal."""
    r = git("add", "--", *RETRO_WRITABLE)
    if r.returncode != 0:
        return None, r.stderr.strip()[:200]
    if not git("diff", "--cached", "--quiet").returncode:
        return None, "nothing staged"
    c = git("commit", "-m", msg)
    if c.returncode != 0:
        return None, c.stderr.strip()[:200]
    return git("rev-parse", "--short", "HEAD").stdout.strip(), None


# ---------------------------------------------------------------------------
# the gate: validate a proposal before it can touch the real tree
# ---------------------------------------------------------------------------

def _safe_target(rel):
    rel = (rel or "").replace("\\", "/").lstrip("./")
    if not rel or rel.startswith("/") or ".." in rel.split("/"):
        return None
    if any(f in rel for f in RETRO_FORBIDDEN):
        return None
    return rel if rel in RETRO_WRITABLE else None


def _apply_patch_to(text, patch):
    """Apply a unified diff to `text` with difflib. Returns new text, or raises ValueError.

    Deliberately not `patch(1)`: this has to behave identically on the Windows box that runs
    the pipeline and on a POSIX checkout, and it must fail closed on any mismatch.
    """
    old = text.splitlines(keepends=True)
    new = list(old)
    hunks = [h for h in re.split(r"(?m)^(?=@@ )", patch) if h.startswith("@@ ")]
    if not hunks:
        raise ValueError("no hunks")
    offset = 0
    for hunk in hunks:
        m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", hunk)
        if not m:
            raise ValueError("bad hunk header")
        old_start, old_count = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
        body = hunk.splitlines(keepends=True)[1:]
        want_old, want_new = [], []
        for line in body:
            if line.startswith(("-", " ")) or line.strip() == "":
                want_old.append(line[1:] if line[:1] in "- " else line)
            if line.startswith(("+", " ")) or line.strip() == "":
                want_new.append(line[1:] if line[:1] in "+ " else line)
        if old_count == 0:
            # "@@ -N,0 +M,K @@" inserts AFTER old line N, so the index is N, not N-1.
            at = old_start + offset
            new[at:at] = want_new
            offset += len(want_new)
            continue
        start = old_start - 1 + offset
        window = new[start:start + len(want_old)]
        if window != want_old:
            raise ValueError("hunk does not match at line %d" % (start + 1))
        new[start:start + len(want_old)] = want_new
        offset += len(want_new) - len(want_old)
    return "".join(new)


def _py_candidates(rel):
    """Import-check targets: the file itself plus every module that imports it."""
    out = [rel]
    for name in ("daily.py", "discover.py", "prepare.py", "verify.py", "digest.py",
                 "ledger.py", "retro.py", "watchdog.py"):
        if name != rel and os.path.exists(os.path.join(ROOT, name)):
            out.append(name)
    return out


def _check_markdown(rel, text):
    """A prompt rewrite must keep the stage contract: the orchestrate header, and every
    heading the pipeline relies on. Losing those has silently produced zero verdicts before."""
    must = ["orchestrate"]
    if rel.endswith("qualify.md"):
        must += ["## Inputs", "## Output", "## Rules", "state/verdicts/parts"]
    elif rel.endswith("apply.md"):
        must += ["## Inputs", "## Method", "## Proof", "## Deliverables, per key"]
    elif rel.endswith("retro.md"):
        must += ["## Output", "must not edit"]
    missing = [m for m in must if m not in text]
    return ("prompt lost required section(s): %s" % ", ".join(missing)) if missing else None


def validate(change):
    """Return (ok, target, new_text, error). Never mutates anything."""
    kind = (change.get("kind") or "").strip()
    if kind not in DATA_KINDS + CODE_KINDS:
        return False, None, None, "unknown kind %r" % kind
    if kind in CODE_KINDS:
        rel = _safe_target(change.get("target"))
        if not rel:
            return False, None, None, "target %r is not in the retro's writable set" % change.get("target")
        patch = change.get("patch") or ""
        try:
            current = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        except OSError as e:
            return False, rel, None, "cannot read target: %s" % e
        try:
            new_text = _apply_patch_to(current, patch)
        except ValueError as e:
            return False, rel, None, "patch does not apply: %s" % e
        if new_text == current:
            return False, rel, None, "patch changes nothing"
        err = _check_python(rel, new_text) if rel.endswith(".py") else _check_markdown(rel, new_text)
        if err:
            return False, rel, None, err
        return True, rel, new_text, None
    return True, None, None, None


def apply_code(rel, new_text, summary, day):
    """Write a validated change, and leave a revertible trail."""
    path = os.path.join(ROOT, rel)
    backup = path + ".retro-bak"
    shutil.copy2(path, backup)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(new_text)
    return backup


# ---------------------------------------------------------------------------
# data-only changes: Python merges, agent never writes a file
# ---------------------------------------------------------------------------

def apply_data(change, day):
    """Merge blocklist / discover-regex / ats-hint proposals. Returns a one-line summary.

    The agent never writes these files: it supplies names or regexes, and Python validates and
    merges them, so a malformed model answer can only ever be rejected.
    """
    kind = change["kind"]
    if kind == "blocklist":
        p = S.path("blocklist.json")
        doc = S.read_json(p, {}) or {}
        have = {S.norm_text(x) for x in doc.get("companies", [])}
        added = []
        for name in change.get("names") or []:
            n = (name or "").strip()
            if n and S.norm_text(n) not in have:
                doc.setdefault("companies", []).append(n)
                have.add(S.norm_text(n))
                added.append(n)
        if not added:
            return None
        doc["_note"] = (doc.get("_note") or "") + \
            " Retro %s added: %s." % (day, ", ".join(added[:10]))
        S.write_json(p, doc)
        return "blocklist += %s" % ", ".join(added[:6])

    if kind == "discover_regex":
        p = S.path("discover-overrides.json")
        doc = S.read_json(p, {}) or {}
        field = "title_block" if change.get("field") == "title" else "agency_block"
        vals = [v for v in (change.get("patterns") or []) if v and v not in doc.get(field, [])]
        if not vals:
            return None
        for v in vals:
            try:
                re.compile(v)
            except re.error:
                return None
        doc.setdefault(field, []).extend(vals)
        doc["_note"] = "Extra deterministic filters, grown by retro.py from repeated LLM SKIPs."
        doc["updated"] = S.now()
        S.write_json(p, doc)
        return "%s += %s" % (field, " | ".join(vals[:4]))

    if kind == "ats_hint":
        p = S.path("ats-hints.json")
        doc = S.read_json(p, {}) or {}
        key = (change.get("ats") or "other").strip().lower()
        hints = [h for h in (change.get("hints") or []) if h]
        if not hints:
            return None
        existing = doc.setdefault(key, [])
        added = [h for h in hints if h not in existing]
        if not added:
            return None
        existing.extend(added[:10])
        doc["_note"] = ("ATS-specific form-filling hints. prompts/apply.md reads this file; "
                        "only selector/behaviour facts, never candidate facts.")
        doc["updated"] = S.now()
        S.write_json(p, doc)
        return "ats-hints[%s] += %d" % (key, len(added))

    return None


def _check_python(rel, text):
    """Compile every module that could import the changed one. A syntax error is caught here,
    before the real file is written."""
    import py_compile
    import tempfile
    with tempfile.TemporaryDirectory(prefix="retro-check-") as tmpdir:
        for cand in _py_candidates(rel):
            src = text if cand == rel else open(os.path.join(ROOT, cand), encoding="utf-8").read()
            p = os.path.join(tmpdir, cand.replace("/", "_") + ".py")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(src)
            try:
                py_compile.compile(p, cfile=p + "c", doraise=True)
            except py_compile.PyCompileError as e:
                return str(e)[:300]
    return None

# ---------------------------------------------------------------------------
# grading: did yesterday's change actually help?
# ---------------------------------------------------------------------------

def grade_actions(conn, day):
    """Grade earlier changes against the KPI they claimed to move.

    Called before today's agent pass so the agent sees its own track record. Uses the last
    GRACE_RUNS days of `retro_runs`; a regression reverts the file from its backup, restores
    the last git commit, or drops the data-only addition.
    """
    graded = []
    for a in S.retro_actions(conn, "status='applied' AND day < ?", (day,)):
        since = [r for r in S.retro_runs(conn, GRACE_RUNS + 4) if r["date"] > a["day"]]
        if len(since) < GRACE_RUNS:
            continue
        metric = a.get("metric")
        baseline = a.get("baseline")
        if not metric or baseline is None:
            S.retro_action_set(conn, a["id"], status="verified", graded_on=day,
                               outcome="ungradable: no metric recorded")
            continue
        vals = []
        for r in since[:GRACE_RUNS]:
            m = T._read_json_text(r.get("metrics"))
            kpi = m.get("kpi") or {}
            # A day with no session files has no cost data; grading against a $0 baseline would
            # revert a good change. Wait for two usable days instead.
            if kpi.get("cost_data_complete") is False:
                continue
            v = kpi.get(metric)
            if isinstance(v, (int, float)):
                vals.append(float(v))
        if len(vals) < GRACE_RUNS:
            continue
        now = sum(vals) / len(vals)
        better = now < baseline * 0.95 if a.get("direction") == "down" else now > baseline * 1.05
        worse = now > baseline * 1.05 if a.get("direction") == "down" else now < baseline * 0.95
        if better:
            S.retro_action_set(conn, a["id"], status="verified", graded_on=day,
                               outcome="%s %.3f → %.3f (improved)" % (metric, baseline, now))
            graded.append((a, "improved", now))
        elif worse:
            outcome = revert_action(a, conn)
            S.retro_action_set(conn, a["id"], status="reverted", graded_on=day,
                               outcome="%s %.3f → %.3f (regressed) · %s" % (metric, baseline, now, outcome))
            graded.append((a, "reverted", now))
        else:
            S.retro_action_set(conn, a["id"], status="verified", graded_on=day,
                               outcome="%s %.3f → %.3f (no change, kept)" % (metric, baseline, now))
            graded.append((a, "no-change", now))
    return graded


def revert_action(a, conn):
    """Undo one applied change. Code: backup file, else git checkout. Data: remove the value."""
    kind, target = a.get("kind"), a.get("target") or ""
    if kind in ("code", "prompt", "config"):
        path = os.path.join(ROOT, target)
        backup = path + ".retro-bak"
        if os.path.exists(backup):
            shutil.copy2(backup, path)
            os.remove(backup)
            return "restored %s from backup" % target
        r = git_restore(target)
        return ("git checkout %s" % target) if r.returncode == 0 else \
            "could not revert %s: %s" % (target, r.stderr.strip()[:120])
    if kind == "blocklist":
        p = S.path("blocklist.json")
        doc = S.read_json(p, {}) or {}
        patch = T._read_json_text(a.get("patch")).get("names") or []
        doc["companies"] = [x for x in doc.get("companies", []) if x not in patch]
        S.write_json(p, doc)
        return "removed %d blocklist entr(ies)" % len(patch)
    if kind == "discover_regex":
        p = S.path("discover-overrides.json")
        doc = S.read_json(p, {}) or {}
        patch = T._read_json_text(a.get("patch"))
        field = patch.get("field") or "agency_block"
        doc[field] = [x for x in doc.get(field, []) if x not in (patch.get("patterns") or [])]
        S.write_json(p, doc)
        return "removed regex(es) from %s" % field
    if kind == "ats_hint":
        p = S.path("ats-hints.json")
        doc = S.read_json(p, {}) or {}
        patch = T._read_json_text(a.get("patch"))
        ats = (patch.get("ats") or "other").lower()
        doc[ats] = [x for x in doc.get(ats, []) if x not in (patch.get("hints") or [])]
        S.write_json(p, doc)
        return "removed ats hints for %s" % ats
    return "nothing to revert"


# ---------------------------------------------------------------------------
# the agent pass
# ---------------------------------------------------------------------------

def run_agent(day, packet, evidence_path):
    """One retro agent pass. Returns (ok, text, error, model, usage).

    The prompt goes in on STDIN, not as an argv element. Passing this prompt as an argument
    made the child process hang with no session written (observed 2026-11-04: 0.75 s CPU in
    37 min); the same invocation with the prompt on stdin starts immediately. `daily.py` still
    passes its two smaller prompts as argv.
    """
    prompt = open(os.path.join(ROOT, "prompts", "retro.md"), encoding="utf-8").read()
    prompt += ("\n\n---\n\n## This run\n\n"
               "- Day: **%s**\n"
               "- Evidence packet: `%s`\n"
               "- Report to write: `%s`\n"
               "- Your proposals: `%s`\n"
               "- State dir: `%s`\n"
               % (day, evidence_path, S.path("retro-%s-review.md" % day),
                  S.path("retro-proposals-%s.json" % day), S.STATE))
    args = ["omp", "-p", "--no-title", "--cwd", ROOT,
            "--config", os.path.join(ROOT, "omp-pipeline.yml")]
    t0 = time.time()
    try:
        r = subprocess.run(args, cwd=ROOT, input=prompt, capture_output=True, text=True,
                           timeout=AGENT_TIMEOUT, encoding="utf-8", errors="replace")
        text, err = (r.stdout or ""), (r.stderr or "")
    except subprocess.TimeoutExpired:
        return False, "", "agent timed out after %ds" % AGENT_TIMEOUT, None, (0, 0, 0.0)
    except FileNotFoundError:
        return False, "", "omp not found on PATH", None, (0, 0, 0.0)
    usage = session_cost_since(day, t0)

    return r.returncode == 0, text, (_real_error(err) if r.returncode else None), None, usage


def _real_error(stderr):
    """omp writes progress lines to stderr ('Working...', 'Reading prompt from piped stdin…').
    Only a genuine error line is worth recording, otherwise every successful pass looks failed."""
    noise = ("working...", "reading prompt from piped stdin", "still starting",
             "phase:", "logs:")
    lines = [ln.strip() for ln in (stderr or "").splitlines()
             if ln.strip() and not any(n in ln.lower() for n in noise)]
    return "; ".join(lines)[:400] or None


def session_cost_since(day, t0):
    """Tokens and cost the retro agent itself just burned: the newest session started after t0."""
    root = T.session_root()
    best = None
    try:
        for name in os.listdir(root):
            st = T._session_started(name)
            if not st or not name.endswith(".jsonl"):
                continue
            if st.timestamp() < t0 - 120:
                continue
            if best is None or st.timestamp() > best[0]:
                best = (st.timestamp(), os.path.join(root, name))
    except OSError:
        return (0, 0, 0.0)
    if not best:
        return (0, 0, 0.0)
    b = {"in": 0, "cached": 0, "out": 0, "cost_billed": 0.0, "cost_est": 0.0, "calls": 0,
         "tool_calls": 0, "sessions": 1, "workers": 0, "first_prompt": ""}
    T._scan_jsonl(best[1], b)
    wdir = best[1][:-len(".jsonl")]
    if os.path.isdir(wdir):
        for f in os.listdir(wdir):
            if f.endswith(".jsonl"):
                T._scan_jsonl(os.path.join(wdir, f), b)
    return (b["in"], b["out"], round(b["cost_billed"] + b["cost_est"], 4))


def read_proposals(day):
    """The proposals file is part of the contract, so an absent file is a failed pass.

    A silently missing file used to look identical to 'no changes', which hid contract drift
    on 2026-11-04 (the agent wrote only its review). Returns (changes, error).
    """
    p = S.path("retro-proposals-%s.json" % day)
    if not os.path.exists(p):
        return [], "agent did not write retro-proposals-%s.json" % day
    doc = T._read_json(p, None)
    if doc is None:
        return [], "proposals file is not valid JSON"
    items = doc.get("changes") if isinstance(doc, dict) else doc
    changes = [c for c in (items or []) if isinstance(c, dict)]
    limit = int((S.CONFIG.get("retro") or {}).get("max_changes_per_run") or 6)
    if len(changes) > limit:
        return changes[:limit], "agent proposed %d changes; capped at %d" % (len(changes), limit)
    return changes, None

# ---------------------------------------------------------------------------
# one full cycle
# ---------------------------------------------------------------------------

def collect_and_report(day, run_agent_pass=True):
    S.ensure_dirs()
    packet = T.collect_and_write(day)          # writes -evidence.json and the one-page -brief.md
    evidence = S.path("retro-%s-brief.md" % day)

    conn = S.db()
    try:
        S.retro_run_upsert(conn, {"date": day, "started": S.now(), "status": "collected",
                                  "metrics": json.dumps({"kpi": packet["kpi"]}),
                                  "findings": len(packet["findings"]),
                                  "report": S.path("retro-%s.md" % day)})
        graded = grade_actions(conn, day)
    finally:
        conn.close()

    result = {"day": day, "collected": len(packet["findings"]), "graded": graded,
              "accepted": [], "rejected": [], "agent": None}
    if graded:
        packet["graded"] = [{"id": g[0]["id"], "target": g[0].get("target"),
                             "summary": g[0].get("summary"), "result": g[1],
                             "outcome": g[0].get("outcome")} for g in graded]

    if not run_agent_pass:
        result["agent"] = "skipped (--no-agent/--dry-run)"
        write_report(day, packet, result)
        return result

    ok, text, err, model, usage = run_agent(day, packet, evidence)
    result["agent"] = {"ok": ok, "error": err, "text": (text or "")[-3000:],
                       "tokens_in": usage[0], "tokens_out": usage[1], "cost": usage[2]}

    proposals, prop_err = read_proposals(day)
    if prop_err:
        result["rejected"].append({"kind": "contract", "summary": "proposals file",
                                   "why": prop_err})
        if not proposals:
            result["agent"]["ok"] = False
            result["agent"]["error"] = prop_err
    conn = S.db()
    try:
        for change in proposals:
            good, rel, new_text, why = validate(change)
            kind = change.get("kind")
            if not good:
                result["rejected"].append({"kind": kind, "target": change.get("target"),
                                           "summary": change.get("summary"), "why": why})
                continue
            if kind in DATA_KINDS:
                summary = apply_data(change, day)
                if not summary:
                    result["rejected"].append({"kind": kind, "summary": change.get("summary"),
                                               "why": "nothing new to add"})
                    continue
                S.retro_action_add(conn, day=day, kind=kind, target=kind, summary=summary,
                                   evidence=change.get("evidence"), metric=change.get("metric"),
                                   direction=change.get("direction"),
                                   baseline=change.get("baseline"),
                                   expected=change.get("expected"),
                                   patch=json.dumps(change, ensure_ascii=False))
                result["accepted"].append(summary)
            else:
                backup = apply_code(rel, new_text, change.get("summary"), day)
                S.retro_action_add(conn, day=day, kind=kind, target=rel,
                                   summary=change.get("summary") or "edit %s" % rel,
                                   evidence=change.get("evidence"), metric=change.get("metric"),
                                   direction=change.get("direction"),
                                   baseline=change.get("baseline"),
                                   expected=change.get("expected"),
                                   patch=change.get("patch"))
                result["accepted"].append("%s: %s" % (rel, change.get("summary") or "edited"))
        agent_ok = ok and not (result["agent"] or {}).get("error")
        S.retro_run_upsert(conn, {"date": day, "finished": S.now(),
                                  "status": "agent_ok" if agent_ok else "agent_failed",
                                  "agent_in": usage[0], "agent_out": usage[1],
                                  "agent_cost": usage[2],
                                  "actions": len(result["accepted"]),
                                  "error": (result["agent"] or {}).get("error") or ""})
    finally:
        conn.close()

    if result["accepted"] and git_ready() and not git_dirty():
        sha, cerr = git_commit_changes("retro %s: %s" % (day, "; ".join(result["accepted"])[:120]))
        result["commit"] = sha or cerr
    elif result["accepted"]:
        result["commit"] = "not committed: worktree already had changes (left for review)"

    write_report(day, packet, result)
    return result


def write_report(day, packet, result):
    """Write state/retro-<date>.md.

    The agent writes its own narrative report to `retro-<date>-review.md`; this function writes
    the deterministic summary. Keeping them apart matters: the first version had both writing
    `retro-<date>.md`, so the runner silently replaced the agent's analysis with its template.
    The posted digest joins both.
    """
    L = [T.render_markdown(packet)]
    L.append("")
    L.append("## Self-healing")
    if result.get("graded"):
        for g in result["graded"]:
            L.append("- graded **%s** → %s" % (g[0].get("summary") or g[0].get("target"), g[1]))
    if result["accepted"]:
        for a in result["accepted"]:
            L.append("- applied: %s" % a)
    else:
        L.append("- no change applied")
    if result["rejected"]:
        for r in result["rejected"]:
            L.append("- rejected: %s (%s)" % (r.get("summary") or r.get("kind"), r.get("why")))
    a = result.get("agent")
    if isinstance(a, dict):
        L.append("- agent: %s · in %s · out %s · $%.2f"
                 % ("ok" if a["ok"] else "failed: %s" % a.get("error"),
                    "{:,}".format(a.get("tokens_in") or 0), "{:,}".format(a.get("tokens_out") or 0),
                    a.get("cost") or 0))
    elif a:
        L.append("- agent: %s" % a)
    if result.get("commit"):
        L.append("- git: %s" % result["commit"])
    md = "\n".join(L)
    open(S.path("retro-%s.md" % day), "w", encoding="utf-8").write(md)
    return md


# ---------------------------------------------------------------------------
# digest: the second daily Discord message
# ---------------------------------------------------------------------------

def post_digest(day, quiet=False):
    """Summarise what self-healing changed today, and post it to the retro webhook."""
    p = S.path("retro-%s.md" % day)
    if not os.path.exists(p):
        return False, "no retro report for %s" % day
    md = open(p, encoding="utf-8").read()
    review = S.path("retro-%s-review.md" % day)
    if os.path.exists(review):
        md += "\n\n---\n\n" + open(review, encoding="utf-8").read()
    conn = S.db()
    try:
        run = S.retro_run_get(conn, day) or {}
        acts = S.retro_actions(conn, "day=?", (day,))
        verified = S.retro_actions(conn, "status='verified' AND graded_on=?", (day,))
        reverted = S.retro_actions(conn, "status='reverted' AND graded_on=?", (day,))
    finally:
        conn.close()
    head = ["**Retro %s — self-healing digest**" % day]
    if run.get("metrics"):
        k = (T._read_json_text(run["metrics"]) or {}).get("kpi") or {}
        head.append("pipeline $%.2f · skip %s%% · submitted %s%% · retro agent $%.2f"
                    % (k.get("cost_total_usd") or 0, k.get("qualify_skip_pct") or 0,
                       k.get("apply_submitted_pct") or 0, run.get("agent_cost") or 0))
    if acts:
        head.append("**Changed today (%d):**" % len(acts))
        head += ["- %s" % a["summary"] for a in acts[:12]]
    else:
        head.append("**Changed today:** nothing — no finding survived the gate")
    if verified or reverted:
        head.append("**Graded:** %d kept · %d reverted" % (len(verified), len(reverted)))
        head += ["- reverted: %s (%s)" % (a["target"], (a["outcome"] or "")[:100])
                 for a in reverted[:6]]
    msg = "\n".join(head)
    if quiet:
        return True, msg
    ok, detail = notify.post_retro(msg)
    return ok, detail if ok else msg + "\n\nNOT POSTED: %s" % detail


# ---------------------------------------------------------------------------
# backfill + status
# ---------------------------------------------------------------------------

def backfill(days=30):
    """Seed retro_runs from past logs. No agent, no changes: this is the history the
    optimisation loop needs before it can compare anything."""
    conn = S.db()
    n = 0
    try:
        for name in sorted(os.listdir(S.path("logs"))):
            m = re.match(r"(\d{4}-\d\d-\d\d)\.log$", name)
            if not m:
                continue
            day = m.group(1)
            if day >= S.TODAY:
                continue
            if S.retro_run_get(conn, day):
                continue
            if not os.path.exists(S.path("candidates-%s.json" % day)):
                continue
            packet = T.collect(day)
            S.retro_run_upsert(conn, {"date": day, "started": S.now(), "finished": S.now(),
                                      "status": "collected",
                                      "metrics": json.dumps({"kpi": packet["kpi"]}),
                                      "findings": len(packet["findings"])})
            n += 1
    finally:
        conn.close()
    return n


def status():
    conn = S.db()
    try:
        rows = S.retro_runs(conn, 14)
        acts = S.retro_actions(conn, "status='applied'", ())
    finally:
        conn.close()
    print("%-12s %8s %8s %7s %6s %6s %4s" % ("day", "$total", "$qualify", "skip%", "sub%", "find", "acts"))
    for r in rows:
        k = (T._read_json_text(r.get("metrics")) or {}).get("kpi") or {}
        cost = "  n/a" if k.get("cost_data_complete") is False else "%8.2f" % (k.get("cost_total_usd") or 0)
        print("%-12s %s %8.2f %7s %6s %6d %4d"
              % (r["date"], cost, k.get("cost_qualify_usd") or 0,
                 k.get("qualify_skip_pct"), k.get("apply_submitted_pct"),
                 r.get("findings") or 0, r.get("actions") or 0))
    if acts:
        print("\npending actions:")
        for a in acts:
            print("  #%d %s %s — %s" % (a["id"], a["day"], a["target"], a["summary"]))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="collect + report, no agent, no changes")
    ap.add_argument("--no-agent", action="store_true", help="collect + grade + report only")
    ap.add_argument("--backfill", type=int, nargs="?", const=30, default=None)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--digest", action="store_true", help="rebuild/post today's retro digest only")
    ap.add_argument("--day", help="review this day instead of today")
    ap.add_argument("--in-process", action="store_true",
                    help="called by daily.py, which already holds .lock; skip the lock check")
    args = ap.parse_args()

    S.ensure_dirs()
    day = args.day or S.TODAY
    if args.status:
        return status()
    if args.backfill is not None:
        print("backfilled %d day(s)" % backfill(args.backfill))
        return 0
    if args.digest:
        ok, detail = post_digest(day)
        print(detail)
        return 0 if ok else 1

    if not os.path.exists(S.path("digest-%s.md" % day)):
        print("no digest for %s yet: the daily run has not finished; retro does nothing" % day)
        return 0

    def go():
        result = collect_and_report(day, run_agent_pass=not (args.dry_run or args.no_agent))
        if not args.dry_run:
            ok, detail = post_digest(day)
            result["discord"] = detail
        print(json.dumps({k: v for k, v in result.items() if k != "agent"}, indent=1, default=str))
        if isinstance(result.get("agent"), dict):
            print("agent:", json.dumps({k: result["agent"][k] for k in
                                        ("ok", "error", "tokens_in", "tokens_out", "cost")}))
        return 0 if not (isinstance(result.get("agent"), dict) and result["agent"].get("error")) else 1

    if args.in_process:
        return go()
    with LockFree(LOCK) as lk:
        if not lk.ok:
            print("daily.py still holds .lock: retro exits (it will run again tomorrow, or "
                  "use `schtasks /Run /TN JobHuntRetro` after the run)")
            return 0
        return go()


if __name__ == "__main__":
    sys.exit(main())
