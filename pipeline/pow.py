#!/usr/bin/env python3
"""PoW: one grounded, published proof-of-work artifact per confirmed application.

Usage:
  python pow.py signals --titles "driver recruiter,fleet safety manager" [--days 21]
                        [--countries US] [--keywords …] [--limit 50] [--pages 1] [--token T]
  python pow.py generate --url <posting url> [--recipe R] [--candidate FILE] [--force] [--dry-run]
  python pow.py validate <file> [--url U --company C --role R --recipe R] [--signals FILE]
  python pow.py run <artifact_id|file> [--execute]
  python pow.py publish <artifact_id> [--base URL --key-file FILE]
  python pow.py attach <artifact_id> --run <trace run id>
  python pow.py revoke <artifact_id>
  python pow.py status [--url U] [--json]
  python pow.py reconcile [--dry-run]
  python pow.py daily [--dry-run]
  python pow.py smoke [--keep]

Design, in one paragraph: the model writes a candidate JSON, Python decides. `generate` owns the
envelope (posting URL, company, role, recipe, id, timestamp) so the artifact cannot be mislabelled;
`validate` re-checks every claim, every evidence reference and the privacy rules the host also
enforces; execution evidence is produced by this file, never by the candidate. AI evals are run by
the built-in deterministic evaluator below (`__eval`, our own code, isolated child process, bounded
timeout), so accepting an `ai_eval` artifact never executes model-written code. An artifact's own
declared code (FDE connectors) runs only after an operator passes `--execute`. Publishing and
attaching need an explicit artifact id, so a daily stage can generate but never publish or attach.

Config (`pow` block in config.json, all default-off):
  enabled                 false  - nothing automatic happens without it
  daily                   false  - the after-verify stage in daily.py is skipped
  max_per_day             1      - generations started per local day by that stage
  agent_budget_usd        2.00   - hard cap on one omp pass (`omp --max-cost`; print mode exits 3)
  exec_timeout_seconds    60     - hard bound on one evaluator / harness run
  signal_window_days      30     - "posted within" window for the gtm_signal fetch + validation
  treg_budget_usd         0.75   - hard cap on treg spend for ONE artifact's signal fetch

Every job-level limit is a dollar budget. An omp pass stops when its own spend reaches
`agent_budget_usd`; the signal fetch stops when its ledger reaches `treg_budget_usd`. Two timeouts
remain, and neither is a job limit: `exec_timeout_seconds` bounds a local evaluator / harness child
process, which costs no money and would otherwise hold the pipeline `.lock` from a runaway loop,
and `HTTP_TIMEOUT` is the socket timeout on treg and host requests, which turns a dead connection
into an error instead of a hang.

Signal fetch (the only place this program calls treg):
  endpoint   blitzapi.jobs.search   POST /v2/jobs/search through treg's proxy
             (treg catalog get blitzapi.jobs.search, verified 2026-10-16)
  price      $0.000495 per returned job row (1 credit), reported on every call as
             `fair_usage.records_used`; an empty page is free. max_results 1..50 per call,
             cursor paginated. The dollar budget (default $0.75) is the only bound: about 30
             calls of 50 rows, or far fewer when `--pages`/`--limit` ask for less.
  filters    job.title.include, job.description.include, job.date_posted.last_days,
             job.location.country_code.include, company.is_agency, company.size.include,
             company.industry.include, company.keywords.include
  store      state/pow/signals/<token>.jsonl holds every row, deduped by posting URL and by
             (company, title) so two title keywords cannot double-store one posting, while a
             company's OTHER roles are kept (the artifact's {n_multi} counts them). The printed
             table is one line per posting; `--emit-sources` prints the sources entries for
             inspection (the artifact's own acct-<n> entries are rebuilt from the store), and the
             meta file next to it is the per-call spend ledger.
  accounts   a gtm_signal candidate supplies the chosen posting URLs; `rebuild_gtm_rows` writes
             every other cell of accounts.csv from the store, so quoting, dates and role counts
             can only come from the fetch. A headline may name a kept account, and every
             company-shaped name is checked against the table (`check_names`).
  auth       the same ~/.treg/config.json token `discover.py` uses; never printed or logged.
"""

import argparse
import csv
import datetime
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import state as S  # noqa: E402
import pow_store as P  # noqa: E402

PY = sys.executable
UA = "job-harness/1.0"
HTTP_TIMEOUT = 60
HARNESS_NAMES = ("run.py", "main.py", "harness.py")

LIMITS = {
    "sources": 12,
    "findings": 12,
    "files": 8,
    "results": 12,
    "file_bytes": 64 * 1024,
    "total_bytes": 200 * 1024,
}

CANDIDATE_KEYS = ("title", "thesis", "sources", "findings", "deliverable", "limitations",
                  "email_line")

RECIPE_TITLE = {
    "gtm_signal": (r"\bgtm\b", r"go[- ]to[- ]market", r"\bgrowth\b", r"revenue operations",
                   r"\brevops\b", r"sales engineer", r"demand gen"),
    "fde_integration": (r"forward[- ]deployed", r"solutions? engineer", r"implementation",
                        r"integration", r"deployment", r"partner engineer",
                        r"technical solutions", r"field engineer", r"technical account"),
    "ai_eval": (r"\bai\b", r"applied ai", r"\bllm", r"machine learning", r"\bml\b", r"evals?\b",
                r"prompt engineer", r"generative"),
}

SAFE_PATH = re.compile(r"^[A-Za-z0-9._\-/]{1,120}$")

# ---------------------------------------------------------------------------
# signal fetch (gtm_signal): one treg endpoint, a hard budget, and the ATS vocabulary
# ---------------------------------------------------------------------------

TREG_ENDPOINT = "blitzapi.jobs.search"   # treg routes this to BlitzAPI POST /v2/jobs/search
TREG_USD_PER_RECORD = 0.000495           # catalog price per returned job row (1 credit)
TREG_MAX_ROWS = 500                      # hard cap on one token's stored postings
TREG_LIMIT_MAX = 50                      # endpoint max_results ceiling
TREG_PAGES_MAX = 3                       # pages one invocation may request (--pages); the dollar
                                         # budget, not a call count, bounds spend across runs

SIGNALS_ENV = "JOBHUNT_POW_SIGNALS"      # explicit signal file for validate/generate --candidate
SIGNAL_WINDOW_DEFAULT = 30               # "posted within N days" default for gtm_signal

ACCOUNTS_HEADER = ["account", "trigger", "evidence", "detail", "posted", "ats"]
GTM_MIN_ACCOUNTS = 8                     # fewer grounded accounts than this = abstain, not a page
GTM_TARGET_ACCOUNTS = 12                 # what a selective pack needs; 12..20 is the aim
GTM_MAX_ACCOUNTS = 25
GTM_PARTNER_SOURCES = 3                  # employer pages whose excerpts define buyers + partners

# Placeholders are the only way a number (or the fetch's own keyword list) reaches the published
# page: Python substitutes what it computed from the fetched rows, so a headline figure cannot be
# invented and the method line cannot misquote the search.
PLACEHOLDERS = ("n_accounts", "n_multi", "window_days", "n_ats_match",
                "n_fetched", "n_companies", "titles", "freshest_days", "n_recent_3d")

# A "finding" that states an absence ("none of the URLs reveals…", "no partner was found") is a
# limitation, not an insight: the page's findings are positive, checkable statements about the rows.
NON_FINDING_RE = (
    re.compile(r"\bnone of\b", re.I),
    re.compile(r"\bnothing\b", re.I),
    re.compile(r"\bno\s+[A-Za-z][\w'’./\-]*(?:\s+[A-Za-z][\w'’./\-]*){0,4}\s+"
               r"(?:found|revealed|reveals|reveal|identified|detected|visible|appear|"
               r"appears|showed|shows|matched|matches)\b", re.I),
    re.compile(r"\bnot visible\b", re.I),
    re.compile(r"\bcannot be (?:inferred|determined|established)\b", re.I),
    re.compile(r"\bunable to (?:find|identify|determine)\b", re.I),
)

# Names in the hook: only a kept row's own company (or the employer, or an integration partner) may
# be named. Detecting "a company name" deterministically is only honest where the text says it is
# one — a company designator ("Logistics", "Inc.", "Group") — so role titles, cities and acronyms
# are never mistaken for accounts.
NAME_DESIGNATOR = {
    "inc", "llc", "ltd", "corp", "corporation", "co", "company", "group", "holdings",
    "logistics", "freight", "transport", "transportation", "trucking", "carrier", "carriers",
    "industries", "distribution", "express", "gmbh",
}
NAME_SUFFIX_ONLY = {"inc", "llc", "ltd", "corp", "corporation", "co", "company", "group",
                    "holdings", "gmbh"}
CAP_RUN_RE = re.compile(r"\b[A-Z][A-Za-z0-9&.'’\-]*(?:[ \t]+[A-Z][A-Za-z0-9&.'’\-]*)*")
PLACEHOLDER_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
_MARK_OPEN, _MARK_CLOSE = "\ue000", "\ue001"

# ATS vendor from the posting URL: (host/query hint, vendor label used in accounts.csv).
ATS_URL_HINTS = (
    ("gh_jid=", "greenhouse"),
    ("greenhouse.io", "greenhouse"),
    ("lever.co", "lever"),
    ("ashbyhq.com", "ashby"),
    ("icims.com", "icims"),
    ("myworkdayjobs.com", "workday"),
    ("workday.com", "workday"),
    ("smartrecruiters.com", "smartrecruiters"),
    ("rippling.com", "rippling"),
    ("jobvite.com", "jobvite"),
    ("taleo.net", "taleo"),
    ("successfactors.com", "successfactors"),
    ("workable.com", "workable"),
    ("breezy.hr", "breezy"),
    ("recruitee.com", "recruitee"),
    ("bamboohr.com", "bamboohr"),
    ("ultipro.com", "ultipro"),
    ("ukg.com", "ultipro"),
    ("paylocity.com", "paylocity"),
    ("avature.net", "avature"),
    ("brassring.com", "brassring"),
    ("teamtailor.com", "teamtailor"),
    ("personio.de", "personio"),
    ("personio.com", "personio"),
    ("jazzhr.com", "jazzhr"),
    ("applytojob.com", "jazzhr"),
    ("jobscore.com", "jobscore"),
    ("zohorecruit.com", "zoho"),
    ("recruiterbox.com", "recruiterbox"),
    ("hrmdirect.com", "hrmdirect"),
    ("catsone.com", "catsone"),
    ("pinpointhq.com", "pinpoint"),
    ("workforcenow.adp.com", "adp"),
)
ATS_VENDORS = tuple(sorted({vendor for _, vendor in ATS_URL_HINTS}))

TRIGGER_STOP = {
    "the", "and", "of", "for", "a", "an", "in", "on", "at", "to", "with", "by", "or", "new",
    "hiring", "hire", "hires", "job", "jobs", "opening", "openings", "open", "role", "roles",
    "position", "positions", "posting", "posted", "remote", "hybrid", "onsite", "us", "usa",
    "contract", "full", "time", "full-time",
}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
EMAIL_DOC_DOMAIN = re.compile(r"@([A-Za-z0-9\-]+\.)*(example\.(com|org|net)|test|invalid|local)$",
                              re.I)
LOCAL_PATH_RE = re.compile(
    r"(?:[A-Za-z]:\\)|file://|(?<![\w:/])/(?:Users|home|tmp|var|mnt|root|opt)/"
    r"|\\\\[A-Za-z0-9._\-]+\\")
# A posting URL carries long numeric ids (a LinkedIn job id is ten digits) that are not phone
# numbers: URLs are masked before the phone-shaped scan, because the URL itself is already
# validated as public http(s) and published on purpose.
URL_IN_TEXT = re.compile(r"https?://[^\s,\"']+", re.I)
PHONE_RE = re.compile(r"\+?\d[\d ().\-]{8,}\d")
SECRET_RE = re.compile(
    r"(?:sk-[A-Za-z0-9_\-]{16,})|(?:gh[pousr]_[A-Za-z0-9]{20,})|(?:AKIA[0-9A-Z]{16})"
    r"|(?:xox[abprs]-[A-Za-z0-9\-]{10,})|(?:-----BEGIN [A-Z ]*PRIVATE KEY-----)"
    r"|(?i:\b(?:api[_-]?key|secret|token|password)\b\s*[:=]\s*\S{8,})"
    r"|(?i:\bbearer\s+[A-Za-z0-9._\-]{20,})")


# ---------------------------------------------------------------------------
# config and small helpers
# ---------------------------------------------------------------------------

_POW_OVERRIDE = None


def pow_conf():
    """The `pow` block of config.json, plus the smoke-only JOBHUNT_POW_CONFIG override.

    JOBHUNT_POW_CONFIG exists so `pow.py smoke` can exercise the publish/attach gates without
    flipping the real config.json. Nothing else sets it; leave it unset for real runs.
    """
    global _POW_OVERRIDE
    if _POW_OVERRIDE is None:
        _POW_OVERRIDE = {}
        raw = os.environ.get("JOBHUNT_POW_CONFIG")
        if raw:
            try:
                parsed = json.loads(raw)
                _POW_OVERRIDE = parsed if isinstance(parsed, dict) else {}
            except ValueError:
                print("pow: ignoring invalid JOBHUNT_POW_CONFIG")
    conf = dict(S.CONFIG.get("pow") or {})
    conf.update(_POW_OVERRIDE)
    return conf


def cfg(key, default=None):
    return pow_conf().get(key, default)


def cohort_for(stamp):
    """Immutable send-batch label: the ISO week the artifact was created.

    pow_telemetry forbids changing a cohort after publish/attach, so it must be derived from
    something that cannot change: the artefact's own creation stamp. Recipe is separate.
    """
    try:
        parsed = time.strptime((stamp or "")[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        parsed = time.gmtime()
    # ISO year-week ("2026-W45"): unambiguous across a year boundary, and one batch per week.
    return time.strftime("%G-W%V", parsed)


def tracehire():
    t = S.CONFIG.get("tracehire") or {}
    return (t.get("base") or ""), (t.get("key_file") or "")


def _tail(data, limit=1200):
    if data is None:
        return ""
    if isinstance(data, bytes):
        data = data.decode("utf-8", "replace")
    return data.strip()[-limit:]


def _clean_env():
    """Minimal environment for anything this file executes: no config, no credentials."""
    keep = ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "PATH", "TEMP", "TMP", "COMSPEC", "PATHEXT")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env.update({"PYTHONIOENCODING": "utf-8", "PYTHONHASHSEED": "0",
                "PYTHONDONTWRITEBYTECODE": "1"})
    return env


def iso_ok(value):
    if not isinstance(value, str) or "T" not in value:
        return False
    try:
        d = datetime.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    return d.tzinfo is not None


def http_url_ok(url):
    if not isinstance(url, str) or not url.strip() or len(url) > 500:
        return False
    p = urllib.parse.urlsplit(url.strip())
    if p.scheme not in ("http", "https") or not p.netloc or "@" in p.netloc:
        return False
    host = (p.hostname or "").lower()
    if not host or "." not in host:
        return False
    if re.match(r"^(localhost|127\.|10\.|192\.168\.|169\.254\.|0\.0\.0\.0|::1$)", host):
        return False
    if re.match(r"^172\.(1[6-9]|2\d|3[01])\.", host):
        return False
    return not (host.endswith(".local") or host.endswith(".internal"))


def _text(errors, field, value, lo, hi):
    if not isinstance(value, str):
        errors.append("%s must be a string" % field)
        return False
    if not lo <= len(value.strip()) <= hi:
        errors.append("%s must be %d..%d characters (got %d)"
                      % (field, lo, hi, len(value.strip())))
        return False
    return True


def add_limitation(art, text):
    lim = art.get("limitations")
    if not isinstance(lim, list):
        lim = art["limitations"] = []
    if len(lim) < 8:
        lim.append(text)


def iter_numbers(obj, path="artifact"):
    """Every number leaf, with its path: JSON cannot carry NaN/Infinity, so neither can an artifact."""
    if isinstance(obj, bool):
        return
    if isinstance(obj, float):
        yield path, obj
    elif isinstance(obj, dict):
        for k in sorted(obj):
            yield from iter_numbers(obj[k], "%s.%s" % (path, k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from iter_numbers(v, "%s[%d]" % (path, i))


def iter_strings(obj, path="artifact"):
    """Every string leaf, with its dotted path: one pass for all the privacy checks."""
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k in sorted(obj):
            yield from iter_strings(obj[k], "%s.%s" % (path, k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from iter_strings(v, "%s[%d]" % (path, i))


def shingles(text, n=12):
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {" ".join(words[i:i + n]) for i in range(len(words) - n + 1)}


def _overlaps(text, grams):
    if not grams:
        return False
    for g in shingles(text):
        if g in grams:
            return True
    return False


def _phone_shaped(text):
    """Phone, not date: enough digits, and no letter glued to the match (ISO stamps have T/Z)."""
    for m in PHONE_RE.finditer(text):
        s = m.group(0)
        if sum(ch.isdigit() for ch in s) < 9:
            continue
        around = text[max(0, m.start() - 1):m.end() + 1]
        if not re.search(r"[A-Za-z]", around):
            return True
    return False


# ---------------------------------------------------------------------------
# recipes: deterministic routing, or an honest abstention
# ---------------------------------------------------------------------------

def _hits(patterns, text):
    return sum(1 for p in patterns if re.search(p, text))


def route_recipe(title, jd=""):
    """(recipe, why) for one posting; recipe None means "say so and stop"."""
    head = (title or "").lower()
    body = (jd or "").lower()
    scores = {r: 3 * _hits(p, head) + _hits(p, body) for r, p in RECIPE_TITLE.items()}
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    best, score = ranked[0]
    if score < 3:
        return None, "no supported recipe matches this title (%s): scores %s" % (
            title or "untitled", scores)
    if len(ranked) > 1 and ranked[1][1] == score:
        return None, "ambiguous recipe routing for %s: %s" % (title or "untitled", scores)
    return best, "routed %r -> %s (scores %s)" % (title or "untitled", best, scores)


# ---------------------------------------------------------------------------
# built-in deterministic evaluator (the only thing an ai_eval artifact may execute)
# ---------------------------------------------------------------------------

def _baseline_keyword_classify(text, config):
    labels = config.get("labels") or {}
    low = (text or "").lower()
    best, best_n = "unknown", 0
    for label in sorted(labels):
        n = 0
        for word in labels.get(label) or []:
            if isinstance(word, str) and word.strip():
                n += len(re.findall(r"(?<![a-z0-9])" + re.escape(word.strip().lower())
                                    + r"(?![a-z0-9])", low))
        if n > best_n:
            best, best_n = label, n
    return best


def _baseline_regex_extract(text, config):
    try:
        m = re.search(config.get("pattern") or "", text or "")
    except re.error:
        return ""
    if not m:
        return ""
    try:
        return m.group(int(config.get("group", 1))) or ""
    except (IndexError, re.error):
        return ""


def _baseline_exact_match(text, config):
    value = (text or "").lower().strip()
    if (config.get("normalize") or "lower_trim") == "lower_alnum":
        value = re.sub(r"[^a-z0-9 ]+", " ", value)
        value = re.sub(r"\s+", " ", value).strip()
    return value


BASELINES = {
    "keyword_classify": _baseline_keyword_classify,
    "regex_extract": _baseline_regex_extract,
    "exact_match": _baseline_exact_match,
}


def validate_eval_spec(spec):
    """Shape and safety rules for the declarative eval spec an ai_eval candidate ships."""
    e = []
    if not isinstance(spec, dict):
        return ["eval spec is not a JSON object"]
    name = spec.get("baseline")
    if name not in BASELINES:
        return ["eval spec baseline must be one of: %s" % ", ".join(sorted(BASELINES))]
    config = spec.get("config")
    if config is None:
        config = {}
    if not isinstance(config, dict):
        e.append("eval spec config must be an object")
        config = {}
    if name == "keyword_classify":
        labels = config.get("labels")
        if not isinstance(labels, dict) or not 2 <= len(labels) <= 12:
            e.append("keyword_classify needs config.labels with 2..12 labels")
        else:
            for label, words in labels.items():
                if not isinstance(words, list) or not 1 <= len(words) <= 20 or \
                        not all(isinstance(w, str) and w.strip() for w in words):
                    e.append("config.labels[%r] must be 1..20 non-empty keyword strings" % label)
    elif name == "regex_extract":
        pat = config.get("pattern")
        if not isinstance(pat, str) or not 1 <= len(pat) <= 200:
            e.append("regex_extract needs config.pattern of 1..200 characters")
        else:
            try:
                re.compile(pat)
            except re.error as ex:
                e.append("config.pattern does not compile: %s" % ex)
            if re.search(r"\([^()]*[+*][^()]*\)\s*[+*{]", pat):
                e.append("config.pattern nests a quantifier inside a quantified group "
                         "(backtracking risk)")
        group = config.get("group", 1)
        if not isinstance(group, int) or isinstance(group, bool) or not 0 <= group <= 9:
            e.append("config.group must be an integer 0..9")
    elif name == "exact_match":
        if (config.get("normalize") or "lower_trim") not in ("lower_trim", "lower_alnum"):
            e.append("config.normalize must be lower_trim or lower_alnum")
    cases = spec.get("cases")
    if not isinstance(cases, list) or not 3 <= len(cases) <= 50:
        e.append("cases must be a list of 3..50 entries")
        cases = []
    seen, chars = set(), 0
    for i, case in enumerate(cases):
        w = "cases[%d]" % i
        if not isinstance(case, dict):
            e.append(w + " must be an object")
            continue
        cid = case.get("id")
        if cid is not None:
            if not isinstance(cid, str) or not 1 <= len(cid) <= 40:
                e.append(w + ".id must be a string of 1..40 characters")
            elif cid in seen:
                e.append(w + ".id is a duplicate: %s" % cid)
            else:
                seen.add(cid)
        txt = case.get("input")
        if not isinstance(txt, str) or not txt.strip() or len(txt) > 4000:
            e.append(w + ".input must be a non-empty string of at most 4000 characters")
        else:
            chars += len(txt)
        exp = case.get("expected")
        if not isinstance(exp, str) or not exp.strip() or len(exp) > 200:
            e.append(w + ".expected must be a non-empty string of at most 200 characters")
    if cases and chars < 120:
        e.append("the cases carry only %d characters of input: too small to be an eval" % chars)
    claim = spec.get("claim")
    if claim is not None:
        if not isinstance(claim, dict) or claim.get("metric") != "accuracy":
            e.append("claim must be an object with metric=accuracy")
        else:
            low = claim.get("min")
            if not isinstance(low, (int, float)) or isinstance(low, bool) or not 0 <= low <= 1:
                e.append("claim.min must be a number between 0 and 1")
    return e


def execute_eval(spec):
    """Score the declared cases against the built-in baseline. Deterministic, no I/O, no network."""
    baseline = spec["baseline"]
    config = spec.get("config") or {}
    fn = BASELINES[baseline]
    rows, correct = [], 0
    for i, case in enumerate(spec["cases"]):
        predicted = fn(case["input"], config)
        if baseline == "exact_match":
            ok = predicted == case["expected"].strip()
        else:
            ok = str(predicted).strip().lower() == case["expected"].strip().lower()
        correct += 1 if ok else 0
        rows.append({"id": case.get("id") or "case%d" % (i + 1), "ok": ok,
                     "predicted": str(predicted)[:120]})
    total = len(rows)
    return {"ok": True, "baseline": baseline, "cases": total, "correct": correct,
            "accuracy": round(correct / total, 4) if total else 0.0, "rows": rows}


def run_eval_spec(spec, timeout):
    """Run `execute_eval` in a child process with a deadline.

    This is pow.py itself under `__eval`: the spec is data, never code, so an ai_eval artifact can
    be accepted without executing anything the model wrote.
    """
    errors = validate_eval_spec(spec)
    if errors:
        return {"ok": False, "error": "; ".join(errors[:4])}
    scratch = tempfile.mkdtemp(prefix="pow-eval-")
    try:
        try:
            proc = subprocess.run([PY, "-I", "-B", os.path.abspath(__file__), "__eval"],
                                  input=json.dumps(spec).encode("utf-8"),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  cwd=scratch, env=_clean_env(), timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "built-in evaluator timed out after %ss" % timeout}
        except OSError as ex:
            return {"ok": False, "error": "could not start the evaluator: %s" % ex}
        if proc.returncode != 0:
            return {"ok": False, "error": "evaluator exited %s: %s"
                    % (proc.returncode, _tail(proc.stderr or proc.stdout))}
        try:
            return json.loads(proc.stdout.decode("utf-8", "replace"))
        except ValueError:
            return {"ok": False, "error": "evaluator returned no JSON: %s" % _tail(proc.stdout)}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def eval_result_entry(spec, res, spec_path):
    claim = spec.get("claim") or {}
    total, correct, accuracy = res["cases"], res["correct"], res["accuracy"]
    misses = [r["id"] for r in res.get("rows", []) if not r["ok"]][:8]
    expected = ("accuracy >= %.2f on the declared cases" % float(claim["min"])) if claim else \
        "accuracy measured against the declared cases (no threshold claimed)"
    observed = "%d/%d correct (accuracy %.3f) with baseline %s" % (
        correct, total, accuracy, spec["baseline"])
    if misses:
        observed += "; missed: " + ", ".join(misses)
    met = True if not claim else (accuracy + 1e-9 >= float(claim["min"]))
    return {"name": "eval:" + spec["baseline"], "input": "%s (%d cases)" % (spec_path, total),
            "expected": expected, "observed": observed,
            "status": "passed" if met else "failed", "evidence_ids": []}


def find_eval_spec(art):
    """(path, spec, error) for the artifact's single declarative eval file."""
    files = (art.get("deliverable") or {}).get("files") or []
    specs = []
    for f in files:
        if not isinstance(f, dict) or not str(f.get("path", "")).endswith(".json"):
            continue
        try:
            obj = json.loads(f.get("content") or "")
        except (TypeError, ValueError):
            continue
        if isinstance(obj, dict) and "baseline" in obj and "cases" in obj:
            specs.append((f["path"], obj))
    if not specs:
        return None, None, ("ai_eval needs an eval spec in deliverable.files: a .json file with "
                            "baseline, config and cases")
    if len(specs) > 1:
        return None, None, "ai_eval allows one eval spec per artifact (found %d)" % len(specs)
    return specs[0][0], specs[0][1], None


# ---------------------------------------------------------------------------
# operator-approved execution of the artifact's own code (FDE connectors)
# ---------------------------------------------------------------------------

def is_harness(path):
    base = os.path.basename(str(path or ""))
    return base in HARNESS_NAMES or base.endswith("_run.py")


def execute_artifact_files(files, timeout):
    """Copy the declared files into a scratch dir and run the declared harnesses there.

    Only called with an explicit operator `--execute`. Bounded: fresh dir under state/pow/tmp,
    minimal env (nothing from config.json or the key files is passed), per-file timeout, output
    truncated. There is no network isolation on the host, which is exactly why this is opt-in.
    """
    targets = [f for f in files if isinstance(f, dict) and is_harness(f.get("path"))]
    if not targets:
        return [{"name": "exec:none", "input": "deliverable.files",
                 "expected": "a runnable harness (run.py, main.py, harness.py, *_run.py)",
                 "observed": "no harness file declared", "status": "not_run",
                 "evidence_ids": []}]
    P.ensure_dirs()
    scratch = tempfile.mkdtemp(prefix="pow-exec-", dir=P.pow_dir("tmp"))
    rows = []
    try:
        for f in files:
            if not isinstance(f, dict):
                continue
            dest = os.path.join(scratch, *str(f["path"]).split("/"))
            os.makedirs(os.path.dirname(dest) or scratch, exist_ok=True)
            with open(dest, "wb") as fh:
                fh.write(str(f.get("content") or "").encode("utf-8"))
        for f in targets:
            rel = str(f["path"])
            tgt = os.path.join(scratch, *rel.split("/"))
            cmd = [PY, "-I", "-B", os.path.basename(tgt)]
            try:
                proc = subprocess.run(cmd, cwd=os.path.dirname(tgt), stdin=subprocess.DEVNULL,
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      env=_clean_env(), timeout=timeout)
                code, output = proc.returncode, _tail(proc.stdout, 900)
            except subprocess.TimeoutExpired:
                code, output = -1, "timed out after %ss" % timeout
            except OSError as ex:
                code, output = -1, "could not start: %s" % ex
            rows.append({"name": "exec:" + rel,
                         "input": "python -I -B %s (scratch copy, no credentials in env)"
                                  % os.path.basename(tgt),
                         "expected": "exit status 0",
                         "observed": ("exit %s%s" % (code, "; " + output if output else ""))[:1900],
                         "status": "passed" if code == 0 else "failed", "evidence_ids": []})
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return rows


# ---------------------------------------------------------------------------
# signal fetch and grounding (gtm_signal)
#
# The artifact's account rows may only come from a `pow.py signals` fetch. Python makes the treg
# call, keeps the budget, and stores every returned posting under the same token as the candidate
# file. `ground_gtm` then re-derives every row from that store, so a row the model remembered but
# the fetch never returned cannot reach the page.
# ---------------------------------------------------------------------------

def ats_guess(url):
    """The ATS vendor a posting URL reveals, or "" when the URL is a generic board."""
    u = (url or "").strip()
    if not u:
        return ""
    low = u.lower()
    host = (urllib.parse.urlsplit(low).hostname or "")
    for hint, vendor in ATS_URL_HINTS:
        if hint.endswith("="):
            if hint in low:
                return vendor
        elif hint in host:
            return vendor
    return ""


def _as_text(value):
    return value.strip() if isinstance(value, str) else ""


def normalize_signal(raw, fetched_at, window_days, query):
    """One treg result row -> the stored signal row (and the excerpt the artifact must quote).

    Both response shapes the endpoint family has returned are accepted: the flat
    `company_name`/`location` shape and a nested `company` object with firmographics.
    """
    if not isinstance(raw, dict):
        return None
    company = raw.get("company_name") or raw.get("company")
    company_li = _as_text(raw.get("company_linkedin_url"))
    industry = size = ""
    if isinstance(company, dict):
        industry = _as_text(company.get("industry"))
        size = _as_text(company.get("size"))
        company_li = company_li or _as_text(company.get("linkedin_url"))
        company = company.get("name") or company.get("domain")
    company, title = _as_text(company), _as_text(raw.get("title"))
    url = _as_text(raw.get("url"))
    if not company or not title or not http_url_ok(url):
        return None
    posted = _as_text(raw.get("date_posted")) or _as_text(raw.get("posted"))
    location = raw.get("location") if isinstance(raw.get("location"), dict) else {}
    city = _as_text(location.get("city"))
    country = _as_text(location.get("country_code"))
    excerpt = "%s — %s" % (title, company)
    if posted:
        # Date only: the full timestamp would trip the phone-shaped privacy scan downstream.
        excerpt += " (posted %s)" % (posted[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", posted)
                                     else posted)
    if city:
        excerpt += " · %s" % city
    # The host caps a source excerpt at 1000 characters; keep the stored excerpt (which the
    # artifact must quote verbatim) well inside that, and identical on both sides.
    excerpt = excerpt[:400]
    return {"url": url, "company": company, "title": title, "posted": posted,
            "location": {"city": city, "country_code": country},
            "company_linkedin": company_li, "industry": industry, "size": size,
            "ats": ats_guess(url), "excerpt": excerpt, "fetched_at": fetched_at,
            "window_days": int(window_days), "source": TREG_ENDPOINT, "query": query}


def _treg_creds():
    """The treg client config `discover.py` already uses. The token is never printed."""
    path = os.path.expanduser("~/.treg/config.json")
    try:
        with open(path, encoding="utf-8") as fh:
            conf = json.load(fh)
    except OSError as ex:
        raise RuntimeError("treg is not configured (%s: %s); run `treg login`"
                           % (path, ex.strerror or ex))
    except ValueError:
        raise RuntimeError("treg config %s is not valid JSON" % path)
    if not isinstance(conf, dict) or not conf.get("token"):
        raise RuntimeError("no treg token in %s; run `treg login`" % path)
    return conf


def treg_call(payload, budget_left_usd, timeout=180):
    """One POST through treg's proxy. Returns {"body", "cost_usd", "records"}.

    Per-call `Idempotency-Key` and `X-Treg-Route-Max-Cost` (the remaining budget) are set so a
    retried or routed call cannot quietly spend past the cap. The response header
    `x-treg-cost-micro` is the charge; without it the call is priced at the catalog rate.
    """
    conf = _treg_creds()
    base = (conf.get("base_url") or "https://treg.to").rstrip("/")
    headers = {"X-Treg-Token": conf["token"],
               "x-treg-org": conf.get("active_org") or "",
               "content-type": "application/json",
               "Idempotency-Key": os.urandom(16).hex(),
               "X-Treg-Route-Max-Cost": "%.6f" % max(0.0, float(budget_left_usd))}
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request("%s/call/%s" % (base, TREG_ENDPOINT), data=body,
                                 method="POST", headers=headers)
    micro = None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw, code, micro = resp.read(), getattr(resp, "status", 200), \
                resp.headers.get("x-treg-cost-micro")
    except urllib.error.HTTPError as ex:
        raw, code, micro = ex.read(), ex.code, ex.headers.get("x-treg-cost-micro")
    except Exception as ex:  # noqa: BLE001 - transport failures are reported, not raised raw
        raise RuntimeError("treg call failed (%s); no signal rows were stored" % type(ex).__name__)
    try:
        parsed = json.loads(raw or b"{}")
    except ValueError:
        parsed = {}
    if code != 200:
        raise RuntimeError("treg HTTP %d: %s" % (code, _tail(raw, 300)))
    if isinstance(parsed, dict) and parsed.get("error"):
        raise RuntimeError("treg error: %s" % str(parsed.get("error"))[:300])
    records = 0
    usage = parsed.get("fair_usage") if isinstance(parsed, dict) else None
    if isinstance(usage, dict):
        try:
            records = int(usage.get("records_used") or 0)
        except (TypeError, ValueError):
            records = 0
    cost = None
    if micro:
        try:
            cost = int(micro) / 1e6
        except (TypeError, ValueError):
            cost = None
    if cost is None:
        cost = records * TREG_USD_PER_RECORD
    return {"body": parsed if isinstance(parsed, dict) else {}, "cost_usd": cost,
            "records": records}


def signal_payload(titles, days, countries, keywords=None, icp_keywords=None, sizes=None,
                   industries=None, limit=50, cursor=None):
    """The blitzapi.jobs.search body: job filters AND company firmographics, max_results 1..50."""
    job = {"title": {"include": list(titles)[:TREG_LIMIT_MAX]},
           "date_posted": {"last_days": int(days)}}
    if countries:
        job["location"] = {"country_code": {"include": list(countries)[:TREG_LIMIT_MAX]}}
    if keywords:
        job["description"] = {"include": list(keywords)[:TREG_LIMIT_MAX]}
    company = {"is_agency": False}
    if icp_keywords:
        company["keywords"] = {"include": list(icp_keywords)[:TREG_LIMIT_MAX]}
    if sizes:
        company["size"] = {"include": list(sizes)[:TREG_LIMIT_MAX]}
    if industries:
        company["industry"] = {"include": list(industries)[:TREG_LIMIT_MAX]}
    body = {"job": job, "company": company, "max_results": int(limit)}
    if cursor:
        body["cursor"] = cursor
    return body


def _posted_date(row):
    """The stored posting's calendar date (YYYY-MM-DD), or "" when the fetch had no usable one."""
    raw = _as_text(row.get("posted"))
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", raw)
    return m.group(1) if m else ""


def _date(value):
    try:
        return datetime.date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _fetched_date(row, meta=None):
    stamp = _as_text(row.get("fetched_at")) or _as_text((meta or {}).get("started"))
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", stamp)
    return m.group(1) if m else ""


def _trigger_words(text):
    words = set()
    for w in re.findall(r"[a-z][a-z0-9+#.\-]*", (text or "").lower()):
        if w in TRIGGER_STOP or any(ch.isdigit() for ch in w):
            continue
        words.add(w[:-1] if len(w) > 3 and w.endswith("s") else w)
    return words


def _trigger_matches(trigger, title):
    """Does the trigger column describe this posting's title? Half its words must come from it.

    "hiring driver recruiters" matches "Driver Recruiter"; "expanding the european sales team"
    does not, so a paraphrased-in-claim cannot borrow a fetched posting it does not describe.
    """
    want = _trigger_words(trigger)
    have = _trigger_words(title)
    if not want or not have:
        return False
    return len(want & have) / len(want) >= 0.5


def signal_titles(meta):
    """The title keywords that actually ran, from the fetch ledger (never from the model)."""
    out = []
    for call in (meta or {}).get("calls") or []:
        if not isinstance(call, dict):
            continue
        for t in call.get("titles") or []:
            t = _as_text(t)
            if t and t not in out:
                out.append(t)
    return out


def signals_path_for(token=None, explicit=None):
    """Where this artifact's signal store lives: --signals, then the env override, then the token."""
    if explicit:
        return os.path.abspath(explicit)
    env = (os.environ.get(SIGNALS_ENV) or "").strip()
    if env:
        return os.path.abspath(env)
    if token:
        return P.signals_file(token)
    return ""


def load_signal_rows(path):
    """Read a signal jsonl by path. Returns (rows, error)."""
    rows = []
    if not path or not os.path.isfile(path):
        return rows, "no signal file at %s" % (path or "(unset)")
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                return rows, "%s line %d is not JSON" % (os.path.basename(path), n)
            if not isinstance(obj, dict) or not _as_text(obj.get("url")):
                return rows, "%s line %d is not a posting" % (os.path.basename(path), n)
            rows.append(obj)
    return rows, None


def _first_url(cells):
    """The first public http(s) URL in one parsed CSV row: the only cell the candidate must supply."""
    for cell in cells:
        c = _as_text(cell)
        if http_url_ok(c):
            return c
    for cell in cells:
        m = URL_IN_TEXT.search(_as_text(cell))
        if m:
            u = m.group(0).rstrip(".,;)]}\"'")
            if http_url_ok(u):
                return u
    return ""


def _trigger_cell(title):
    """The fetched posting title as the table's trigger, collapsed and trimmed to the column limit."""
    t = " ".join(_as_text(title).split())
    if len(t) > 160:
        cut = t[:160]
        t = cut.rsplit(" ", 1)[0] if " " in cut else cut
    return t


def rebuild_gtm_rows(cand, rows):
    """Author the gtm_signal table from the fetch: the candidate supplies posting URLs, nothing else.

    A model-written CSV fails in ways that have nothing to do with the finding: an unquoted comma
    in a company name, a hand-typed posted date, a `detail` that contradicts the store, an excerpt
    retyped by hand. The model's only real job is to *choose*, so its rows are parsed leniently for
    the posting URLs and the table is rebuilt here: account, trigger, detail, posted and ats all
    come from the stored posting, the file is written by the `csv` module (so every cell is quoted
    correctly), one row per company in the candidate's own order (strongest signal first), and each
    `acct-<n>` source is regenerated from the store so its excerpt cannot disagree with it.

    Returns (candidate, errors, info) where info["rows"] is the chosen rows, in order.
    """
    info = {"rows": []}
    deliverable = cand.get("deliverable")
    files = deliverable.get("files") if isinstance(deliverable, dict) else None
    first = files[0] if isinstance(files, list) and files and isinstance(files[0], dict) else None
    if not first or not rows:
        return cand, [], info          # ground_gtm reports the missing table / the empty store
    text = first.get("content") if isinstance(first.get("content"), str) else ""
    try:
        parsed = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
    except csv.Error as ex:
        return cand, ["accounts.csv is not parseable CSV: %s" % ex], info
    parsed = [r for r in parsed if any((c or "").strip() for c in r)]
    if not parsed:
        return cand, ["accounts.csv is empty: one posting URL per chosen account is all it "
                      "needs"], info
    header = [_as_text(c).lower() for c in parsed[0]]
    body = parsed[1:] if header and header[0] == "account" else parsed
    by_url, by_key, index_of = {}, {}, {}
    for i, r in enumerate(rows, 1):
        u = _as_text(r.get("url"))
        if not u:
            continue
        by_url.setdefault(u, r)
        by_key.setdefault(P.url_key(u), r)
        index_of.setdefault(u, i)
        index_of.setdefault(P.url_key(u), i)
    roles = {}
    for r in rows:
        c = S.norm_text(r.get("company") or "")
        if c:
            roles[c] = roles.get(c, 0) + 1
    errors, seen, table = [], set(), []
    for i, raw in enumerate(body, 1):
        where = "accounts.csv row %d" % i
        url = _first_url(raw)
        if not url:
            errors.append("%s carries no posting URL: one row per chosen account, and the "
                          "posting URL is the only cell Python reads" % where)
            continue
        stored = by_url.get(url) or by_key.get(P.url_key(url))
        if stored is None:
            errors.append("%s evidence %r is not in this token's signal store: rows must come "
                          "from `python pow.py signals`, never from memory" % (where, url[:120]))
            continue
        company = _as_text(stored.get("company"))
        key = S.norm_text(company)
        if key in seen:
            continue                   # one row per company: the first (strongest) occurrence wins
        seen.add(key)
        index = index_of.get(url) or index_of.get(P.url_key(url)) or 0
        n_roles = roles.get(key, 0)
        table.append([company, _trigger_cell(stored.get("title")), url,
                      "%d open roles" % n_roles if n_roles > 1 else "",
                      _posted_date(stored), _as_text(stored.get("ats"))])
        info["rows"].append({"company": company, "url": url, "index": index,
                             "excerpt": _as_text(stored.get("excerpt")),
                             "fetched_at": _as_text(stored.get("fetched_at")),
                             "roles": n_roles})

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(ACCOUNTS_HEADER)
    writer.writerows(table)
    out = dict(cand)
    deliverable = dict(deliverable)
    files = list(files)
    files[0] = dict(first, content=buf.getvalue())
    deliverable["files"] = files
    out["deliverable"] = deliverable

    # Sources: the employer pages stay the candidate's (they are its own reading), and the account
    # sources are rebuilt from the store, keyed by the posting's position in it. An acct source no
    # kept row refers to is pruned, so a finding that cites one fails as an unknown source.
    sources = out.get("sources") if isinstance(out.get("sources"), list) else []
    entries = [s for s in sources
               if isinstance(s, dict) and not _as_text(s.get("id")).startswith("acct-")]
    for row in info["rows"]:
        excerpt = row["excerpt"]
        entries.append({"id": "acct-%d" % row["index"], "url": row["url"],
                        "retrieved_at": row["fetched_at"] or P.now_iso(),
                        "sha256": hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
                        "excerpt": excerpt})
    out["sources"] = entries
    return out, errors, info


def _name_norm(text):
    """Case/punctuation-insensitive company name: "Maersk's" and "A.P. Moller - Maersk" fold."""
    return S.norm_text(re.sub(r"['’]s\b", "", text or ""))


def check_names(text, where, kept, store, employer=""):
    """A name in the hook must be a kept row's own company — nothing else is verifiable.

    Two deterministic checks, no guessing at what "looks like" a name:

    (a) any company the fetch returned but the pack did not keep may not be named (that would
        present a filtered-out account as part of the picture), and
    (b) a company-shaped phrase — one carrying a designator such as "Logistics", "Freight" or
        "Inc." — must be the employer, an integration partner or a kept row. Role titles, cities,
        acronyms and ordinary capitalised words carry no designator, so they are never flagged.

    Returns a list of errors; `text` is published as written.
    """
    if not isinstance(text, str) or not text.strip():
        return []
    allowed = [_name_norm(n) for n in list(kept) + list(ATS_VENDORS) + [employer]]
    allowed = [a for a in allowed if a]
    errors, reported = [], set()
    norm = _name_norm(text)
    for name in sorted({_as_text(n) for n in store}):
        n = _name_norm(name)
        if len(n) < 4 or n in allowed or any(n in a for a in allowed) or n in reported:
            continue
        if re.search(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(n), norm):
            reported.add(n)
            errors.append("%s names %r, which the fetch returned but which is not a kept account "
                          "row: a name may only be an account the table actually shows"
                          % (where, name))
    for run in CAP_RUN_RE.finditer(text):
        words = [w for w in re.split(r"[^A-Za-z0-9&]+", run.group(0)) if w]
        low = [w.lower() for w in words]
        if not low or all(w in NAME_SUFFIX_ONLY for w in low):
            continue                       # a bare suffix ("Inc.", "LLC") belongs to the name before
        if not any(w in NAME_DESIGNATOR for w in low):
            continue                       # not shaped like a company name
        phrase = _name_norm(run.group(0))
        if not phrase or any(phrase in a for a in allowed) or phrase in reported:
            continue
        errors.append("%s names %r, which is not one of the kept account rows (nor the employer "
                      "or an integration partner): use a name exactly as the fetched company, or "
                      "drop it" % (where, " ".join(words)))
    return errors


def ground_gtm(cand, rows, meta=None, company=""):
    """Ground one gtm_signal candidate in the fetched rows. Returns (errors, values, extra).

    `values` is the placeholder table (counts computed here, never by the model); `extra` carries
    the parsed rows so the caller can report what was found. Every check is deterministic:
    an evidence URL must exist in the store, the account must be that row's company, the trigger
    must describe that row's title, the posted date must be the fetched one and inside the window,
    the ATS column must be the ATS the URL reveals, and the acct-<n> source must quote the stored
    excerpt exactly.
    """
    errors, extra = [], {"rows": [], "multi": {}, "partners": []}
    if not rows:
        return (["the signal store for this token is empty: run `python pow.py signals ...` "
                 "before writing the candidate"], values_none(), extra)
    by_url, by_key, index_of = {}, {}, {}
    for i, r in enumerate(rows, 1):
        u = _as_text(r.get("url"))
        if not u:
            continue
        by_url.setdefault(u, r)
        by_key.setdefault(P.url_key(u), r)
        index_of.setdefault(u, i)
        index_of.setdefault(P.url_key(u), i)
    roles = {}
    fetched, companies = 0, set()
    for r in rows:
        c = S.norm_text(r.get("company") or "")
        if _as_text(r.get("url")):
            fetched += 1
        if c:
            roles[c] = roles.get(c, 0) + 1
            companies.add(c)

    deliverable = cand.get("deliverable")
    files = deliverable.get("files") if isinstance(deliverable, dict) else None
    first = files[0] if isinstance(files, list) and files and isinstance(files[0], dict) else None
    if not first:
        errors.append("deliverable.files[0] must be accounts.csv: one row per account, header "
                      + ",".join(ACCOUNTS_HEADER))
        return errors, values_none(), extra
    if _as_text(first.get("path")) not in ("accounts.csv", "accounts.csv/"):
        errors.append("deliverable.files[0].path must be accounts.csv "
                      "(the page renders that file as the table)")
    if _as_text(first.get("language")).lower() != "csv":
        errors.append("deliverable.files[0].language must be csv")
    text = first.get("content") if isinstance(first.get("content"), str) else ""
    try:
        table = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
    except csv.Error as ex:
        errors.append("accounts.csv is not parseable CSV: %s" % ex)
        return errors, values_none(), extra
    table = [r for r in table if any((c or "").strip() for c in r)]
    if not table:
        errors.append("accounts.csv is empty: a header line plus one row per account")
        return errors, values_none(), extra
    header = [_as_text(c) for c in table[0]]
    if header != ACCOUNTS_HEADER:
        errors.append("accounts.csv header must be exactly %s (got %s)"
                      % (",".join(ACCOUNTS_HEADER), ",".join(header) or "(empty)"))
        return errors, values_none(), extra

    body = table[1:]
    if len(body) < GTM_MIN_ACCOUNTS:
        errors.append("only %d account row(s) in accounts.csv; at least %d are needed"
                      % (len(body), GTM_MIN_ACCOUNTS))
    if len(body) > GTM_MAX_ACCOUNTS:
        errors.append("accounts.csv has %d rows (max %d): keep the strongest %d"
                      % (len(body), GTM_MAX_ACCOUNTS, GTM_MAX_ACCOUNTS))

    seen, window_days, chosen = {}, 0, []
    for i, raw in enumerate(body, 1):
        where = "accounts.csv row %d" % i
        row = [_as_text(c) for c in raw]
        if len(row) != len(ACCOUNTS_HEADER):
            errors.append("%s has %d column(s); the header defines %d"
                          % (where, len(row), len(ACCOUNTS_HEADER)))
            continue
        account, trigger, evidence, detail, posted, ats = row
        if not 2 <= len(account) <= 120:
            errors.append("%s account must be 2..120 characters (got %d)"
                          % (where, len(account)))
        if not 3 <= len(trigger) <= 160:
            errors.append("%s trigger must be 3..160 characters (got %d)"
                          % (where, len(trigger)))
        stored = by_url.get(evidence) or by_key.get(P.url_key(evidence))
        if stored is None:
            errors.append("%s evidence %r is not in this token's signal store: rows must come "
                          "from `python pow.py signals`, never from memory"
                          % (where, evidence[:120]))
            continue
        company = _as_text(stored.get("company"))
        title = _as_text(stored.get("title"))
        if S.norm_text(account) != S.norm_text(company):
            errors.append("%s account %r is not the fetched company %r for that posting"
                          % (where, account[:80], company[:80]))
        if not _trigger_matches(trigger, title):
            errors.append("%s trigger %r does not describe the fetched posting title %r: use the "
                          "posting's own title words" % (where, trigger[:80], title[:80]))
        want_date = _posted_date(stored)
        if not want_date:
            errors.append("%s: the fetched posting has no usable date, so it cannot be a dated row"
                          % where)
        elif posted != want_date:
            errors.append("%s posted %r must be the fetched date %r" % (where, posted, want_date))
        try:
            wd = int(stored.get("window_days") or 0)
        except (TypeError, ValueError):
            wd = 0
        if not wd:
            wd = int((meta or {}).get("window_days") or 0) or SIGNAL_WINDOW_DEFAULT
        window_days = max(window_days, wd)
        fdate, pdate = _date(_fetched_date(stored, meta)), _date(want_date)
        age = None
        if fdate and pdate:
            age = (fdate - pdate).days
            if age < 0:
                errors.append("%s posted date %s is after the fetch date %s"
                              % (where, want_date, fdate.isoformat()))
            elif age > wd:
                errors.append("%s posted %s is %d days old: outside the %d-day window the fetch "
                              "used" % (where, want_date, age, wd))
        guess = _as_text(stored.get("ats")) or ats_guess(evidence)
        if ats.lower() != guess.lower():
            errors.append("%s ats %r must be %r: the ATS is derived from the posting URL, not "
                          "from the employer's partner list" % (where, ats, guess))
        company_key = S.norm_text(account)
        if company_key in seen:
            errors.append("%s repeats %r: one row per company (put extra roles in detail)"
                          % (where, account[:60]))
        seen[company_key] = i
        n_roles = roles.get(company_key, 0)
        claim = re.search(r"(\d+)\s+open\s+roles?", detail, re.I)
        if claim and int(claim.group(1)) != n_roles:
            errors.append("%s detail claims %s open roles but the fetch returned %d for %s"
                          % (where, claim.group(1), n_roles, account[:60]))
        elif n_roles > 1 and not claim:
            errors.append("%s: the fetch returned %d postings for %s, so detail must say "
                          "\"%d open roles\"" % (where, n_roles, account[:60], n_roles))
        if len(detail) > 200:
            errors.append("%s detail must be at most 200 characters" % where)
        chosen.append({"account": account, "evidence": evidence, "posted": want_date,
                       "ats": ats.lower(), "excerpt": _as_text(stored.get("excerpt")),
                       "roles": n_roles, "age": age,
                       "index": index_of.get(evidence) or
                       index_of.get(P.url_key(evidence)) or 0})

    # Sources: 1..3 employer pages (buyers + partners) and one acct-<n> per row, where <n> is the
    # posting's position in the signal store: `pow.py signals --emit-sources` prints them ready to
    # copy, so the excerpt and its hash are never retyped by hand.
    sources = cand.get("sources") if isinstance(cand.get("sources"), list) else []
    acct, employer = {}, []
    for src in sources:
        if not isinstance(src, dict):
            continue
        sid = _as_text(src.get("id"))
        if sid.startswith("acct-"):
            acct[sid] = src
        else:
            employer.append(src)
    if not 1 <= len(employer) <= GTM_PARTNER_SOURCES:
        errors.append("sources must include 1..%d employer pages (their excerpts define the "
                      "buyer list and the ATS partners); found %d"
                      % (GTM_PARTNER_SOURCES, len(employer)))
    for i, ch in enumerate(chosen, 1):
        want_id = "acct-%d" % ch["index"]
        src = acct.get(want_id)
        if not src:
            errors.append("accounts.csv row %d needs source %s (the fetched posting at that URL; "
                          "`pow.py signals --emit-sources` prints it verbatim)"
                          % (i, want_id))
            continue
        if _as_text(src.get("url")) != ch["evidence"]:
            errors.append("%s url must be the accounts.csv row's evidence URL" % want_id)
        if _as_text(src.get("excerpt")) != ch["excerpt"]:
            errors.append("%s excerpt must be the fetched excerpt exactly: %r"
                          % (want_id, ch["excerpt"][:120]))
    wanted = {"acct-%d" % ch["index"] for ch in chosen}
    unused = sorted(set(acct) - wanted)
    if unused:
        errors.append("unused account source(s): %s (one acct-<n> per accounts.csv row)"
                      % ", ".join(unused[:5]))
    missing = sorted(wanted - set(acct))
    if missing:
        errors.append("missing account source(s): %s" % ", ".join(missing[:5]))

    partner_text = " ".join(_as_text(s.get("excerpt")) for s in employer).lower()
    partners = [v for v in ATS_VENDORS
                if re.search(r"\b%s\b" % re.escape(v), partner_text)]
    extra["partners"] = partners
    extra["rows"] = chosen
    extra["companies"] = len(companies)
    extra["multi"] = {k: v for k, v in roles.items() if v > 1}
    values = {"n_accounts": len(chosen),
              "n_multi": sum(1 for ch in chosen if ch["roles"] > 1),
              "window_days": window_days or SIGNAL_WINDOW_DEFAULT,
              # The funnel: what the fetch actually produced versus what survived qualification.
              "n_fetched": fetched,
              "n_companies": len(companies)}
    # Recency is the strongest hook a list like this has, and it is computable: the age of the
    # newest kept posting and how many kept rows are inside the last three days.
    ages = [ch["age"] for ch in chosen if isinstance(ch.get("age"), int)]
    if ages:
        values["freshest_days"] = min(ages)
        values["n_recent_3d"] = sum(1 for a in ages if a <= 3)
    if partners:
        # A single match is not a selling point, and the postings are mostly LinkedIn URLs where
        # the ATS is invisible at all: the placeholder exists only from two rows up.
        n_match = sum(1 for ch in chosen if ch["ats"] in partners)
        if n_match >= 2:
            values["n_ats_match"] = n_match
    keywords = signal_titles(meta)
    if keywords:
        # The keywords come from the fetch ledger, never from the model: the method line states
        # the search that produced the pack, so it has to be the search that ran.
        values["titles"] = ", ".join(keywords)

    # One finding has to be the method: the replicable rule (trigger + funnel + keywords), not a
    # bare list. It is identified deterministically by the placeholders it must carry.
    claims = [str(f.get("claim") or "") for f in (cand.get("findings") or [])
              if isinstance(f, dict)]
    methods = [c for c in claims if all(t in c for t in ("{n_fetched}", "{n_companies}",
                                                         "{titles}"))]
    if len(methods) != 1:
        errors.append("gtm_signal needs exactly one METHOD finding: a single claim that states "
                      "the filter and the funnel with {n_fetched} postings retrieved, "
                      "{n_companies} distinct companies, {n_accounts} kept, {titles} as the "
                      "title keywords used and the {window_days}-day window (found %d such "
                      "finding(s))" % len(methods))
    # A finding is a positive, checkable read of the rows. "None of the URLs reveals…" is what
    # this is not: absence belongs in limitations, where the page says what it cannot see.
    for i, f in enumerate(cand.get("findings") or []):
        if not isinstance(f, dict):
            continue
        claim = str(f.get("claim") or "")
        for pat in NON_FINDING_RE:
            hit = pat.search(claim)
            if hit:
                errors.append("findings[%d].claim states an absence (%r), which is not a finding: "
                              "state what the rows show (recency, role mix, seniority, "
                              "multi-location operators) and put what is missing in limitations"
                              % (i, hit.group(0)))
                break
    # A name on the page has to be an account the page shows.
    kept_names = [_as_text(ch["account"]) for ch in chosen]
    store_names = [_as_text(r.get("company")) for r in rows]
    for field in ("title", "thesis", "email_line"):
        errors.extend(check_names(cand.get(field), field, kept_names, store_names, company))
    return errors, values, extra


def values_none():
    """Placeholder table for a candidate whose rows could not be grounded at all."""
    return {"window_days": SIGNAL_WINDOW_DEFAULT}


def resolve_placeholders(cand, values, errors):
    """Substitute the computed counts (and the fetched keyword list) for their placeholders.

    Unknown placeholders and, in the headline fields and finding claims, bare two-or-more-digit
    numbers are refused: every figure on the page is then a count Python computed from the
    validated rows, and the method line quotes the search that actually ran. In `title`, `thesis`
    and `email_line` a count that resolves to 0 or 1 is refused too: "1 accounts" and "repeat
    openings at 1" are not hooks, so such a statistic has no business in the headline.
    """
    def sub(text, where, strict, headline=False):
        if not isinstance(text, str):
            return text

        def rep(m):
            name = m.group(1)
            if name not in values:
                errors.append("%s uses {%s}, which is unavailable for this artifact (available: "
                              "%s)" % (where, name,
                                       ", ".join("{%s}" % k for k in sorted(values,
                                                                           key=PLACEHOLDERS
                                                                           .index))
                                       if values else "none"))
                return ""
            value = values[name]
            if headline and isinstance(value, int) and value < 2:
                errors.append("%s uses {%s}, which resolves to %d: a count that low is not a "
                              "hook, so leave the statistic out and say the same thing another "
                              "way" % (where, name, value))
                return ""
            return "%s%s%s" % (_MARK_OPEN, value, _MARK_CLOSE)

        out = PLACEHOLDER_RE.sub(rep, text)
        if strict:
            masked = re.sub("%s[^%s]*%s" % (_MARK_OPEN, _MARK_CLOSE, _MARK_CLOSE), " ", out)
            stray = sorted(set(re.findall(r"\d{2,}", masked)))
            if stray:
                errors.append("%s contains a number (%s) that no placeholder produced: use "
                              "{n_accounts}, {n_multi}, {n_fetched} or {window_days} so the "
                              "figure comes from the fetched rows"
                              % (where, ", ".join(stray[:3])))
        return re.sub("%s([^%s]*)%s" % (_MARK_OPEN, _MARK_CLOSE, _MARK_CLOSE), r"\1", out)

    out = dict(cand)
    for field in ("title", "thesis", "email_line"):
        out[field] = sub(cand.get(field), field, True, True)
    findings = cand.get("findings")
    if isinstance(findings, list):
        rows = []
        for i, f in enumerate(findings):
            if not isinstance(f, dict):
                rows.append(f)
                continue
            f = dict(f)
            f["claim"] = sub(f.get("claim"), "findings[%d].claim" % i, True)
            f["limitations"] = sub(f.get("limitations"), "findings[%d].limitations" % i, False)
            rows.append(f)
        out["findings"] = rows
    deliverable = cand.get("deliverable")
    if isinstance(deliverable, dict):
        deliverable = dict(deliverable)
        deliverable["summary"] = sub(deliverable.get("summary"), "deliverable.summary", False)
        out["deliverable"] = deliverable
    return out


# ---------------------------------------------------------------------------
# deterministic validation
# ---------------------------------------------------------------------------

def validate_artifact(art):
    """Every rule a stored or published artifact must satisfy, as a list of human sentences."""
    e = []
    if not isinstance(art, dict):
        return ["artifact is not a JSON object"]
    required = ("schema_version", "artifact_id", "posting_url", "company", "role", "recipe",
                "title", "thesis", "generated_at", "sources", "findings", "deliverable",
                "limitations", "email_line")
    missing = [k for k in required if k not in art]
    if missing:
        return ["missing key: %s" % k for k in missing]
    extra = set(art) - set(required)
    if extra:
        e.append("unknown top-level key(s): %s" % ", ".join(sorted(extra)))
    for where, text in iter_strings(art):
        try:
            text.encode("utf-8")
        except UnicodeEncodeError:
            e.append("%s contains an unpaired surrogate (not valid UTF-8)" % where)
            break
    for where, number in iter_numbers(art):
        if number != number or number in (float("inf"), float("-inf")):
            e.append("%s is not a finite number (JSON has no NaN/Infinity)" % where)
    if art["schema_version"] != 1:
        e.append("schema_version must be 1")
    recipe = art["recipe"]
    if recipe not in P.RECIPES:
        e.append("recipe must be one of: %s" % ", ".join(P.RECIPES))
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_-]{15,63}$", str(art["artifact_id"])):
        e.append("artifact_id must be 16..64 characters of [A-Za-z0-9_-]")
    if not http_url_ok(art["posting_url"]):
        e.append("posting_url must be a public http(s) URL")
    _text(e, "company", art["company"], 1, 160)
    _text(e, "role", art["role"], 1, 160)
    _text(e, "title", art["title"], 8, 120)
    _text(e, "thesis", art["thesis"], 40, 700)
    if _text(e, "email_line", art["email_line"], 20, 200):
        line = art["email_line"]
        if line != line.lower():
            e.append("email_line must be lowercase")
        if line.strip() != line:
            e.append("email_line must not have leading/trailing whitespace")
        if "\n" in line or "\r" in line:
            e.append("email_line must be a single line")
        for banned, why in (("!", "exclamation marks"), ("@", "email addresses"),
                            ("<", "angle brackets"), (">", "angle brackets")):
            if banned in line:
                e.append("email_line must not contain %s" % why)
        if re.search(r"(https?://|www\.)", line):
            e.append("email_line must not contain a URL (the host appends the public page URL)")
    if not iso_ok(art["generated_at"]):
        e.append("generated_at must be ISO 8601 in UTC (…Z)")

    sources = art["sources"]
    source_ids, source_urls = set(), set()
    # gtm_signal cites one source per account row on top of the employer pages, so its cap is
    # higher than the generic one; everything else stays at LIMITS["sources"].
    sources_cap = GTM_MAX_ACCOUNTS + GTM_PARTNER_SOURCES if recipe == "gtm_signal" \
        else LIMITS["sources"]
    if not isinstance(sources, list) or not 1 <= len(sources) <= sources_cap:
        e.append("sources must be a list of 1..%d entries" % sources_cap)
        sources = []
    for i, src in enumerate(sources):
        where = "sources[%d]" % i
        if not isinstance(src, dict):
            e.append(where + " must be an object")
            continue
        sid = src.get("id")
        if not isinstance(sid, str) or not re.match(r"^[A-Za-z0-9_.\-]{1,40}$", sid):
            e.append(where + ".id must be 1..40 characters of [A-Za-z0-9_.-]")
        elif sid in source_ids:
            e.append(where + ".id is a duplicate: %s" % sid)
        else:
            source_ids.add(sid)
        if not http_url_ok(src.get("url")):
            e.append(where + ".url must be a public http(s) URL")
        else:
            source_urls.add(src["url"].strip())
        if not iso_ok(src.get("retrieved_at")):
            e.append(where + ".retrieved_at must be ISO 8601 with a timezone")
        if not isinstance(src.get("sha256"), str) or \
                not re.match(r"^[0-9a-f]{64}$", src["sha256"]):
            e.append(where + ".sha256 must be 64 lowercase hex characters")
        elif isinstance(src.get("excerpt"), str):
            # The hash is of the excerpt as written: recomputable here, so it cannot be invented.
            digest = hashlib.sha256(src["excerpt"].encode("utf-8")).hexdigest()
            if digest != src["sha256"]:
                e.append(where + ".sha256 is not the SHA-256 of this excerpt (recomputed here)")
        _text(e, where + ".excerpt", src.get("excerpt"), 20, 1200)

    findings = art["findings"]
    if not isinstance(findings, list) or not 1 <= len(findings) <= LIMITS["findings"]:
        e.append("findings must be a list of 1..%d entries" % LIMITS["findings"])
        findings = []
    seen_findings = set()
    for i, finding in enumerate(findings):
        where = "findings[%d]" % i
        if not isinstance(finding, dict):
            e.append(where + " must be an object")
            continue
        fid = finding.get("id")
        if not isinstance(fid, str) or not 1 <= len(fid) <= 40:
            e.append(where + ".id must be a string of 1..40 characters")
        elif fid in seen_findings:
            e.append(where + ".id is a duplicate: %s" % fid)
        else:
            seen_findings.add(fid)
        _text(e, where + ".claim", finding.get("claim"), 20, 600)
        refs = finding.get("evidence_ids")
        if not isinstance(refs, list) or not refs:
            e.append(where + ".evidence_ids must be a non-empty list of source ids")
        else:
            for ref in refs:
                if ref not in source_ids:
                    e.append("%s.evidence_ids references unknown source: %r" % (where, ref))
        if not isinstance(finding.get("limitations"), str) or \
                len(finding["limitations"]) > 300:
            e.append(where + ".limitations must be a string of at most 300 characters "
                     "(empty is fine)")

    deliverable = art["deliverable"]
    if not isinstance(deliverable, dict):
        e.append("deliverable must be an object")
        deliverable = {}
    _text(e, "deliverable.summary", deliverable.get("summary"), 40, 1400)
    files = deliverable.get("files") or []
    if not isinstance(files, list) or len(files) > LIMITS["files"]:
        e.append("deliverable.files must be a list of at most %d entries" % LIMITS["files"])
        files = []
    paths, total_bytes = set(), 0
    for i, f in enumerate(files):
        where = "deliverable.files[%d]" % i
        if not isinstance(f, dict):
            e.append(where + " must be an object")
            continue
        path = f.get("path")
        if not isinstance(path, str) or not SAFE_PATH.match(path) or path.startswith("/") \
                or ".." in path:
            e.append(where + ".path must be a relative path of [A-Za-z0-9._/-] without ..")
        elif path in paths:
            e.append(where + ".path is a duplicate: %s" % path)
        else:
            paths.add(path)
        _text(e, where + ".language", f.get("language"), 2, 24)
        content = f.get("content")
        if not isinstance(content, str) or not content.strip():
            e.append(where + ".content must be a non-empty string")
        else:
            size = len(content.encode("utf-8"))
            if size > LIMITS["file_bytes"]:
                e.append("%s.content is %d bytes (max %d)"
                         % (where, size, LIMITS["file_bytes"]))
            total_bytes += size
    if total_bytes > LIMITS["total_bytes"]:
        e.append("deliverable.files total %d bytes (max %d)"
                 % (total_bytes, LIMITS["total_bytes"]))

    results = deliverable.get("results") or []
    if not isinstance(results, list) or len(results) > LIMITS["results"]:
        e.append("deliverable.results must be a list of at most %d entries" % LIMITS["results"])
        results = []
    for i, r in enumerate(results):
        where = "deliverable.results[%d]" % i
        if not isinstance(r, dict):
            e.append(where + " must be an object")
            continue
        name = r.get("name")
        _text(e, where + ".name", name, 1, 200)
        for field in ("input", "expected"):
            if not isinstance(r.get(field), str) or len(r.get(field)) > 1000:
                e.append("%s.%s must be a string of at most 1000 characters" % (where, field))
        observed = r.get("observed")
        if not isinstance(observed, str) or len(observed) > 2000:
            e.append(where + ".observed must be a string of at most 2000 characters")
        status = r.get("status")
        if status not in ("passed", "failed", "not_run"):
            e.append(where + ".status must be passed, failed or not_run")
        elif status != "not_run" and not (observed or "").strip():
            e.append("%s: status %r needs an observed value" % (where, status))
        if status == "passed" and isinstance(name, str) and \
                not name.startswith(("exec:", "eval:")):
            e.append("%s: a result may only be `passed` when pow.py executed it "
                     "(name prefixed exec: or eval:)" % where)
        refs = r.get("evidence_ids")
        if refs is None:
            refs = []
        if not isinstance(refs, list):
            e.append(where + ".evidence_ids must be a list")
        else:
            for ref in refs:
                if ref not in source_ids:
                    e.append("%s.evidence_ids references unknown source: %r" % (where, ref))

    limitations = art["limitations"]
    if not isinstance(limitations, list) or not 1 <= len(limitations) <= 8:
        e.append("limitations must be a list of 1..8 entries")
    else:
        for i, lim in enumerate(limitations):
            if not isinstance(lim, str) or not 5 <= len(lim.strip()) <= 300:
                e.append("limitations[%d] must be 5..300 characters" % i)

    if recipe == "gtm_signal":
        if not 3 <= len(findings) <= 5:
            e.append("gtm_signal needs 3..5 evidenced findings (the insight layer over the rows)")
        if len(source_urls) < 3:
            e.append("gtm_signal needs at least 3 distinct public source URLs")
        file_paths = [f.get("path") for f in files if isinstance(f, dict)]
        if not file_paths or file_paths[0] != "accounts.csv":
            e.append("gtm_signal needs accounts.csv as deliverable.files[0]")
    elif recipe == "fde_integration":
        if len(source_urls) < 2:
            e.append("fde_integration needs at least 2 public source URLs (the docs it was "
                     "built against)")
        if not files:
            e.append("fde_integration needs at least 1 runnable file in deliverable.files")
        if not results:
            e.append("fde_integration needs at least 1 result row carrying an explicit "
                     "execution status")
    elif recipe == "ai_eval":
        if not files:
            e.append("ai_eval needs at least 1 file (the eval spec and its harness notes)")
        if not any(isinstance(r, dict) and str(r.get("name", "")).startswith("eval:")
                   and r.get("status") == "passed" for r in results):
            e.append("ai_eval needs an executed `eval:` result with status passed: pow.py runs "
                     "the built-in deterministic evaluator")
    return e


def _resume_grams():
    """Word runs from the private resume files: copied resume text must never reach a public page."""
    grams = set()
    harness = os.path.dirname(S.ROOT)
    try:
        names = [n for n in os.listdir(harness) if re.match(r"resume_.*\.txt$", n)]
    except OSError:
        return grams
    for name in names:
        try:
            with open(os.path.join(harness, name), encoding="utf-8", errors="replace") as fh:
                grams |= shingles(fh.read(40000))
        except OSError:
            continue
    return grams


def scan_publish_safety(art, private_text=""):
    """The publish projection must carry no PII, no credentials, no private files, no local paths.

    The host re-checks all of this (it 422s on unsafe content and 400s on schema breaches); doing
    it here means the operator sees the problem locally instead of after a network round trip.
    """
    e = []
    jd_grams = shingles(private_text)
    resume_grams = _resume_grams()
    for where, text in iter_strings(art):
        if not isinstance(text, str) or not text.strip():
            continue
        for m in EMAIL_RE.finditer(text):
            if not EMAIL_DOC_DOMAIN.search(m.group(0)):
                e.append("%s contains an email address (contact PII is never published)" % where)
                break
        if not where.endswith(".sha256") and _phone_shaped(URL_IN_TEXT.sub(" ", text)):
            # `sources[].sha256` is format-validated 64 lowercase hex (and must equal the excerpt
            # hash), so long digit runs there are a fingerprint, not a phone number; URLs are
            # masked because posting ids are long digit runs too.
            e.append("%s contains a phone-shaped number" % where)
        if LOCAL_PATH_RE.search(text):
            e.append("%s contains a local filesystem path" % where)
        if SECRET_RE.search(text):
            e.append("%s looks like a credential or secret" % where)
        if _overlaps(text, jd_grams):
            e.append("%s copies a 12-word run from the private job description" % where)
        if _overlaps(text, resume_grams):
            e.append("%s copies a 12-word run from the private resume" % where)
        if len(e) >= 12:
            break
    return e[:12]


# ---------------------------------------------------------------------------
# target resolution and artifact composition
# ---------------------------------------------------------------------------

def jd_text(target):
    paths = []
    if target.get("jd_file"):
        jd = target["jd_file"]
        paths.append(jd if os.path.isabs(jd) else S.path(jd))
    if target.get("key"):
        paths.append(S.path("jd", "%s.txt" % target["key"]))
    for p in paths:
        try:
            if p and os.path.isfile(p):
                with open(p, encoding="utf-8", errors="replace") as fh:
                    return fh.read(200000)
        except OSError:
            continue
    return ""


def resolve_target(conn, args):
    url = (args.url or "").strip()
    key = (args.key or "").strip()
    row = None
    if url:
        row = conn.execute("SELECT * FROM postings WHERE url_key=?",
                           (S.norm_url(url),)).fetchone()
    if row is None and key:
        row = conn.execute("SELECT * FROM postings WHERE url_key=?", (key,)).fetchone()
    if row is not None:
        exact = (url or row["url"] or row["apply_url"] or "").strip()
        return {"url_key": P.url_key(exact), "url": exact,
                "company": (args.company or row["company"] or "").strip(),
                "role": (args.role or row["title"] or "").strip(),
                "key": None,
                "jd_file": args.jd_file or row["jd_file"]}
    if not url and key:
        url = "https://" + key
    if not url:
        print("pow: give --url <posting url> (or --key <url_key>)")
        return None
    company = (args.company or "").strip()
    role = (args.role or "").strip()
    if not company or not role:
        print("pow: no posting row for %s; pass --company and --role" % url)
        return None
    return {"url_key": P.url_key(url), "url": url, "company": company, "role": role,
            "key": None, "jd_file": args.jd_file}


def artifact_fingerprint(art):
    """The host's content identity: canonical UTF-8 sorted-key JSON of the WHOLE artifact.

    Per the finalized host contract this includes `artifact_id`, so the fingerprint equals the
    local content SHA-256: an identical re-post dedupes onto the existing page, while a regenerated
    artifact (new id) is a new page. The host shape-checks `dedupe_key` and returns the same
    `sha256`; a difference is a canonicalization bug signal, never a silent variant.
    """
    return hashlib.sha256(P.canonical_bytes(art)).hexdigest()


def compose_artifact(target, recipe, cand):
    """The envelope is Python's: only the evidence fields come from the candidate."""
    art = {
        "schema_version": 1,
        "artifact_id": "",
        "posting_url": target["url"],
        "company": target["company"],
        "role": target["role"],
        "recipe": recipe,
        "title": cand.get("title"),
        "thesis": cand.get("thesis"),
        "generated_at": P.now_iso(),
        "sources": cand.get("sources") if cand.get("sources") is not None else [],
        "findings": cand.get("findings") if cand.get("findings") is not None else [],
        "deliverable": cand.get("deliverable") if cand.get("deliverable") is not None else {},
        "limitations": cand.get("limitations") if cand.get("limitations") is not None else [],
        "email_line": cand.get("email_line"),
    }
    body = {k: v for k, v in art.items() if k not in ("artifact_id", "generated_at")}
    art["artifact_id"] = P.content_id(recipe, target["url_key"], body)
    return art


def _say_errors(errors, label, say=print):
    say("invalid (%s):" % label)
    for x in errors[:12]:
        say("  - %s" % x)
    if len(errors) > 12:
        say("  … %d more" % (len(errors) - 12))
    return 1


def _reject(conn, target, recipe, cand, errors, label, say=print):
    art = cand if isinstance(cand, dict) else {"raw_candidate": cand}
    aid = art.get("artifact_id")
    if not (isinstance(aid, str) and re.match(r"^[A-Za-z0-9][A-Za-z0-9_-]{15,63}$", aid)):
        aid = P.content_id(recipe or "candidate", target["url_key"], art)
        art = dict(art, artifact_id=aid)
    try:
        blob = json.dumps(art, ensure_ascii=False)
        if len(blob) <= 2 * 1024 * 1024:
            P.save_rejected(art)
        else:
            print("pow: refused candidate too large to archive (%d chars)" % len(blob))
    except (TypeError, ValueError) as ex:
        print("pow: could not archive the rejected candidate (%s)" % ex)
    P.artifact_upsert(conn, {"artifact_id": aid, "url_key": target["url_key"], "recipe": recipe,
                             "status": "rejected", "error": "; ".join(errors)[:2000],
                             "content_sha256": None})
    _say_errors(errors, "%s %s" % (aid, recipe or "-"), say)
    say("  candidate: %s → state/pow/rejected/%s.json" % (label, aid))
    return 1


def record_abstain(conn, target, recipe, reason, cost=None):
    aid = P.content_id(recipe or "routing", target["url_key"], {"reason": reason})
    P.artifact_upsert(conn, {"artifact_id": aid, "url_key": target["url_key"], "recipe": recipe,
                             "status": "abstained", "error": reason})
    P.record_event(conn, kind="blocked", artifact_id=aid, url_key=target["url"],
                   details=cost)
    print("abstained: %s" % reason)
    if cost:
        # The fetch is paid for whether or not a page results, so the spend is recorded anyway.
        print("  signal fetch: %d treg call(s), $%.6f"
              % (cost.get("treg_calls") or 0, cost.get("cost_usd") or 0.0))
    return 0


def signal_cost(token=None):
    """The fetch ledger for one token as event details: {"cost_usd", "treg_calls"} or None."""
    if not token:
        return None
    meta = P.signals_meta(token)
    if not isinstance(meta, dict):
        return None
    calls = meta.get("calls") if isinstance(meta.get("calls"), list) else []
    try:
        spent = round(float(meta.get("cost_usd") or 0.0), 6)
    except (TypeError, ValueError):
        spent = 0.0
    if not calls and not spent:
        return None
    return {"cost_usd": spent, "treg_calls": len(calls)}


def ground_gtm_candidate(conn, target, cand, label, token, signals, dry_run, loud):
    """Ground a gtm_signal candidate and resolve its placeholders.

    The account table is authored here (`rebuild_gtm_rows`): the candidate supplies the posting
    URLs it chose and Python fills and quotes every other cell from the fetch.

    Returns (candidate, exit_code). A None code means "carry on with this candidate"; any other
    value is the command's result: below the account minimum the honest outcome is an abstention
    (the fetch is paid for either way), above it a fabrication is a rejection.
    """
    path = signals_path_for(token=token, explicit=signals)
    if not token and path.endswith(".jsonl"):
        token = os.path.basename(path)[:-len(".jsonl")]
    rows, err = load_signal_rows(path)
    meta = P.signals_meta(token) if token else None
    if err:
        errors = [err + ": gtm_signal rows may only come from a fetch, so run "
                         "`python pow.py signals ...` first (or point --signals at the store)"]
        if dry_run:
            return cand, _say_errors(errors, "%s (dry run, not stored)" % label, loud)
        return cand, _reject(conn, target, "gtm_signal", cand, errors, label, loud)
    cand, table_errors, table = rebuild_gtm_rows(cand, rows)
    errors, values, extra = ground_gtm(cand, rows, meta, target.get("company", ""))
    errors = table_errors + errors
    fabricated = [x for x in errors if "is not in this token's signal store" in x]
    # Abstain only when the FETCH could not support the minimum: a candidate that under-delivers
    # from a store that had enough companies is a rejection, not a shrug.
    if (len(extra["rows"]) < GTM_MIN_ACCOUNTS and not fabricated
            and extra.get("companies", 0) < GTM_MIN_ACCOUNTS):
        reason = ("only %d qualified account row(s) could be grounded in the fetched postings "
                  "(an artifact needs %d); nothing was published"
                  % (len(extra["rows"]), GTM_MIN_ACCOUNTS))
        if dry_run:
            print("pow: would abstain — %s (dry run, not stored)" % reason)
            return cand, 0
        return cand, record_abstain(conn, target, "gtm_signal", reason,
                                    cost=signal_cost(token))
    if errors:
        if dry_run:
            return cand, _say_errors(errors, "%s (dry run, not stored)" % label, loud)
        return cand, _reject(conn, target, "gtm_signal", cand, errors, label, loud)
    perrs = []
    resolved = resolve_placeholders(cand, values, perrs)
    if perrs:
        if dry_run:
            return cand, _say_errors(perrs, "%s (dry run, not stored)" % label, loud)
        return cand, _reject(conn, target, "gtm_signal", cand, perrs, label, loud)
    if not dry_run:
        if table["rows"]:
            print("pow: authored accounts.csv from the fetched postings: %d row(s), one per "
                  "company, every cell computed and quoted by Python" % len(table["rows"]))
        print("pow: grounded %d account row(s) of %d fetched postings; partners: %s"
              % (values["n_accounts"], len(rows),
                 ", ".join(extra["partners"]) or "none found in the employer pages"))
    return resolved, None


def accept_candidate(conn, target, recipe, cand, label, private_text="", dry_run=False,
                     quiet=False, token=None, signals=None):
    """Turn one candidate into a validated artifact, or refuse it with reasons. Returns an exit code."""
    say = (lambda *a, **k: None) if quiet else print

    def loud(msg):
        print(msg)

    if not isinstance(cand, dict):
        return _reject(conn, target, recipe, cand, ["candidate is not a JSON object"],
                       label, loud)
    unknown = set(cand) - set(CANDIDATE_KEYS) - {"abstained", "reason", "recipe", "schema_version"}
    if unknown:
        return _reject(conn, target, recipe, cand,
                       ["candidate has unknown key(s): %s (the envelope is Python's)"
                        % ", ".join(sorted(unknown))], label, loud)
    if cand.get("recipe") and cand["recipe"] != recipe:
        return _reject(conn, target, recipe, cand,
                       ["candidate recipe %r does not match the routed recipe %r"
                        % (cand["recipe"], recipe)], label, loud)
    # The fetch ledger is keyed by token; --signals names the same file, so the token is its stem.
    if not token:
        inferred = signals_path_for(token=None, explicit=signals)
        if inferred.endswith(".jsonl"):
            token = os.path.basename(inferred)[:-len(".jsonl")]
    if cand.get("abstained") is True:
        reason = cand.get("reason")
        if not isinstance(reason, str) or len(reason.strip()) < 20:
            return _reject(conn, target, recipe, cand,
                           ["an abstention needs a `reason` of at least 20 characters"],
                           label, loud)
        return record_abstain(conn, target, recipe, reason.strip(), cost=signal_cost(token))

    # The signal fetch is the artifact's only source of account rows: ground before composing, so
    # nothing derived from an unfetched row can reach the envelope, the hash, or the page.
    if recipe == "gtm_signal":
        cand, code = ground_gtm_candidate(conn, target, cand, label, token, signals, dry_run,
                                          loud)
        if code is not None:
            return code

    art = compose_artifact(target, recipe, cand)
    deliverable = art["deliverable"]
    agent_rows = []
    if isinstance(deliverable, dict):
        deliverable.setdefault("files", [])
        deliverable.setdefault("results", [])
        agent_rows = [r for r in (deliverable.get("results") or []) if isinstance(r, dict)]
        deliverable["results"] = []
    errors = []
    if recipe == "ai_eval":
        spec_path, spec, err = find_eval_spec(art)
        if err:
            errors.append(err)
        else:
            timeout = float(cfg("exec_timeout_seconds", 60) or 60)
            res = run_eval_spec(spec, timeout)
            if not res.get("ok"):
                errors.append("built-in evaluator failed: %s" % res.get("error"))
            else:
                deliverable["results"] = [eval_result_entry(spec, res, spec_path)]
        if agent_rows:
            add_limitation(art, "agent-declared result rows were discarded: pow.py's built-in "
                                "evaluator is the only execution evidence for this recipe")
    elif recipe == "fde_integration":
        files = deliverable.get("files") or []
        deliverable["results"] = [{
            "name": "exec:pending", "input": "%d declared file(s)" % len(files),
            "expected": "operator-approved execution in an isolated scratch dir",
            "observed": "not executed during generation", "status": "not_run",
            "evidence_ids": []}]
        if agent_rows:
            add_limitation(art, "agent-declared result rows were discarded; `python pow.py run "
                                "<artifact_id> --execute` is the only way to produce execution "
                                "evidence")
    elif agent_rows:
        add_limitation(art, "this recipe has no executable step: agent-declared result rows "
                            "were removed")

    errors.extend(validate_artifact(art))
    if not errors:
        errors.extend(scan_publish_safety(art, private_text))
    if errors:
        if dry_run:
            return _say_errors(errors, "%s (dry run, not stored)" % label, loud)
        return _reject(conn, target, recipe, art, errors, label, loud)
    if dry_run:
        print("valid %s (%s) — not stored (dry run)" % (art["artifact_id"], recipe))
        return 0
    rel = P.save_artifact(art)
    digest = hashlib.sha256(P.canonical_bytes(art)).hexdigest()
    P.artifact_upsert(conn, {"artifact_id": art["artifact_id"], "url_key": target["url_key"],
                             "recipe": recipe, "status": "accepted", "artifact_path": rel,
                             "content_sha256": digest, "error": None})
    # Telemetry receives the EXACT posting URL (query tokens included) so a remote join can
    # identify the posting; pow_artifacts.url_key stays the normalized local join key. The signal
    # fetch's real spend rides on the same event, which is where pow_metrics reads cost from.
    P.record_event(conn, kind="generated", artifact_id=art["artifact_id"],
                   url_key=target["url"], cohort=cohort_for(P.now_iso()),
                   details=signal_cost(token))
    say("  publish: python pow.py publish %s" % art["artifact_id"])
    print("accepted %s %s: %s" % (art["artifact_id"], recipe, art["title"]))
    return 0


# ---------------------------------------------------------------------------
# the agent pass
# ---------------------------------------------------------------------------

def agent_prompt(recipe):
    shared = os.path.join(ROOT, "prompts", "pow.md")
    recipe_file = os.path.join(ROOT, "prompts", "pow-%s.md" % recipe)
    text = open(shared, encoding="utf-8").read()
    if os.path.exists(recipe_file):
        text += "\n\n---\n\n" + open(recipe_file, encoding="utf-8").read()
    return text


def acquire_agent_lock():
    """Non-blocking lock so two pow.py runs cannot drive two omp passes at once.

    Same primitive as daily.py's lock (msvcrt on Windows, flock elsewhere), scoped to state/pow.
    Returns the open handle, or None when another generation holds it.
    """
    P.ensure_dirs()
    fh = open(P.pow_dir(".lock"), "a+")
    try:
        try:
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except ImportError:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except OSError:
        fh.close()
        return None


def agent_budget():
    """The dollar cap for one omp pass: the only job-level limit on the agent."""
    try:
        return max(0.0, float(cfg("agent_budget_usd", 2.0) or 0.0))
    except (TypeError, ValueError):
        return 2.0


def agent_command(recipe, budget=None):
    """The omp argv for one artifact pass: `--max-cost` is the cap, there is no wall-clock limit.

    `omp --max-cost` stops sending model requests once the session's spend reaches the value, and
    print mode then exits 3. That is the bound the operator asked for: a slow model costs less per
    minute, so an expensive pass is stopped by money rather than by the clock.
    """
    cap = agent_budget() if budget is None else float(budget)
    return ["omp", "-p", "--no-title", "--cwd", ROOT,
            "--config", os.path.join(ROOT, "omp-pipeline.yml"),
            "--max-cost=%.2f" % cap, agent_prompt(recipe)]


def run_agent(recipe):
    """One budget-capped omp pass. Metered: the caller asked for it explicitly.

    Returns omp's exit code. 0 = finished, 3 = `agent_budget_usd` reached (print mode's own exit
    code for the cap), anything else = the pass failed. On 3 the candidate on disk is whatever the
    agent had written by then; the caller validates it like any other candidate.
    """
    cmd = agent_command(recipe)
    try:
        proc = subprocess.run(cmd, cwd=ROOT, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except FileNotFoundError:
        print("pow: omp is not installed. Write a candidate yourself and pass --candidate FILE.")
        return -1
    except OSError as ex:
        print("pow: could not run omp (%s). Write a candidate yourself and pass --candidate FILE."
              % ex)
        return -1
    tail = _tail(proc.stdout, 2000)
    if tail:
        print(tail)
    if proc.returncode == 3:
        print("pow: agent budget reached ($%.2f of model spend); using whatever candidate it "
              "had written" % agent_budget())
    return proc.returncode


# ---------------------------------------------------------------------------
# HTTP: the TraceHire host
# ---------------------------------------------------------------------------

def _read_key(key_file=None):
    path = key_file or tracehire()[1]
    if not path:
        raise RuntimeError("no tracehire.key_file in config.json (or pass --key-file)")
    try:
        with open(path, encoding="utf-8") as fh:
            key = fh.read().strip()
    except OSError as ex:
        raise RuntimeError("cannot read the API key file %s: %s" % (path, ex))
    if not key:
        raise RuntimeError("the API key file %s is empty" % path)
    return key


def http_json(method, path, body=None, base=None, key_file=None, timeout=HTTP_TIMEOUT):
    """One request to the host. Returns {status, json|text|transport_error}; never raises on HTTP."""
    base = (base or tracehire()[0] or "").rstrip("/")
    if not base:
        raise RuntimeError("no tracehire.base in config.json (or pass --base)")
    key = _read_key(key_file)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Authorization": "Bearer " + key, "User-Agent": UA,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            code = getattr(resp, "status", 200)
    except urllib.error.HTTPError as ex:
        raw, code = ex.read(), ex.code
    except Exception as ex:  # noqa: BLE001 - transport failures are data here, not crashes
        return {"status": 0, "transport_error": str(ex)}
    text = raw[:600].decode("utf-8", "replace")
    try:
        parsed = json.loads(raw or b"{}")
    except ValueError:
        parsed = None
    return {"status": code, "json": parsed, "text": text}


def _host_error(res):
    payload = res.get("json") or {}
    detail = payload.get("details")
    parts = [str(payload.get("error") or res.get("transport_error") or (res.get("text") or "")[:200])]
    if detail:
        parts.append(json.dumps(detail)[:300])
    return " ".join(p for p in parts if p)


def _quote(value):
    return urllib.parse.quote(str(value), safe="")


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def signal_sources(rows):
    """Ready-to-copy `sources` entries for every stored posting (nothing here is model-written).

    The id is the posting's position in the store, so an accounts.csv row's evidence URL names
    exactly one acct-<n>; excerpt and sha256 come from the fetch, not from the agent's keyboard.
    """
    out = []
    for i, r in enumerate(rows, 1):
        excerpt = _as_text(r.get("excerpt"))
        out.append({"id": "acct-%d" % i,
                    "url": _as_text(r.get("url")),
                    "retrieved_at": _as_text(r.get("fetched_at")) or P.now_iso(),
                    "sha256": P.sha256_hex(excerpt),
                    "excerpt": excerpt})
    return out


def _split_csv(value):
    """Comma-separated flag value -> list (titles and filters are phrases, so never split spaces)."""
    return [p.strip() for p in (value or "").split(",") if p.strip()]


def _clamp(value, lo, hi, default):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _task_token():
    task = S.read_json(P.task_file(), None)
    if isinstance(task, dict):
        return _as_text(task.get("signals_token")) or _as_text(task.get("token"))
    return ""


def cmd_signals(args):
    """Fetch postings for a gtm_signal artifact: the one place treg is called, under a hard budget."""
    token = _as_text(getattr(args, "token", "")) or _task_token()
    if not re.match(r"^[A-Za-z0-9]{6,64}$", token):
        print("pow: no signals token. Run `python pow.py generate --url …` (it writes "
              "state/pow/task.json with the token this artifact will reuse), or pass --token.")
        return 2
    budget = float(cfg("treg_budget_usd", 0.75) or 0.0)
    meta = P.signals_meta(token)
    if not isinstance(meta, dict):
        meta = {"token": token, "started": P.now_iso(), "calls": [], "cost_usd": 0.0,
                "window_days": 0, "row_count": 0}
    meta.setdefault("calls", [])
    meta.setdefault("row_count", 0)
    meta.setdefault("window_days", 0)
    meta["cost_usd"] = round(float(meta.get("cost_usd") or 0.0), 6)

    if args.emit_sources:
        rows, err = P.load_signals(token)
        if err:
            print("pow: %s" % err)
            return 1
        print(json.dumps(signal_sources(rows), indent=1, ensure_ascii=False))
        return 0

    titles = _split_csv(args.titles)
    if not titles:
        print("pow: signals needs --titles \"driver recruiter,fleet safety manager\" "
              "(comma-separated; each one is a title keyword)")
        return 2
    days = _clamp(args.days if args.days is not None
                  else cfg("signal_window_days", SIGNAL_WINDOW_DEFAULT), 1, 90,
                  SIGNAL_WINDOW_DEFAULT)
    countries = _split_csv(args.countries) or list((S.CONFIG.get("query") or {})
                                                  .get("country_code") or ["US"])
    keywords = _split_csv(args.keywords)
    icp = _split_csv(args.icp_keywords)
    sizes = _split_csv(args.sizes)
    industries = _split_csv(args.industries)
    limit = _clamp(args.limit, 1, TREG_LIMIT_MAX, TREG_LIMIT_MAX)
    pages = _clamp(args.pages, 1, TREG_PAGES_MAX, 1)
    label = "titles=%s;days=%d" % ("|".join(titles)[:120], days)

    if args.dry_run:
        print(json.dumps([signal_payload(titles, days, countries, keywords, icp, sizes,
                                        industries, limit, None)], indent=1))
        print("dry run: %d call(s) would be made against %s (no request sent)"
              % (pages, TREG_ENDPOINT))
        return 0

    shown, added_total, cursor, stop, calls_made = [], 0, None, "", 0
    for page in range(pages):
        used = len(meta["calls"])
        left = round(budget - meta["cost_usd"], 6)
        if left <= 0:
            stop = ("budget exhausted ($%.6f of $%.4f spent in %d call(s)); stop searching and "
                    "work with the rows already stored, or abstain"
                    % (meta["cost_usd"], budget, used))
            break
        payload = signal_payload(titles, days, countries, keywords, icp, sizes, industries,
                                 limit, cursor)
        if args.fixture:
            # Offline path for `pow.py smoke`: ingest a saved response, charge what it reports.
            if not os.path.isfile(args.fixture):
                print("pow: --fixture %s does not exist" % args.fixture)
                return 2
            with open(args.fixture, encoding="utf-8") as fh:
                body = json.load(fh)
            body = body if isinstance(body, dict) else {"results": body}
            cost = float(body.get("cost_usd") or 0.0)
        else:
            res = treg_call(payload, left)
            body, cost = res["body"], res["cost_usd"]
        results = body.get("results") if isinstance(body.get("results"), list) else []
        at = P.now_iso()
        rows = [r for r in (normalize_signal(raw, at, days, label) for raw in results) if r]
        try:
            added, total = P.append_signals(token, rows)
        except ValueError as ex:
            print("pow: %s; fix or remove state/pow/signals/%s.jsonl" % (ex, token))
            return 1
        if total > TREG_MAX_ROWS:
            # Keep the store bounded: the artifact shows at most GTM_MAX_ACCOUNTS rows anyway.
            stored, err = P.load_signals(token)
            if not err:
                P.save_signals(token, stored[:TREG_MAX_ROWS])
            total = min(total, TREG_MAX_ROWS)
        meta["calls"].append({"at": at, "titles": titles, "days": days, "countries": countries,
                              "keywords": keywords, "limit": limit, "page": page + 1,
                              "rows": len(rows), "new_rows": added,
                              "cost_usd": round(cost, 6), "fixture": bool(args.fixture)})
        meta["cost_usd"] = round(meta["cost_usd"] + cost, 6)
        meta["window_days"] = max(int(meta.get("window_days") or 0), days)
        meta["row_count"] = total
        meta["endpoint"] = TREG_ENDPOINT
        P.signals_meta_save(token, meta)
        # The pool, after every call: a pack built from one broad search is thin by construction,
        # so the agent has to see the store growing and the budget shrinking to judge when another
        # search is worth it.
        pool, _perr = P.load_signals(token)
        companies_now = len({S.norm_text(r.get("company")) for r in pool
                             if _as_text(r.get("company"))})
        print("pow signals: call %d added %d new posting(s): %d in the store from %d distinct "
              "companies, $%.6f of $%.4f budget used"
              % (len(meta["calls"]), added, total, companies_now, meta["cost_usd"], budget))
        shown.extend(rows)
        added_total += added
        calls_made += 1
        cursor = body.get("cursor")
        if not cursor:
            break

    if shown:
        print("%-34s %-40s %-11s %-15s %s"
              % ("company", "title", "posted", "ats", "url"))
        for r in shown[:60]:
            print("%-34s %-40s %-11s %-15s %s"
                  % (r["company"][:34], r["title"][:40], (r["posted"] or "")[:10],
                     r["ats"] or "-", r["url"]))
        if len(shown) > 60:
            print("… %d more in the signal file" % (len(shown) - 60))
        per_company = {}
        for r in shown:
            key = r["company"]
            per_company[key] = per_company.get(key, 0) + 1
        many = sorted(((n, c) for c, n in per_company.items() if n > 1), reverse=True)
        if many:
            print("companies with more than one matching role: "
                  + ", ".join("%s (%d)" % (c, n) for n, c in many[:12]))
    print("pow signals: %d call(s) this run, %d row(s) stored for this artifact, "
          "$%.6f of $%.4f budget" % (calls_made, meta["row_count"], meta["cost_usd"], budget))
    print("  store: %s" % os.path.abspath(P.signals_file(token)).replace("\\", "/"))
    pool_rows, _perr = P.load_signals(token)
    pool_companies = len({S.norm_text(r.get("company")) for r in pool_rows
                          if _as_text(r.get("company"))})
    print("  pool: %d posting(s) from %d distinct companies (keep %d..%d of them)"
          % (len(pool_rows), pool_companies, GTM_MIN_ACCOUNTS, GTM_TARGET_ACCOUNTS))
    if pool_companies < GTM_MIN_ACCOUNTS and not stop and calls_made:
        print("  the pool is below the %d-account floor: another, differently-aimed search (title "
              "variant, other window, icp keywords) is what a pack needs" % GTM_MIN_ACCOUNTS)
    elif pool_companies < GTM_TARGET_ACCOUNTS and not stop and calls_made:
        print("  thin pool for a selective pack: another, differently-aimed search (title "
              "variant, other window, icp keywords) would widen it")
    if stop:
        print("pow signals: %s" % stop)
        return 2
    if not shown and calls_made:
        print("pow signals: the search returned no postings; widen --titles or --days, or abstain")
    return 0


def cmd_generate(args):
    conn = P.db()
    try:
        target = resolve_target(conn, args)
        if not target:
            return 2
        if not http_url_ok(target["url"]):
            print("pow: posting URL must be public http(s): %r" % target["url"])
            return 2
        private = jd_text(target)
        recipe = args.recipe
        why = "explicit --recipe"
        if not recipe:
            recipe, why = route_recipe(target["role"], private)
            if not recipe:
                if args.dry_run:
                    print("pow: routing would abstain (dry run, nothing stored): %s" % why)
                    return 0
                return record_abstain(conn, target, None, why)
        existing = P.artifact_for(conn, target["url_key"], recipe=recipe,
                                  statuses=("accepted", "published", "attached"))
        if existing and not args.force:
            print("pow: %s already has a %s artifact %s (%s)%s"
                  % (target["url_key"], recipe, existing["artifact_id"], existing["status"],
                     "" if existing["published_url"] else
                     "; publish it: python pow.py publish %s" % existing["artifact_id"]))
            return 0
        print("pow: %s | %s | %s -> %s" % (target["company"], target["role"],
                                           target["url"], recipe))
        print("pow: %s" % why)
        if args.dry_run and not args.candidate:
            print("dry run: would run one omp pass capped at $%.2f of model spend, validate the "
                  "candidate, then store it. Nothing was called or written." % agent_budget())
            return 0

        if args.candidate:
            cand = S.read_json(args.candidate, None)
            if cand is None:
                print("pow: cannot read candidate %s" % args.candidate)
                return 2
            return accept_candidate(conn, target, recipe, cand, args.candidate, private,
                                    dry_run=args.dry_run, quiet=args.quiet,
                                    signals=getattr(args, "signals", None))

        token = os.urandom(8).hex()
        cand_path = P.candidate_file(token)
        signal_path = P.signals_file(token)
        S.write_json(P.task_file(), {
            "recipe": recipe, "routing": why, "posting_url": target["url"],
            "url_key": target["url_key"], "company": target["company"],
            "role": target["role"], "jd_file": target["jd_file"] or "", "today": S.TODAY,
            "candidate_path": os.path.abspath(cand_path).replace("\\", "/"),
            # The signal fetch writes under this token; `pow.py signals` reads it back from here,
            # so the agent never has to invent or remember one.
            "signals_token": token,
            "signals_path": os.path.abspath(signal_path).replace("\\", "/"),
            "signal_window_days": int(cfg("signal_window_days", SIGNAL_WINDOW_DEFAULT) or
                                      SIGNAL_WINDOW_DEFAULT),
            "treg_budget_usd": float(cfg("treg_budget_usd", 0.75) or 0.75),
            "agent_budget_usd": agent_budget(),
        })
        lock = acquire_agent_lock()
        if lock is None:
            print("pow: another pow.py generation holds state/pow/.lock; "
                  "not starting a second agent pass")
            return 3
        print("pow: one omp pass, agent budget $%.2f (--max-cost; metered by your model plan)"
              % agent_budget())
        try:
            rc = run_agent(recipe)
        finally:
            lock.close()
        if rc not in (0, 3):
            P.artifact_upsert(conn, {
                "artifact_id": P.content_id(recipe, target["url_key"],
                                            {"failed": "agent", "token": token}),
                "url_key": target["url_key"], "recipe": recipe, "status": "rejected",
                "error": "the generation agent failed; no candidate was read"})
            return 1
        cand = S.read_json(cand_path, None)
        if cand is None:
            P.artifact_upsert(conn, {
                "artifact_id": P.content_id(recipe, target["url_key"],
                                            {"failed": "no candidate", "token": token}),
                "url_key": target["url_key"], "recipe": recipe, "status": "rejected",
                "error": "the agent wrote no candidate at %s"
                         % os.path.relpath(cand_path, S.STATE)})
            print("pow: the agent wrote no candidate at %s" % cand_path)
            return 1
        P.save_candidate(token, cand)
        return accept_candidate(conn, target, recipe, cand,
                                os.path.relpath(cand_path, S.STATE), private,
                                dry_run=args.dry_run, quiet=args.quiet, token=token)
    finally:
        conn.close()


def cmd_validate(args):
    obj = S.read_json(args.file, None)
    if obj is None:
        print("pow: cannot read %s" % args.file)
        return 2
    if isinstance(obj, dict) and all(k in obj for k in
                                     ("schema_version", "artifact_id", "posting_url", "recipe")):
        art = obj
    else:
        missing = [flag for flag, value in (("--url", args.url), ("--company", args.company),
                                            ("--role", args.role), ("--recipe", args.recipe))
                   if not value]
        if missing:
            print("pow: a bare candidate needs %s to be composed first" % ", ".join(missing))
            return 2
        target = {"url": args.url, "url_key": P.url_key(args.url),
                  "company": args.company, "role": args.role, "jd_file": args.jd_file}
        if args.recipe == "gtm_signal":
            # A bare candidate has not been grounded yet: do it here, with the same rules the
            # accepting path uses, so `validate` never says "valid" about an unfetched row.
            path = signals_path_for(explicit=getattr(args, "signals", None))
            rows, err = load_signal_rows(path)
            if err:
                print("pow: %s\n  gtm_signal rows may only come from a fetch: run "
                      "`python pow.py signals ...`, or pass --signals FILE" % err)
                return 1
            obj, table_errors, _table = rebuild_gtm_rows(obj, rows)
            gerrs, values, _extra = ground_gtm(obj, rows, None, args.company)
            perrs = []
            resolved = resolve_placeholders(obj, values, perrs)
            errors = table_errors + gerrs + perrs
            if errors:
                return _say_errors(errors, args.file)
            obj = resolved
        art = compose_artifact(target, args.recipe, obj)
    errors = validate_artifact(art)
    if not errors:
        errors = scan_publish_safety(art, jd_text({"jd_file": args.jd_file}))
    if errors:
        return _say_errors(errors, args.file)
    print("valid: %s (%s) %d source(s), %d finding(s)"
          % (art["artifact_id"], art["recipe"], len(art["sources"]), len(art["findings"])))
    return 0


def cmd_run(args):
    conn = P.db()
    try:
        row, art = None, None
        if os.path.isfile(args.target):
            art = S.read_json(args.target, None)
            if isinstance(art, dict) and art.get("artifact_id"):
                row = P.artifact_get(conn, art["artifact_id"])
        else:
            row = P.artifact_get(conn, args.target)
            if row:
                art = P.load_artifact(args.target)
        if not isinstance(art, dict):
            print("pow: no artifact at %s" % args.target)
            return 2
        if row and row["status"] in ("published", "attached", "revoked"):
            print("pow: refusing to modify a %s artifact; generate a new one to change content"
                  % row["status"])
            return 2
        recipe = art.get("recipe")
        deliverable = art.get("deliverable") or {}
        files = deliverable.get("files") or []
        timeout = float(cfg("exec_timeout_seconds", 60) or 60)
        if recipe == "ai_eval":
            spec_path, spec, err = find_eval_spec(art)
            if err:
                print("pow: %s" % err)
                return 1
            res = run_eval_spec(spec, timeout)
            if not res.get("ok"):
                print("pow: built-in evaluator failed: %s" % res.get("error"))
                return 1
            deliverable["results"] = [eval_result_entry(spec, res, spec_path)]
            print("%s -> %s" % (deliverable["results"][0]["name"],
                                deliverable["results"][0]["observed"]))
        elif recipe == "fde_integration":
            if not args.execute:
                print("pow: nothing executed. `python pow.py run %s --execute` runs the declared "
                      "harness file(s) in a scratch dir with a %ss timeout: untrusted code, "
                      "your call." % (args.target, timeout))
                return 0
            print("pow: running the artifact's own code (scratch dir under state/pow/tmp, "
                  "minimal env, %ss timeout)" % timeout)
            deliverable["results"] = execute_artifact_files(files, timeout)
            for r in deliverable["results"]:
                print("  %-8s %-28s %s" % (r["status"], r["name"], r["observed"][:160]))
        else:
            print("pow: recipe %s has no executable step; run --execute does nothing" % recipe)
            return 0
        art["deliverable"] = deliverable
        errors = validate_artifact(art)
        if errors:
            return _say_errors(errors, "re-run")
        return _store_rerun(conn, row, args.target, art)
    finally:
        conn.close()


def _store_rerun(conn, row, target, art):
    if row:
        rel = P.save_artifact(art)
        P.artifact_set(conn, art["artifact_id"], artifact_path=rel,
                       content_sha256=hashlib.sha256(P.canonical_bytes(art)).hexdigest(),
                       error=None)
    else:
        S.write_json(os.path.abspath(target), art)
    print("pow: updated %s (re-publish it, or keep it local)" % art["artifact_id"])
    return 0


def cmd_publish(args):
    conn = P.db()
    try:
        if not cfg("publish_enabled", False):
            print("pow: publishing is disabled (set pow.publish_enabled true in config.json "
                  "to allow the explicit publish command). Nothing was sent.")
            return 2
        row = P.artifact_get(conn, args.artifact_id)
        if not row:
            print("pow: unknown artifact %s" % args.artifact_id)
            return 2
        if row["status"] == "revoked":
            print("pow: %s was revoked; generate a new artifact instead" % args.artifact_id)
            return 2
        art = P.load_artifact(args.artifact_id)
        if not isinstance(art, dict):
            print("pow: artifact file missing (%s); regenerate" % row["artifact_path"])
            return 2
        errors = validate_artifact(art) or scan_publish_safety(art, "")
        if errors:
            return _say_errors(errors, "publish %s" % args.artifact_id)
        # The local canonical hash is the content identity, and it is the dedupe key the host
        # dedupes on. The host may report a different sha256 if its canonicalization differs:
        # that is noted, never written over the local identity.
        local = hashlib.sha256(P.canonical_bytes(art)).hexdigest()
        dedupe = artifact_fingerprint(art)
        if row["status"] in ("published", "attached") and row["content_sha256"] == local:
            print("already published: %s %s" % (args.artifact_id, row["published_url"]))
            return 0
        res = http_json("POST", "/api/v1/pow/artifacts",
                        {"artifact": art, "dedupe_key": dedupe},
                        base=args.base, key_file=args.key_file)
        payload = res.get("json") or {}
        code = res.get("status")
        if code in (200, 201) and payload.get("id"):
            rid = str(payload["id"])
            url = payload.get("url") or ""
            host_sha = str(payload.get("sha256") or "")
            if not host_sha or host_sha != dedupe:
                # The host echoes the dedupe_key it computed. A missing or different value means
                # the page we just created is not the artifact we validated: do not adopt it, do
                # not mark the row published (so it stays unattachable), and do not delete the
                # remote page (that external effect is uncertain and the operator must decide).
                note = ("publish http %s returned id %s but sha256 %r != dedupe_key %s; "
                        "remote page may exist (uncertain external effect), local row left %s"
                        % (code, rid, host_sha or "<missing>", dedupe, row["status"]))
                P.artifact_set(conn, args.artifact_id, error=note[:2000])
                P.record_event(conn, kind="blocked", artifact_id=args.artifact_id,
                               url_key=art["posting_url"])
                print("publish FAILED verification: host sha256 %r does not match the canonical "
                      "dedupe_key %s." % (host_sha or "<missing>", dedupe))
                print("  the remote page (%s %s) may exist; it was NOT deleted and the local row "
                      "is still %s, so it is not attachable. Inspect the host, then republish or "
                      "revoke by hand." % (rid, url or "-", row["status"]))
                return 1
            P.artifact_set(conn, args.artifact_id, status="published", remote_id=rid,
                           published_url=url, content_sha256=local, error=None)
            P.record_event(conn, kind="published", artifact_id=args.artifact_id,
                           url_key=art["posting_url"], cohort=cohort_for(row["created_at"]),
                           evidence_ref=url or rid)
            print("published %s → %s" % (args.artifact_id, url))
            return 0
        err = _host_error(res)
        P.artifact_set(conn, args.artifact_id,
                       error="publish %s: %s" % (code or "transport", err))
        print("publish failed (%s): %s" % (code or "transport", err))
        if code == 409 and "content_conflict" in err:
            print("the host already has this artifact_id with different content: regenerate to "
                  "mint a new id (the host never overwrites)")
        elif code == 409 and "revoked" in err:
            P.artifact_set(conn, args.artifact_id, status="revoked")
        elif code == 422:
            print("the host rejected the content as unsafe: fix the projection, do not retry "
                  "as is")
        elif code == 403:
            print("the host deployment has publishing switched off (POW_PUBLISH_ENABLED); "
                  "nothing to fix locally")
        P.record_event(conn, kind="blocked", artifact_id=args.artifact_id,
                       url_key=(P.load_artifact(args.artifact_id) or {}).get("posting_url")
                       or row["url_key"])
        return 1
    finally:
        conn.close()


def cmd_attach(args):
    conn = P.db()
    try:
        if not cfg("attach_enabled", False):
            print("pow: attaching is disabled (set pow.attach_enabled true in config.json to "
                  "allow the explicit attach command). No draft was read or written.")
            return 2
        row = P.artifact_get(conn, args.artifact_id)
        if not row:
            print("pow: unknown artifact %s" % args.artifact_id)
            return 2
        if row["status"] not in ("published", "attached") or not row["remote_id"]:
            print("pow: %s is %s, publish it first" % (args.artifact_id, row["status"]))
            return 2
        run = conn.execute("SELECT * FROM runs WHERE run_id=?", (args.run,)).fetchone()
        if not run:
            print("pow: unknown run id %s; attach needs the local runs row so the posting can be "
                  "matched" % args.run)
            return 2
        if P.url_key(run["url"]) != row["url_key"]:
            print("pow: refusing to attach: run %s belongs to %s, the artifact is for %s"
                  % (args.run, P.url_key(run["url"]), row["url_key"]))
            return 2
        res = http_json("POST", "/api/v1/pow/artifacts/%s/attach" % _quote(row["remote_id"]),
                        {"run_id": args.run}, base=args.base, key_file=args.key_file)
        artifact = P.load_artifact(args.artifact_id) or {}
        exact_url = artifact.get("posting_url") or row["url_key"]
        payload = res.get("json") or {}
        if res.get("status") == 200:
            draft = str(payload.get("draft_id") or row["gmail_draft_id"] or run["draft_id"] or "")
            attached = bool(payload.get("attached"))
            already = bool(payload.get("already_attached"))
            fields = {"gmail_draft_id": draft, "error": None}
            if attached or already:
                fields["status"] = "attached"
            P.artifact_set(conn, args.artifact_id, **fields)
            if attached or already:
                P.record_event(conn, kind="attached", artifact_id=args.artifact_id,
                               url_key=exact_url, cohort=cohort_for(row["created_at"]),
                               evidence_ref=draft)
                print("%s %s → draft %s"
                      % ("already attached:" if already else "attached:", args.artifact_id, draft))
                return 0
            print("the host appended nothing (attached=false); the artifact line is not in the "
                  "draft")
            return 1
        code, err = res.get("status"), _host_error(res)
        P.artifact_set(conn, args.artifact_id, error="attach %s: %s" % (code or "transport", err))
        print("attach failed (%s): %s" % (code or "transport", err))
        hints = {
            "posting_url_mismatch": "the run's job_url differs from the artifact's posting_url",
            "no_draft": "that run has no Gmail draft yet",
            "draft_missing": "the Gmail draft was deleted; re-run the trace stage",
            "draft_sent": "the draft was already sent; the host will not edit sent mail",
            "attachments_present": "the draft has attachments the host will not rewrite; "
                                   "append the line by hand",
            "unsupported_mime": "the draft's MIME cannot be edited safely; append by hand",
            "not_published": "publish the artifact first",
            "artifact_unavailable": "the host cannot read the stored artifact; republish it",
            "attach_in_progress": "another attach is running for this artifact; retry shortly",
            "not_a_draft": "that Gmail message is not a draft any more",
            "revoked": "the artifact was revoked",
        }
        for key, hint in hints.items():
            if key in err:
                print("  %s" % hint)
                break
        P.record_event(conn, kind="blocked", artifact_id=args.artifact_id,
                       url_key=(P.load_artifact(args.artifact_id) or {}).get("posting_url")
                       or row["url_key"])
        return 1
    finally:
        conn.close()


def cmd_revoke(args):
    conn = P.db()
    try:
        row = P.artifact_get(conn, args.artifact_id)
        if not row:
            print("pow: unknown artifact %s" % args.artifact_id)
            return 2
        remote = row["remote_id"] or args.artifact_id
        res = http_json("DELETE", "/api/v1/pow/artifacts/%s" % _quote(remote),
                        base=args.base, key_file=args.key_file)
        if res.get("status") == 200:
            P.artifact_set(conn, args.artifact_id, status="revoked", error=None)
            exact_url = (P.load_artifact(args.artifact_id) or {}).get("posting_url") \
                or row["url_key"]
            P.record_event(conn, kind="revoked", artifact_id=args.artifact_id,
                           url_key=exact_url, cohort=cohort_for(row["created_at"]),
                           evidence_ref=row["published_url"])
            print("revoked: %s (the public page 404s from now on)" % args.artifact_id)
            return 0
        err = _host_error(res)
        P.artifact_set(conn, args.artifact_id, error="revoke %s: %s"
                       % (res.get("status") or "transport", err))
        print("revoke failed (%s): %s" % (res.get("status") or "transport", err))
        return 1
    finally:
        conn.close()


def cmd_status(args):
    conn = P.db()
    try:
        where, params = "1=1", []
        if args.url:
            where += " AND url_key=?"
            params.append(P.url_key(args.url))
        rows = P.artifacts(conn, where, params, limit=args.limit)
        if args.json:
            print(json.dumps([dict(r) for r in rows], indent=1, ensure_ascii=False))
            return 0
        if not rows:
            print("no pow artifacts yet")
            return 0
        print("%-27s %-15s %-10s %-20s %-22s %s"
              % ("artifact", "recipe", "status", "created", "published", "draft / error"))
        counts = {}
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
            tail = (r["gmail_draft_id"] or "") if r["status"] == "attached" else \
                ((r["error"] or "")[:60] if r["error"] else (r["published_url"] or ""))
            print("%-27s %-15s %-10s %-20s %-22s %s"
                  % (r["artifact_id"], (r["recipe"] or "-"), (r["status"] or "-"),
                     (r["created_at"] or "")[:19], (r["published_url"] or "-"), tail))
        print("\n%d artifact(s): %s" % (len(rows), ", ".join(
            "%s=%d" % kv for kv in sorted(counts.items()))))
        return 0
    finally:
        conn.close()


def cmd_reconcile(args):
    conn = P.db()
    try:
        rows = P.artifacts(conn, "status IN ('published','attached')")
        if not rows:
            print("pow reconcile: nothing published")
        for row in rows:
            if not row["remote_id"]:
                continue
            if args.dry_run:
                print("WOULD CONFIRM %s (%s)" % (row["artifact_id"], row["published_url"]))
                continue
            res = http_json("GET", "/api/v1/pow/artifacts/%s" % _quote(row["remote_id"]),
                            base=args.base, key_file=args.key_file, timeout=30)
            if res.get("status") == 200:
                remote = (res.get("json") or {}).get("artifact") or {}
                url = remote.get("url") or ""
                if url and url != row["published_url"]:
                    if args.readonly:
                        print("url differs (read-only, not stored): %s → %s"
                              % (row["artifact_id"], url))
                    else:
                        P.artifact_set(conn, row["artifact_id"], published_url=url, error=None)
                        print("updated url: %s → %s" % (row["artifact_id"], url))
                else:
                    print("still live: %s %s" % (row["artifact_id"], row["published_url"]))
            elif res.get("status") == 404:
                if args.readonly:
                    print("gone (read-only): %s answers 404; run without --readonly to mark it "
                          "revoked" % row["artifact_id"])
                else:
                    P.artifact_set(conn, row["artifact_id"], status="revoked",
                                   error="public page is 404 (revoked or lost on the host)")
                    print("gone: %s marked revoked" % row["artifact_id"])
            else:
                print("could not confirm %s (%s %s)"
                      % (row["artifact_id"], res.get("status") or "transport",
                         _host_error(res)))
        for row in P.attachable(conn):
            print("attach available: python pow.py attach %s --run %s"
                  % (row["artifact_id"], row["run_id"]))
        return 0
    finally:
        conn.close()


def confirmed_targets(conn, days=None):
    """Applications that verify.py confirmed, with a public posting URL. Never inferred.

    `days` keeps the stage on recent applications (default pow.window_days, 7): an artifact for a
    six-month-old application is wasted spend.
    """
    days = int(days if days is not None else (cfg("window_days", 7) or 7))
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%S",
                           time.localtime(time.time() - days * 86400))
    rows = conn.execute(
        "SELECT a.key AS app_key, a.company, a.role, a.url_key, a.applied_at, a.updated_at, "
        "       p.url AS posting_url, p.apply_url, p.title AS posting_title, p.jd_file "
        "FROM applications a LEFT JOIN postings p ON p.url_key = a.url_key "
        "WHERE a.status='submitted' AND a.confirmed=1 "
        "AND a.url_key IS NOT NULL AND a.url_key <> '' "
        "AND COALESCE(a.applied_at, a.updated_at) >= ? "
        "ORDER BY COALESCE(a.applied_at, a.updated_at) DESC", (cutoff,)).fetchall()
    out = []
    for row in rows:
        # trace.py starts runs on the posting's apply URL; the host matches attach on the run's
        # exact job_url, so the artifact's posting_url must be that same URL (query kept).
        url = (row["apply_url"] or row["posting_url"] or "").strip()
        if not http_url_ok(url):
            continue
        out.append({"url": url, "url_key": row["url_key"],
                    "company": row["company"] or "", "role": row["role"] or "",
                    "key": row["app_key"], "jd_file": row["jd_file"]})
    return out


def cmd_daily(args):
    conf = S.CONFIG.get("pow") or {}
    if not (conf.get("enabled") and conf.get("daily")):
        print("pow: disabled (config pow.enabled + pow.daily); nothing generated")
        return 0
    conn = P.db()
    try:
        cap = int(conf.get("max_per_day") or 1)
        used = P.created_on(conn, S.TODAY)
        if used >= cap:
            print("pow: daily cap %d already used (%d row(s) created today)" % (cap, used))
            return 0
        targets = confirmed_targets(conn)
        print("pow: %d confirmed application(s) inside the %s-day window"
              % (len(targets), cfg("window_days", 7) or 7))
        todo = [t for t in targets
                if not P.artifact_for(conn, t["url_key"],
                                      statuses=("accepted", "published", "attached"))]
        if not todo:
            print("pow: no confirmed application needs an artifact (%d checked)" % len(targets))
            return 0
        budget = cap - used
        print("pow: %d target(s), budget %d today (generation starts at most %d omp pass(es); "
              "this stage never publishes and never attaches)" % (len(todo), budget, budget))
        for t in todo[:budget]:
            if args.dry_run:
                print("WOULD GENERATE %s %s" % (t["url_key"], t["url"]))
                continue
            argv = [PY, os.path.join(ROOT, "pow.py"), "generate", "--url", t["url"],
                    "--company", t["company"], "--role", t["role"], "--quiet"]
            if t.get("jd_file"):
                argv += ["--jd-file", t["jd_file"]]
            rc = subprocess.run(argv, cwd=ROOT, stdin=subprocess.DEVNULL).returncode
            if rc != 0:
                print("pow: generation for %s returned %s; continuing" % (t["url_key"], rc))
        return 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# offline smoke: no agent, no paid call, no external network
# ---------------------------------------------------------------------------

def _fixture_source(sid, url, excerpt):
    return {"id": sid, "url": url, "retrieved_at": "2026-11-04T00:00:00Z",
            "sha256": hashlib.sha256(excerpt.encode("utf-8")).hexdigest(), "excerpt": excerpt}


SMOKE_FETCHED_AT = "2026-11-04T12:00:00Z"
SMOKE_WINDOW_DAYS = 21

# Raw blitzapi.jobs.search rows: the flat shape the endpoint returns, one line per posting.
SMOKE_SIGNAL_RESULTS = (
    {"company_name": "Acme Freight", "title": "Driver Recruiter",
     "url": "https://jobs.lever.co/acmefreight/1a2b3c", "date_posted": "2026-10-28 09:12:00+00",
     "location": {"city": "Columbus", "country_code": "US"}},
    {"company_name": "Northline Logistics", "title": "Recruiting Manager, Driver Hiring",
     "url": "https://boards.greenhouse.io/northline/jobs/4471", "date_posted": "2026-11-01 08:00:00+00",
     "location": {"city": "Kansas City", "country_code": "US"}},
    {"company_name": "Northline Logistics", "title": "Fleet Safety Manager",
     "url": "https://boards.greenhouse.io/northline/jobs/4472", "date_posted": "2026-11-02 08:00:00+00",
     "location": {"city": "Kansas City", "country_code": "US"}},
    {"company_name": "Harbor Trucking", "title": "Driver Recruiter",
     "url": "https://www.linkedin.com/jobs/view/driver-recruiter-at-harbor-trucking-4481001",
     "date_posted": "2026-10-24 11:30:00+00",
     "location": {"city": "Savannah", "country_code": "US"}},
    {"company_name": "Pinecrest Carriers", "title": "Driver Recruiter, OTR",
     "url": "https://jobs.lever.co/pinecrest/8f7e6d", "date_posted": "2026-10-30 14:05:00+00",
     "location": {"city": "Little Rock", "country_code": "US"}},
    {"company_name": "Ridgeline Transport", "title": "Recruiting Coordinator",
     "url": "https://www.linkedin.com/jobs/view/recruiting-coordinator-ridgeline-4481002",
     "date_posted": "2026-10-26 10:15:00+00",
     "location": {"city": "Denver", "country_code": "US"}},
    {"company_name": "Bluewater Haulage", "title": "Driver Recruiter",
     "url": "https://jobs.ashbyhq.com/bluewater/9c1d2e", "date_posted": "2026-11-03 07:45:00+00",
     "location": {"city": "Mobile", "country_code": "US"}},
    {"company_name": "Cedar Valley Freight", "title": "Fleet Safety and Compliance Manager",
     "url": "https://cevalley.icims.com/jobs/2231/fleet-safety-manager",
     "date_posted": "2026-10-29 16:40:00+00",
     "location": {"city": "Cedar Rapids", "country_code": "US"}},
    {"company_name": "Summit LTL", "title": "OTR Recruiter",
     "url": "https://www.linkedin.com/jobs/view/otr-recruiter-at-summit-ltl-4481003",
     "date_posted": "2026-11-02 09:00:00+00",
     "location": {"city": "Toledo", "country_code": "US"}},
    {"company_name": "Ironline Logistics", "title": "Driver Recruiter",
     "url": "https://www.linkedin.com/jobs/view/driver-recruiter-at-ironline-4481004",
     "date_posted": "2026-11-03 18:20:00+00",
     "location": {"city": "Joliet", "country_code": "US"}},
    {"company_name": "Ironline Logistics", "title": "Dispatcher, Linehaul",
     "url": "https://www.linkedin.com/jobs/view/dispatcher-linehaul-at-ironline-4481005",
     "date_posted": "2026-11-03 18:25:00+00",
     "location": {"city": "Joliet", "country_code": "US"}},
)


def _fixture_signal_rows(window_days=SMOKE_WINDOW_DAYS):
    """The fixture posts, normalized exactly as `pow.py signals` stores them."""
    rows = []
    for raw in SMOKE_SIGNAL_RESULTS:
        row = normalize_signal(raw, SMOKE_FETCHED_AT, window_days, "fixture")
        if row:
            rows.append(row)
    return rows


def _gtm_candidate(rows=None, **overrides):
    """A v2 gtm_signal candidate built from fixture signal rows: accounts.csv + acct- sources.

    The rows are what `pow.py signals` would have stored (title, company, url, posted date, ATS),
    so the same grounding rules the real path applies are exercised here.
    """
    rows = rows if rows is not None else _fixture_signal_rows()
    employer = [
        _fixture_source("partners", "https://www.example.com/integrations",
                        "Our integration directory lists Greenhouse, Lever, Ashby, iCIMS and "
                        "Workday, so recruiting data lands in the warehouse next to the rest."),
        _fixture_source("buyers", "https://www.example.com/customers",
                        "Fleet and logistics operators use this product to read driver hiring "
                        "demand straight from their own job postings."),
        _fixture_source("careers", "https://www.example.com/careers",
                        "The go-to-market team is hired against fleet and logistics accounts in "
                        "the united states."),
    ]
    order, counts, first, index_of = [], {}, {}, {}
    for i, r in enumerate(rows, 1):
        company = r["company"]
        counts[company] = counts.get(company, 0) + 1
        if company not in first:
            first[company] = r
            index_of[company] = i
            order.append(company)
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(ACCOUNTS_HEADER)
    sources = list(employer)
    for company in order:
        r = first[company]
        detail = "%d open roles" % counts[company] if counts[company] > 1 else "single seat"
        writer.writerow([r["company"], r["title"], r["url"], detail, _posted_date(r),
                         r["ats"]])
        sources.append(_fixture_source("acct-%d" % index_of[company], r["url"], r["excerpt"]))
    cand = {
        "title": "{n_accounts} fleets posted driver recruiting roles in {window_days} days, "
                 "{n_ats_match} on your ats partners",
        "thesis": "These are {n_accounts} us fleets hiring driver recruiters right now, with "
                  "{n_multi} of them staffing more than one recruiting seat, so a product that "
                  "reads postings has live demand to route instead of a market estimate.",
        "sources": sources,
        "findings": [
            {"id": "method",
             "claim": "method: fetched {n_fetched} postings across {n_companies} companies for "
                      "the title keywords {titles} inside a {window_days}-day window, and kept "
                      "the {n_accounts} accounts whose posting is a buyer-side trigger.",
             "evidence_ids": ["buyers", "acct-1"],
             "limitations": "One fetch, one day; the keywords are the search, not a census."},
            {"id": "f1",
             "claim": "{n_multi} of {n_accounts} accounts are hiring for more than one "
                      "recruiting or safety seat at once, which reads as a team build-out rather "
                      "than a single backfill.",
             "evidence_ids": ["acct-1", "acct-2"],
             "limitations": "A snapshot of open postings, not a headcount change."},
            {"id": "f2",
             "claim": "{n_ats_match} of the accounts post on greenhouse, lever, ashby, icims or "
                      "workday, the vendors this company lists as integration partners.",
             "evidence_ids": ["partners", "acct-4"],
             "limitations": "The ATS is inferred from the posting URL."},
            {"id": "f3",
             "claim": "Every account below posted a driver recruiting role inside the "
                      "{window_days}-day window, so the list is open demand today rather than a "
                      "historic pattern.",
             "evidence_ids": ["acct-1", "acct-9"],
             "limitations": ""},
        ],
        "deliverable": {
            "summary": "One CSV of fleets with a hiring trigger taken from the fetched postings, "
                       "sorted strongest signal first: account, the posting title that fired, the "
                       "public URL as evidence, how many seats are open, the posted date and the "
                       "ATS the URL reveals. The page is the list plus the reading of it, not a "
                       "market study.",
            "files": [{"path": "accounts.csv", "language": "csv", "content": buf.getvalue()}],
        },
        "limitations": ["A hiring signal is not buying intent: the list shows demand, not budget.",
                        "This is a sample of one fetch, not a census of the market.",
                        "Every row is a public posting read on one day, so it can close tomorrow."],
        "email_line": "pulled {n_accounts} fleets that posted driver recruiter roles in the last "
                      "{window_days} days, {n_ats_match} on your ats partners:",
    }
    cand.update(overrides)
    return cand


def _fde_candidate():
    sources = [
        _fixture_source("apidocs", "https://docs.example.com/acme/api",
                        "Acme's API documents a cursor paginated records endpoint with a "
                        "required since parameter and a 429 rate limit note."),
        _fixture_source("auth", "https://docs.example.com/acme/auth",
                        "Acme authenticates with a bearer token sent in the Authorization "
                        "header and documents a token rotation endpoint."),
    ]
    harness = (
        "import json\n"
        "records = [{'id': 1}, {'id': 2}]\n"
        "page = {'data': records, 'next_cursor': 'abc'}\n"
        "assert page['next_cursor'] == 'abc'\n"
        "print(json.dumps({'pages': 1, 'records': len(page['data'])}))\n"
    )
    return {
        "title": "Cursor-paginated connector repro against Acme's documented API",
        "thesis": "Acme's documented pagination contract (cursor plus required since parameter) "
                  "can be reproduced with a 40-line client that follows next_cursor and backs off "
                  "on 429, so an integration review can start from working code rather than a "
                  "description.",
        "sources": sources,
        "findings": [
            {"id": "f1", "claim": "Records are cursor paginated and the documented next_cursor "
                                  "field is what the client must follow.",
             "evidence_ids": ["apidocs"], "limitations": "Docs snapshot; no live call made."},
            {"id": "f2", "claim": "Authentication is a bearer token in the Authorization header "
                                  "with a documented rotation endpoint.",
             "evidence_ids": ["auth"], "limitations": ""},
        ],
        "deliverable": {
            "summary": "A runnable reproduction of the documented pagination loop plus a notes "
                       "file mapping each line of the client to the doc sentence it implements. "
                       "The harness runs against an in-memory fixture; it makes no network call "
                       "and holds no credentials.",
            "files": [
                {"path": "run.py", "language": "python", "content": harness},
                {"path": "NOTES.md", "language": "markdown",
                 "content": "# Mapping\n\n- pagination: follows next_cursor from the records "
                            "endpoint.\n- auth: bearer header placeholder only; no credential is "
                            "stored.\n- rate limit: documents the 429 backoff that is not "
                            "implemented in this repro.\n"},
            ],
        },
        "limitations": ["The repro runs against an in-memory fixture, not Acme's live API.",
                        "No credential exists here, so authenticated behaviour is unverified."],
        "email_line": "built a runnable cursor-pagination repro from acme's public api docs:",
    }


def _ai_eval_spec():
    labels = {
        "integration": ["api", "webhook", "connector", "sync"],
        "pricing": ["price", "pricing", "tier", "seats"],
    }
    cases = [
        {"id": "c1", "input": "Does the product expose a webhook for every integration event "
                             "so a connector can sync records?", "expected": "integration"},
        {"id": "c2", "input": "What does the enterprise tier cost per seat each month for a "
                             "fifty seat team?", "expected": "pricing"},
        {"id": "c3", "input": "Can a connector sync the api on a schedule without a webhook "
                             "subscription?", "expected": "integration"},
        {"id": "c4", "input": "Is there a free trial below the entry pricing tier for small "
                             "teams?", "expected": "pricing"},
    ]
    return {"baseline": "keyword_classify", "config": {"labels": labels}, "cases": cases,
            "claim": {"metric": "accuracy", "min": 0.75}}


def _ai_candidate():
    spec = _ai_eval_spec()
    return {
        "title": "Keyword baseline for routing inbound product questions to integration or "
                 "pricing",
        "thesis": "Before any model spend, a two-label keyword classifier scores 1.00 on four "
                  "hand-labelled questions, so the routing decision is measurable against a "
                  "deterministic baseline instead of an opinion.",
        "sources": [_fixture_source("docs", "https://docs.example.com/acme/help",
                                    "Acme's help centre groups questions into integration topics "
                                    "and pricing topics, which is the label scheme used here."),
                    _fixture_source("api", "https://docs.example.com/acme/api",
                                    "Acme's API reference documents webhooks, connectors and a "
                                    "sync endpoint used by the integration examples.")],
        "findings": [
            {"id": "f1", "claim": "Four hand-labelled questions from Acme's help topics are "
                                  "separable by a two-label keyword rule with no model call.",
             "evidence_ids": ["docs"], "limitations": "Four cases only: a smoke-level eval."},
            {"id": "f2", "claim": "The integration label's vocabulary (api, webhook, connector, "
                                  "sync) matches the documented API surface.",
             "evidence_ids": ["api"], "limitations": ""},
        ],
        "deliverable": {
            "summary": "A declarative two-label eval plus a runnable harness note. pow.py runs it "
                       "with the built-in deterministic keyword baseline in an isolated child "
                       "process, so the reported accuracy is executed evidence, not an assertion.",
            "files": [
                {"path": "eval_spec.json", "language": "json",
                 "content": json.dumps(spec, indent=1)},
                {"path": "HARNESS.md", "language": "markdown",
                 "content": "# Eval harness\n\n- baseline: keyword_classify (built into pow.py)\n"
                            "- scoring: exact label match, accuracy reported\n"
                            "- run: python pow.py run <artifact_id>\n"},
            ],
        },
        "limitations": ["Four cases with two labels: this measures the harness, not the product.",
                        "No model baseline was called, so the comparison is keyword-only."],
        "email_line": "wrote a declarative eval and ran it against a keyword baseline:",
    }


class _StubHost:
    """A local stand-in for the TraceHire PoW routes: no deploy, no network, no Gmail."""

    def __init__(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        self.calls = []
        self.published = {}
        self.host_sha = {}
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                body = json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _url(self, aid):
                return "http://127.0.0.1:%d/p/%s" % (stub.port, aid.replace("_", "")[:24])

            def _sha(self, aid, artifact):
                return stub.host_sha.get(aid) or artifact_fingerprint(artifact)

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                body = json.loads(raw or b"{}")
                auth = (self.headers.get("Authorization") or "").startswith("Bearer ")
                stub.calls.append((self.command, self.path, auth, body))
                if not auth:
                    return self._send(401, {"error": "unauthorized"})
                if self.path == "/api/v1/pow/artifacts":
                    # The contract envelope: {"artifact": v1, "dedupe_key": local sha256}
                    artifact = body.get("artifact") or {}
                    aid = artifact.get("artifact_id")
                    if not aid or not body.get("dedupe_key"):
                        return self._send(400, {"error": "invalid_artifact",
                                                "details": ["artifact + dedupe_key required"]})
                    if body["dedupe_key"] != artifact_fingerprint(artifact):
                        return self._send(400, {"error": "invalid_dedupe_key",
                                                "details": ["dedupe_key is not the host "
                                                            "fingerprint"]})
                    if aid in stub.published and stub.published[aid] != artifact:
                        return self._send(409, {"error": "content_conflict"})
                    stub.published[aid] = artifact
                    return self._send(201, {"id": aid, "url": self._url(aid),
                                       "sha256": self._sha(aid, artifact)})
                if self.path.startswith("/api/v1/pow/artifacts/") and self.path.endswith("/attach"):
                    aid = self.path.split("/")[5]
                    if aid not in stub.published:
                        return self._send(409, {"error": "not_published"})
                    return self._send(200, {"draft_id": "draft_smoke_1", "attached": True,
                                            "already_attached": False})
                return self._send(404, {"error": "not_found"})

            def do_GET(self):
                stub.calls.append((self.command, self.path,
                                   (self.headers.get("Authorization") or "").startswith("Bearer "),
                                   None))
                aid = self.path.rsplit("/", 1)[-1]
                if aid in stub.published:
                    return self._send(200, {"artifact": {
                        "id": aid, "url": self._url(aid),
                        "sha256": self._sha(aid, stub.published[aid])}})
                return self._send(404, {"error": "not_found"})

            def do_DELETE(self):
                stub.calls.append((self.command, self.path,
                                   (self.headers.get("Authorization") or "").startswith("Bearer "),
                                   None))
                aid = self.path.rsplit("/", 1)[-1]
                if aid in stub.published:
                    stub.published.pop(aid, None)
                    return self._send(200, {"revoked": True})
                return self._send(404, {"error": "not_found"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.base = "http://127.0.0.1:%d" % self.port
        import threading
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def cmd_smoke(args):
    """End-to-end offline scenario: temp DB, local stub host, built-in evaluator, no paid call."""
    scratch = tempfile.mkdtemp(prefix="pow-smoke-")
    env = dict(os.environ, JOBHUNT_STATE=scratch)
    key_file = os.path.join(scratch, "key.txt")
    with open(key_file, "w", encoding="utf-8") as fh:
        fh.write("smoke_key\n")
    files = {}

    def fixture(name, obj):
        path = os.path.join(scratch, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
        files[name] = path
        return path

    def child(*argv, **kw):
        env2 = dict(env)
        env2.update(kw.get("env") or {})
        proc = subprocess.run([PY, os.path.abspath(__file__)] + list(argv), cwd=ROOT,
                              env=env2, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT)
        return proc.returncode, proc.stdout.decode("utf-8", "replace")

    def import_db():
        import sqlite3
        return sqlite3.connect(os.path.join(scratch, "state.db"), timeout=10)

    results = []

    def check(ok, label, detail=""):
        results.append((bool(ok), label, detail))
        print("%s %s%s" % ("PASS" if ok else "FAIL", label,
                           "" if ok else " — " + str(detail)[:400]))

    def row(artifact_id):
        try:
            conn = import_db()
            try:
                r = conn.execute("SELECT artifact_id, status, recipe, content_sha256, "
                                 "published_url, gmail_draft_id, error FROM pow_artifacts "
                                 "WHERE artifact_id=?", (artifact_id,)).fetchone()
            finally:
                conn.close()
            return r
        except Exception as ex:  # noqa: BLE001
            return ("error", str(ex))

    url = "https://jobs.example.com/acme/gtm-engineer"
    stub = _StubHost()
    try:
        # 0. the signal fetch: fixture response in, jsonl store out (no treg, no network)
        rows = _fixture_signal_rows()
        token = "smoke" + os.urandom(5).hex()
        signal_file = os.path.join(scratch, "pow", "signals", "%s.jsonl" % token)
        meta_file = os.path.join(scratch, "pow", "signals", "%s.meta.json" % token)
        fixture("treg.json", {"results": list(SMOKE_SIGNAL_RESULTS), "cost_usd": 0.0015,
                              "total_results": len(SMOKE_SIGNAL_RESULTS), "cursor": None})
        rc, out = child("signals", "--token", token, "--titles", "driver recruiter,fleet "
                        "safety manager", "--days", str(SMOKE_WINDOW_DAYS), "--countries", "US",
                        "--fixture", files["treg.json"])
        check(rc == 0 and os.path.isfile(signal_file),
              "signals stored every fetched posting under the token", out)
        stored_rows = []
        if os.path.isfile(signal_file):
            with open(signal_file, encoding="utf-8") as fh:
                stored_rows = [json.loads(line) for line in fh if line.strip()]
        check(len(stored_rows) == len(rows) and all(r.get("ats") is not None
                                                   for r in stored_rows),
              "signal store holds one row per posting with an ATS guess",
              "%d stored vs %d fixture" % (len(stored_rows), len(rows)))
        meta = json.load(open(meta_file, encoding="utf-8")) if os.path.exists(meta_file) else {}
        check(meta.get("calls") and meta.get("cost_usd", 0) > 0,
              "the fetch ledger records calls and spend", meta)
        check("call 1 added 11 new posting(s): 11 in the store from 9 distinct companies" in out
              and "$0.001500" in out
              and "from 9 distinct companies (keep 8..12" in out
              and "thin pool for a selective pack" in out,
              "signals prints the pool and the spend after every call", out[-400:])
        rc, out = child("signals", "--token", token, "--emit-sources")
        try:
            emitted = json.loads(out)
        except ValueError:
            emitted = []
        check(rc == 0 and len(emitted) == len(rows)
              and all(e["sha256"] == hashlib.sha256(e["excerpt"].encode("utf-8")).hexdigest()
                      for e in emitted)
              and [e["id"] for e in emitted[:2]] == ["acct-1", "acct-2"],
              "signals --emit-sources prints ready-to-copy grounded sources", out[:300])
        companies = []
        for r in rows:
            if r["company"] not in companies:
                companies.append(r["company"])
        n_accounts = len(companies)
        first_at = {}
        by_company = {}
        for r in rows:
            first_at.setdefault(r["company"], r["ats"])
            by_company.setdefault(r["company"], []).append(r)
        partners = [v for v in ("greenhouse", "lever", "ashby", "icims", "workday")]
        n_ats = sum(1 for c in companies if first_at[c] in partners)

        fixture("gtm.json", _gtm_candidate(rows))
        fixture("fde.json", _fde_candidate())
        fixture("ai.json", _ai_candidate())
        base_cand = _gtm_candidate(rows)
        bad_cand = dict(base_cand)
        bad_cand["findings"] = [
            base_cand["findings"][0],
            {"id": "f1", "claim": "A claim that cites evidence nobody declared in this artifact.",
             "evidence_ids": ["nope"], "limitations": ""},
            base_cand["findings"][2]]
        fixture("bad.json", bad_cand)
        fixture("abstain.json", {"abstained": True,
                                 "reason": "no public source describes this employer's product "
                                           "or hiring motion within the retrieval budget"})

        check(P.url_key("https://Jobs.Example.com/A/B/?q=1&utm_source=x&utm_medium=y"
                        "&gh_jid=7#frag") == "jobs.example.com/A/B?q=1&gh_jid=7",
              "posting identity mirrors the host rule (utm dropped, gh_jid kept, case kept)")

        # 0b. one omp pass is bounded by money, not by the clock: the argv carries --max-cost and
        # the call passes no timeout. omp is mocked here, so this runs on a machine without it.
        pow_conf()                                   # make sure the override dict exists
        saved = dict(_POW_OVERRIDE)
        _POW_OVERRIDE.update({"agent_budget_usd": 1.25})
        passed = []

        class _AgentDone(object):
            returncode = 3
            stdout = b""

        def _fake_run(argv, **kwargs):
            passed.append((argv, kwargs))
            return _AgentDone()

        real_run = subprocess.run
        subprocess.run = _fake_run
        try:
            agent_rc = run_agent("gtm_signal")
        finally:
            subprocess.run = real_run
            _POW_OVERRIDE.clear()
            _POW_OVERRIDE.update(saved)
        argv = passed[0][0] if passed else []
        check("--max-cost=1.25" in argv and "timeout" not in (passed[0][1] if passed else {})
              and agent_rc == 3
              and len([a for a in argv if a.startswith("--max-cost=")]) == 1,
              "one omp pass is capped with --max-cost and carries no timeout",
              "%r / %r" % (argv, passed[0][1] if passed else {}))

        # 1. generate from a hand-written candidate: no agent, no network
        rc, out = child("generate", "--url", url, "--company", "Acme", "--role", "GTM Engineer",
                        "--recipe", "gtm_signal", "--candidate", files["gtm.json"], "--quiet",
                        "--signals", signal_file)
        check(rc == 0, "generate gtm_signal from a candidate", out)
        gtm_id = ""
        for token_ in out.split():
            if token_.startswith("p_") and len(token_) == 26:
                gtm_id = token_
                break
        check(bool(gtm_id), "generated id returned", out)
        r = row(gtm_id) if gtm_id else None
        check(r and r[1] == "accepted", "gtm artifact stored as accepted", r)
        gtm_art = json.load(open(os.path.join(scratch, "pow", "artifacts", "%s.json" % gtm_id),
                                 encoding="utf-8")) if gtm_id else {}
        want_title = ("%d fleets posted driver recruiting roles in %d days, %d on your ats "
                      "partners" % (n_accounts, SMOKE_WINDOW_DAYS, n_ats))
        want_line = ("pulled %d fleets that posted driver recruiter roles in the last %d days, "
                     "%d on your ats partners:" % (n_accounts, SMOKE_WINDOW_DAYS, n_ats))
        claims = " ".join(str(f.get("claim")) for f in (gtm_art.get("findings") or []))
        check(gtm_art.get("title") == want_title and gtm_art.get("email_line") == want_line
              and "{" not in claims and gtm_art.get("thesis", "").find("{") < 0,
              "placeholders resolve to counts computed from the stored rows",
              "%r / %r / %r" % (gtm_art.get("title"), gtm_art.get("email_line"), claims))
        check("accounts.csv" == ((gtm_art.get("deliverable") or {}).get("files") or [{}])[0]
              .get("path"), "accounts.csv is deliverable.files[0]",
              (gtm_art.get("deliverable") or {}).get("files"))
        method = next((f for f in (gtm_art.get("findings") or [])
                       if str(f.get("id")) == "method"), {})
        method_claim = str(method.get("claim") or "")
        check(("%d postings across %d companies" % (len(stored_rows), len(companies)))
              in method_claim
              and "driver recruiter, fleet safety manager" in method_claim
              and ("%d-day window" % SMOKE_WINDOW_DAYS) in method_claim
              and ("kept the %d accounts" % n_accounts) in method_claim,
              "the method finding states the filter and funnel from the fetch ledger",
              method_claim)
        conn = import_db()
        try:
            ev = conn.execute("SELECT details_json FROM pow_events WHERE kind='generated' "
                              "AND artifact_id=?", (gtm_id,)).fetchone()
        finally:
            conn.close()
        det = json.loads(ev[0] or "{}") if ev else {}
        check(det.get("treg_calls") == 1 and det.get("cost_usd", 0) > 0,
              "the generated event carries the signal fetch cost", det)

        # 1a. without a signal store, a gtm_signal candidate cannot be validated at all
        rc, out = child("generate", "--url", "https://jobs.example.com/acme/other-gtm",
                        "--company", "Acme", "--role", "GTM Engineer", "--recipe", "gtm_signal",
                        "--candidate", files["gtm.json"], "--signals",
                        os.path.join(scratch, "nope.jsonl"))
        check(rc == 1 and "may only come from a fetch" in out,
              "gtm_signal without a signal store is refused", out)

        # 1b. a row the fetch never returned is refused
        fabricated = _gtm_candidate(rows)
        csv_text = fabricated["deliverable"]["files"][0]["content"]
        fabricated["deliverable"]["files"][0]["content"] = csv_text.replace(
            rows[0]["url"], "https://www.linkedin.com/jobs/view/made-up-fleet-9999", 1)
        fixture("fabricated.json", fabricated)
        rc, out = child("generate", "--url", "https://jobs.example.com/acme/fab",
                        "--company", "Acme", "--role", "GTM Engineer", "--recipe", "gtm_signal",
                        "--candidate", files["fabricated.json"], "--signals", signal_file)
        check(rc == 1 and "is not in this token's signal store" in out,
              "a row absent from the fetch is refused", out)

        # 1b2. the method finding is required: the page must state the rule that produced the pack
        nomethod = _gtm_candidate(rows)
        nomethod["findings"] = [f for f in nomethod["findings"] if f["id"] != "method"]
        fixture("nomethod.json", nomethod)
        rc, out = child("generate", "--url", "https://jobs.example.com/acme/nm",
                        "--company", "Acme", "--role", "GTM Engineer", "--recipe", "gtm_signal",
                        "--candidate", files["nomethod.json"], "--signals", signal_file)
        check(rc == 1 and "METHOD finding" in out,
              "a pack without a method finding is refused", out)

        # 1c. an invented number in the headline is refused (only placeholders may produce counts)
        invented = _gtm_candidate(rows, title="%d fleets hiring driver recruiters this week"
                                  % (n_accounts + 3))
        fixture("invented.json", invented)
        rc, out = child("generate", "--url", "https://jobs.example.com/acme/inv",
                        "--company", "Acme", "--role", "GTM Engineer", "--recipe", "gtm_signal",
                        "--candidate", files["invented.json"], "--signals", signal_file)
        check(rc == 1 and "no placeholder produced" in out,
              "a bare number in the title is refused", out)

        # 1c2. a placeholder that cannot be derived is refused, not rendered as zero
        nopartners = _gtm_candidate(rows)
        nopartners["sources"] = [
            _fixture_source("buyers", "https://www.example.com/customers",
                            "Fleet and logistics operators read driver hiring demand from their "
                            "own postings."),
            _fixture_source("careers", "https://www.example.com/careers",
                            "The go-to-market team is hired against fleet and logistics accounts "
                            "in the united states."),
        ] + [s for s in nopartners["sources"] if s["id"].startswith("acct-")]
        fixture("nopartners.json", nopartners)
        rc, out = child("generate", "--url", "https://jobs.example.com/acme/np",
                        "--company", "Acme", "--role", "GTM Engineer", "--recipe", "gtm_signal",
                        "--candidate", files["nopartners.json"], "--signals", signal_file)
        check(rc == 1 and "{n_ats_match}" in out and "unavailable" in out,
              "an underivable placeholder is refused instead of rendered as zero", out)

        # 1d. a fetch that cannot support the minimum is an abstention, not a thin page
        poor_results = [SMOKE_SIGNAL_RESULTS[i] for i in (0, 1, 3, 4, 5)]
        fixture("treg-poor.json", {"results": poor_results, "cost_usd": 0.0011,
                                   "cursor": None})
        poor_token = "poor" + token
        child("signals", "--token", poor_token, "--titles", "driver recruiter",
              "--days", str(SMOKE_WINDOW_DAYS), "--fixture", files["treg-poor.json"])
        poor_rows = [r for r in (normalize_signal(raw, SMOKE_FETCHED_AT, SMOKE_WINDOW_DAYS,
                                                 "fixture") for raw in poor_results) if r]
        fixture("poor.json", _gtm_candidate(poor_rows))
        poor_signals = os.path.join(scratch, "pow", "signals", "%s.jsonl" % poor_token)
        rc, out = child("generate", "--url", "https://jobs.example.com/acme/poor",
                        "--company", "Acme", "--role", "GTM Engineer", "--recipe", "gtm_signal",
                        "--candidate", files["poor.json"], "--signals", poor_signals)
        check(rc == 0 and "abstained" in out and "only 5 qualified account row(s)" in out
              and "signal fetch: 1 treg call(s)" in out,
              "a fetch below the account minimum becomes an abstention that still records cost",
              out)

        # 1d2. under-delivering from a store that DID have enough companies is a rejection
        fixture("few.json", _gtm_candidate(rows[:6]))
        rc, out = child("generate", "--url", "https://jobs.example.com/acme/few",
                        "--company", "Acme", "--role", "GTM Engineer", "--recipe", "gtm_signal",
                        "--candidate", files["few.json"], "--signals", signal_file)
        check(rc == 1 and "at least 8 are needed" in out,
              "an under-delivering candidate is rejected, not abstained", out)

        # 1f. the dollar budget is the only bound on the fetch (there is no call cap)
        budget_conf = json.dumps({"treg_budget_usd": 0.0005})
        btok = "bud" + token
        rc, out = child("signals", "--token", btok, "--titles", "driver recruiter",
                        "--days", "21", "--fixture", files["treg.json"],
                        env={"JOBHUNT_POW_CONFIG": budget_conf})
        rc2, out2 = child("signals", "--token", btok, "--titles", "driver recruiter",
                          "--days", "21", "--fixture", files["treg.json"],
                          env={"JOBHUNT_POW_CONFIG": budget_conf})
        check(rc == 0 and rc2 == 2 and "budget exhausted" in out2 and "call cap" not in out2,
              "the dollar budget stops the fetch before it can spend again", out + out2)

        # 1g. the table is Python's: an unquoted comma, a wrong detail, a typed posted date and a
        # retyped excerpt are rebuilt from the fetched store instead of failing the candidate.
        messy = _gtm_candidate(rows)
        junk = [",".join(ACCOUNTS_HEADER)]
        for c in companies:
            r = by_company[c][0]
            # printed, not written: any comma in a company name or title splits the row
            junk.append("%s,%s,%s,%s,%s,%s" % (r["company"], r["title"], r["url"],
                                              "4 matching postings", "2026-01-01", "workday"))
        messy["deliverable"]["files"] = [{"path": "accounts.csv", "language": "csv",
                                          "content": "\n".join(junk) + "\n"}]
        hand = "hand typed excerpt, not the stored one"
        messy["sources"] = [dict(s, excerpt=hand,
                                 sha256=hashlib.sha256(hand.encode("utf-8")).hexdigest())
                            if s["id"].startswith("acct-") else s
                            for s in messy["sources"]]
        fixture("messy.json", messy)
        rc, out = child("generate", "--url", "https://jobs.example.com/acme/messy",
                        "--company", "Acme", "--role", "GTM Engineer", "--recipe", "gtm_signal",
                        "--candidate", files["messy.json"], "--signals", signal_file)
        messy_id = next((t for t in out.split() if t.startswith("p_") and len(t) == 26), "")
        messy_art = json.load(open(os.path.join(scratch, "pow", "artifacts", "%s.json" % messy_id),
                                   encoding="utf-8")) if messy_id else {}
        table_text = ((messy_art.get("deliverable") or {}).get("files") or [{}])[0] \
            .get("content", "")
        rebuilt = list(csv.reader(io.StringIO(table_text)))
        north = by_company["Northline Logistics"][0]
        want_row = ["Northline Logistics", north["title"], north["url"], "2 open roles",
                    _posted_date(north), north["ats"]]
        single = [r for r in rebuilt[1:] if r and r[0] == "Acme Freight"]
        check(rc == 0 and rebuilt and rebuilt[0] == ACCOUNTS_HEADER
              and all(len(r) == len(ACCOUNTS_HEADER) for r in rebuilt)
              and len(rebuilt) - 1 == len(companies) and want_row in rebuilt
              and single and single[0][3] == "",
              "a hand-crafted accounts.csv is accepted and rebuilt cell by cell from the store",
              out + "\n" + table_text)

        # 1h. an acct source no row refers to is pruned; the kept ones are rebuilt from the store
        pruned = _gtm_candidate(rows)
        pruned["sources"] = pruned["sources"] + [
            _fixture_source("acct-11", rows[10]["url"], rows[10]["excerpt"])]
        fixture("pruned.json", pruned)
        rc, out = child("generate", "--url", "https://jobs.example.com/acme/pruned",
                        "--company", "Acme", "--role", "GTM Engineer", "--recipe", "gtm_signal",
                        "--candidate", files["pruned.json"], "--signals", signal_file)
        pid = next((t for t in out.split() if t.startswith("p_") and len(t) == 26), "")
        parc = json.load(open(os.path.join(scratch, "pow", "artifacts", "%s.json" % pid),
                              encoding="utf-8")) if pid else {}
        srcs = parc.get("sources") or []
        ids = [s["id"] for s in srcs]
        check(rc == 0 and "acct-11" not in ids
              and len([i for i in ids if i.startswith("acct-")]) == len(companies)
              and all(s["sha256"] == hashlib.sha256(s["excerpt"].encode("utf-8")).hexdigest()
                      for s in srcs if s["id"].startswith("acct-")),
              "unused account sources are pruned and the kept ones are regenerated",
              out + str(ids))

        # 1h2. a finding that cites a pruned source still fails, and names the id
        stale = _gtm_candidate(rows)
        stale["findings"] = list(stale["findings"][:2]) + [
            {"id": "stale", "claim": "A finding that cites an account source no kept row has.",
             "evidence_ids": ["acct-11"], "limitations": ""}]
        fixture("stale.json", stale)
        rc, out = child("generate", "--url", "https://jobs.example.com/acme/stale",
                        "--company", "Acme", "--role", "GTM Engineer", "--recipe", "gtm_signal",
                        "--candidate", files["stale.json"], "--signals", signal_file)
        check(rc == 1 and "acct-11" in out and "unknown source" in out,
              "a finding citing a pruned account source is refused by id", out)

        def artifact_of(text):
            aid = next((t for t in text.split() if t.startswith("p_") and len(t) == 26), "")
            path = os.path.join(scratch, "pow", "artifacts", "%s.json" % aid)
            if not aid or not os.path.isfile(path):
                return {}, aid
            with open(path, encoding="utf-8") as fh:
                return json.load(fh), aid

        def store_rows(token_name):
            path = os.path.join(scratch, "pow", "signals", "%s.jsonl" % token_name)
            if not os.path.isfile(path):
                return []
            with open(path, encoding="utf-8") as fh:
                return [json.loads(line) for line in fh if line.strip()]

        def age_table(token_rows):
            """(freshest age, rows inside three days) over the first posting of each company.

            Ages are relative to each row's own fetch stamp, which is when `signals` stored it —
            not to the fixture constant.
            """
            seen_c, ages = set(), []
            for r in token_rows:
                if r["company"] in seen_c:
                    continue
                seen_c.add(r["company"])
                ages.append((datetime.date.fromisoformat(r["fetched_at"][:10])
                             - datetime.date.fromisoformat(_posted_date(r))).days)
            return (min(ages) if ages else 0), sum(1 for a in ages if a <= 3)

        # 1i. the hook: a title may name kept accounts, and real recency is computable. The store is
        # shifted one day back so the freshest kept posting is two days old (a 1 would be refused).
        shifted = []
        for raw in SMOKE_SIGNAL_RESULTS:
            r = dict(raw)
            day = datetime.date.fromisoformat(r["date_posted"][:10]) - datetime.timedelta(days=1)
            r["date_posted"] = day.isoformat() + r["date_posted"][10:]
            shifted.append(r)
        fixture("treg-shifted.json", {"results": shifted, "cost_usd": 0.0015, "cursor": None})
        shift_token = "shift" + token
        rc, out = child("signals", "--token", shift_token, "--titles", "driver recruiter",
                        "--days", str(SMOKE_WINDOW_DAYS), "--countries", "US",
                        "--fixture", files["treg-shifted.json"])
        shift_rows = store_rows(shift_token)
        want_fresh, want_recent = age_table(shift_rows)
        shift_signals = os.path.join(scratch, "pow", "signals", "%s.jsonl" % shift_token)
        hook = _gtm_candidate(shift_rows)
        hook["title"] = ("{n_accounts} fleets, including Northline Logistics and Cedar Valley "
                         "Freight, opened driver-recruiter roles in the last {freshest_days} days")
        hook["email_line"] = ("pulled {n_accounts} fleets that posted driver recruiting roles in "
                              "the last {freshest_days} days, {n_recent_3d} of them this week:")
        fixture("hook.json", hook)
        rc, out = child("generate", "--url", "https://jobs.example.com/acme/hook",
                        "--company", "Acme", "--role", "GTM Engineer", "--recipe", "gtm_signal",
                        "--candidate", files["hook.json"], "--signals", shift_signals)
        hook_art, _hook_id = artifact_of(out)
        check(rc == 0 and want_fresh >= 2
              and ("in the last %d days" % want_fresh) in str(hook_art.get("title", ""))
              and "Northline Logistics" in str(hook_art.get("title", ""))
              and ("%d of them this week" % want_recent) in str(hook_art.get("email_line", "")),
              "a title may name kept accounts and state the computed recency",
              out + " / " + str(hook_art.get("title")))

        # 1i2. a headline count that resolves to 0 or 1 is refused (no "1 accounts"). The store is
        # shifted so its freshest posting is exactly one day older than the fetch stamp.
        today = datetime.date.today()
        newest = max(datetime.date.fromisoformat(r["date_posted"][:10])
                     for r in SMOKE_SIGNAL_RESULTS)
        delta = (today - datetime.timedelta(days=1)) - newest
        fresh_raw = []
        for raw in SMOKE_SIGNAL_RESULTS:
            r = dict(raw)
            day = datetime.date.fromisoformat(r["date_posted"][:10]) + delta
            r["date_posted"] = day.isoformat() + r["date_posted"][10:]
            fresh_raw.append(r)
        fixture("treg-fresh1.json", {"results": fresh_raw, "cost_usd": 0.0015, "cursor": None})
        fresh_token = "fresh" + token
        child("signals", "--token", fresh_token, "--titles", "driver recruiter",
              "--days", str(SMOKE_WINDOW_DAYS), "--countries", "US",
              "--fixture", files["treg-fresh1.json"])
        fresh_signals = os.path.join(scratch, "pow", "signals", "%s.jsonl" % fresh_token)
        fresh1 = _gtm_candidate(store_rows(fresh_token))
        fresh1["title"] = ("{n_accounts} fleets opened driver-recruiter roles in the last "
                           "{freshest_days} days")
        fixture("fresh1.json", fresh1)
        rc, out = child("generate", "--url", "https://jobs.example.com/acme/fresh1",
                        "--company", "Acme", "--role", "GTM Engineer", "--recipe", "gtm_signal",
                        "--candidate", files["fresh1.json"], "--signals", fresh_signals)
        check(rc == 1 and "resolves to 1" in out and "not a hook" in out,
              "a headline count that resolves to one is refused", out)
        perrs = []
        resolve_placeholders({"title": "repeat openings at {n_multi} accounts"},
                             {"n_multi": 0}, perrs)
        check(any("resolves to 0" in e for e in perrs),
              "a headline count that resolves to zero is refused", perrs)

        # 1i3. an absence is a limitation, not a finding
        absence = _gtm_candidate(rows)
        absence["findings"] = [absence["findings"][0],
                              {"id": "f1", "claim": "None of the kept posting URLs reveals a "
                                                    "matching ats integration partner.",
                               "evidence_ids": ["partners"], "limitations": ""},
                              absence["findings"][2]]
        fixture("absence.json", absence)
        rc, out = child("generate", "--url", "https://jobs.example.com/acme/absence",
                        "--company", "Acme", "--role", "GTM Engineer", "--recipe", "gtm_signal",
                        "--candidate", files["absence.json"], "--signals", signal_file)
        check(rc == 1 and "states an absence" in out and "limitations" in out,
              "a finding that states an absence is refused", out)

        # 1i4. the ATS placeholder only exists from two matching rows up
        linked = []
        for i, raw in enumerate(SMOKE_SIGNAL_RESULTS):
            r = dict(raw)
            if "icims.com" not in r["url"]:
                r["url"] = ("https://www.linkedin.com/jobs/view/%s-%d"
                            % (re.sub(r"[^a-z0-9]+", "-", r["company_name"].lower()).strip("-"),
                               4480000 + i))
            linked.append(r)
        fixture("treg-linked.json", {"results": linked, "cost_usd": 0.0015, "cursor": None})
        link_token = "link" + token
        child("signals", "--token", link_token, "--titles", "driver recruiter",
              "--days", str(SMOKE_WINDOW_DAYS), "--countries", "US",
              "--fixture", files["treg-linked.json"])
        link_signals = os.path.join(scratch, "pow", "signals", "%s.jsonl" % link_token)
        fixture("ats1.json", _gtm_candidate(store_rows(link_token)))
        rc, out = child("generate", "--url", "https://jobs.example.com/acme/ats1",
                        "--company", "Acme", "--role", "GTM Engineer", "--recipe", "gtm_signal",
                        "--candidate", files["ats1.json"], "--signals", link_signals)
        check(rc == 1 and "{n_ats_match}" in out and "unavailable" in out,
              "the ats placeholder exists only from two matching rows", out)

        # 1i5. a name on the page has to be an account the page shows
        unknown = _gtm_candidate(rows)
        unknown["title"] = ("{n_accounts} fleets, including Uber Freight, opened driver-recruiter "
                            "roles in the last {window_days} days")
        fixture("unknown.json", unknown)
        rc, out = child("generate", "--url", "https://jobs.example.com/acme/name1",
                        "--company", "Acme", "--role", "GTM Engineer", "--recipe", "gtm_signal",
                        "--candidate", files["unknown.json"], "--signals", signal_file)
        check(rc == 1 and "not one of the kept account rows" in out,
              "a company name that is not a kept row is refused", out)
        dropped = _gtm_candidate([r for r in rows if r["company"] != "Acme Freight"])
        dropped["title"] = ("{n_accounts} fleets, including Acme Freight, opened driver-recruiter "
                            "roles in the last {window_days} days")
        fixture("dropped.json", dropped)
        rc, out = child("generate", "--url", "https://jobs.example.com/acme/name2",
                        "--company", "Acme", "--role", "GTM Engineer", "--recipe", "gtm_signal",
                        "--candidate", files["dropped.json"], "--signals", signal_file)
        check(rc == 1 and "not a kept account row" in out,
              "a fetched company the pack dropped cannot be named", out)

        # 1i6. Python keeps the candidate's row order: the agent supplies strongest signal first
        ordered = _gtm_candidate(rows)
        lines = ordered["deliverable"]["files"][0]["content"].strip().split("\n")
        ordered["deliverable"]["files"] = [dict(
            ordered["deliverable"]["files"][0],
            content="\n".join([lines[0]] + list(reversed(lines[1:]))) + "\n")]
        fixture("ordered.json", ordered)
        rc, out = child("generate", "--url", "https://jobs.example.com/acme/order",
                        "--company", "Acme", "--role", "GTM Engineer", "--recipe", "gtm_signal",
                        "--candidate", files["ordered.json"], "--signals", signal_file)
        order_art, _order_id = artifact_of(out)
        order_rows = list(csv.reader(io.StringIO(
            ((order_art.get("deliverable") or {}).get("files") or [{}])[0].get("content", ""))))
        check(rc == 0 and len(order_rows) > 1 and order_rows[1][0] == companies[-1],
              "Python keeps the candidate's row order (strongest signal first)",
              out + " / " + str(order_rows[:2]))

        # 2. an ungrounded claim is refused
        rc, out = child("generate", "--url", url, "--company", "Acme", "--role", "GTM Engineer",
                        "--recipe", "gtm_signal", "--candidate", files["bad.json"], "--force",
                        "--signals", signal_file)
        check(rc == 1 and "unknown source" in out, "ungrounded claim rejected", out)

        # 3. honest abstention is stored, not fabricated
        rc, out = child("generate", "--url", "https://jobs.example.com/acme/ops-engineer",
                        "--company", "Acme", "--role", "Ops Engineer", "--recipe", "gtm_signal",
                        "--candidate", files["abstain.json"])
        check(rc == 0 and "abstained" in out, "abstention recorded", out)

        # 4. ai_eval: the built-in evaluator runs and its result must be a passed eval:
        rc, out = child("generate", "--url", "https://jobs.example.com/acme/ai-engineer",
                        "--company", "Acme", "--role", "Applied AI Engineer",
                        "--recipe", "ai_eval", "--candidate", files["ai.json"], "--quiet")
        check(rc == 0, "generate ai_eval from a candidate", out)
        ai_id = ""
        for token in out.split():
            if token.startswith("p_") and len(token) == 26:
                ai_id = token
                break
        art_path = os.path.join(scratch, "pow", "artifacts", "%s.json" % ai_id)
        art = json.load(open(art_path, encoding="utf-8")) if os.path.exists(art_path) else {}
        entries = ((art.get("deliverable") or {}).get("results") or [])
        check(bool(entries) and entries[0]["name"].startswith("eval:")
              and entries[0]["status"] == "passed" and "accuracy" in entries[0]["observed"],
              "built-in evaluator produced a passed eval: result", entries[:1])

        # 5. fde: generation declares not_run (no code executed without the operator)
        rc, out = child("generate", "--url", "https://jobs.example.com/acme/solutions-engineer",
                        "--company", "Acme", "--role", "Solutions Engineer",
                        "--recipe", "fde_integration", "--candidate", files["fde.json"],
                        "--quiet")
        check(rc == 0, "generate fde_integration from a candidate", out)
        fde_id = ""
        for token in out.split():
            if token.startswith("p_") and len(token) == 26:
                fde_id = token
                break
        art_path = os.path.join(scratch, "pow", "artifacts", "%s.json" % fde_id)
        art = json.load(open(art_path, encoding="utf-8")) if os.path.exists(art_path) else {}
        entries = ((art.get("deliverable") or {}).get("results") or [])
        check(bool(entries) and entries[0]["status"] == "not_run",
              "fde result is not_run before --execute", entries[:1])

        # 6. run without --execute executes nothing
        rc, out = child("run", fde_id)
        check(rc == 0 and "nothing executed" in out, "run without --execute is a no-op", out)

        # 7. run --execute runs the declared harness in an isolated scratch dir
        rc, out = child("run", fde_id, "--execute")
        art = json.load(open(art_path, encoding="utf-8")) if os.path.exists(art_path) else {}
        entries = ((art.get("deliverable") or {}).get("results") or [])
        check(rc == 0 and entries and entries[0]["name"] == "exec:run.py"
              and entries[0]["status"] == "passed",
              "executed harness recorded as passed", out + json.dumps(entries[:1]))

        # 8. publishing and attaching are disabled by config, even for an explicit id.
        # The smoke states that itself: a real config.json may have them enabled.
        disabled = json.dumps({"publish_enabled": False, "attach_enabled": False})
        rc, out = child("publish", gtm_id, "--base", stub.base, "--key-file", key_file,
                        env={"JOBHUNT_POW_CONFIG": disabled})
        check(rc == 2 and "disabled" in out,
              "publish refused while pow.publish_enabled is false", out)
        rc, out = child("attach", gtm_id, "--run", "run_smoke_1", "--base", stub.base,
                        "--key-file", key_file, env={"JOBHUNT_POW_CONFIG": disabled})
        check(rc == 2 and "disabled" in out,
              "attach refused while pow.attach_enabled is false", out)
        enabled = json.dumps({"publish_enabled": True, "attach_enabled": True})

        # 9. publish, then the identical publish is a no-op (the host is not called twice)
        rc, out = child("publish", gtm_id, "--base", stub.base, "--key-file", key_file,
                        env={"JOBHUNT_POW_CONFIG": enabled})
        check(rc == 0 and "published" in out, "publish gtm", out)
        first_calls = len([c for c in stub.calls if c[1] == "/api/v1/pow/artifacts"])
        rc, out = child("publish", gtm_id, "--base", stub.base, "--key-file", key_file,
                        env={"JOBHUNT_POW_CONFIG": enabled})
        second_calls = len([c for c in stub.calls if c[1] == "/api/v1/pow/artifacts"])
        check(rc == 0 and "already published" in out and second_calls == first_calls,
              "identical publish is locally idempotent", out)
        r = row(gtm_id)
        check(r and r[1] == "published" and r[4], "gtm row published with a public url", r)
        stored = json.load(open(os.path.join(scratch, "pow", "artifacts", "%s.json" % gtm_id),
                                encoding="utf-8"))
        full = hashlib.sha256(P.canonical_bytes(stored)).hexdigest()
        check(r and r[3] == full == artifact_fingerprint(stored),
              "dedupe_key is the canonical hash of the whole artifact (id included)",
              str(r)[:300])

        # 10. a host sha256 that does not match the dedupe_key is a hard failure: the row keeps
        # its previous status (so it is not attachable) and the remote page is not deleted.
        art = json.load(open(art_path, encoding="utf-8")) if os.path.exists(art_path) else {}
        local_sha = hashlib.sha256(P.canonical_bytes(art)).hexdigest()
        stub.host_sha[fde_id] = "ab" * 32
        rc, out = child("publish", fde_id, "--base", stub.base, "--key-file", key_file,
                        env={"JOBHUNT_POW_CONFIG": enabled})
        r = row(fde_id)
        check(rc == 1 and r and r[1] == "accepted" and "FAILED verification" in out,
              "host sha mismatch fails nonzero and leaves the row unpublished", out + str(r))
        rc, out = child("attach", fde_id, "--run", "run_smoke_1", "--base", stub.base,
                        "--key-file", key_file, env={"JOBHUNT_POW_CONFIG": enabled})
        check(rc == 2 and "publish it first" in out,
              "a failed publish leaves the artifact unattachable", out)
        del stub.host_sha[fde_id]
        rc, out = child("publish", fde_id, "--base", stub.base, "--key-file", key_file,
                        env={"JOBHUNT_POW_CONFIG": enabled})
        r = row(fde_id)
        check(rc == 0 and r and r[1] == "published" and r[3] == local_sha,
              "a verified republish stores the published state with the canonical hash",
              out + str(r))

        # 11. attach: guarded by the local runs row (posting match), then recorded
        conn = import_db()
        try:
            conn.execute("INSERT OR REPLACE INTO runs (url_key, company, title, url, run_id, "
                         "status, draft_id, started, attempts, retries) VALUES (?,?,?,?,?,?,?,?,1,0)",
                         (P.url_key("https://jobs.example.com/acme/solutions-engineer"), "Acme",
                          "Solutions Engineer",
                          "https://jobs.example.com/acme/solutions-engineer", "run_smoke_1",
                          "done", "draft_smoke_1", P.now_iso()))
            conn.commit()
        finally:
            conn.close()
        rc, out = child("attach", fde_id, "--run", "run_smoke_1", "--base", stub.base,
                        "--key-file", key_file, env={"JOBHUNT_POW_CONFIG": enabled})
        r = row(fde_id)
        check(rc == 0 and r and r[1] == "attached" and r[5] == "draft_smoke_1",
              "attach recorded the draft id", out + str(r))
        rc, out = child("attach", gtm_id, "--run", "run_smoke_1", "--base", stub.base,
                        "--key-file", key_file, env={"JOBHUNT_POW_CONFIG": enabled})
        check(rc == 2 and "refusing to attach" in out, "attach refuses an unrelated run", out)

        # 12. privacy scan: a 64-hex fingerprint is not treated as a phone number
        digits = "9" * 64
        check(_phone_shaped(digits) and not scan_publish_safety(
                  {"sources": [{"sha256": digits}]}),
              "sha256 is excluded from the phone-shaped scan", digits)
        posting_id = {"sources": [{"url": "https://www.linkedin.com/jobs/view/"
                                          "otr-recruiter-at-summit-ltl-4481003999"}]}
        check(not scan_publish_safety(posting_id),
              "a long numeric posting id in a URL is not a phone number", posting_id)
        check(bool(scan_publish_safety({"findings": [{"claim": "call 555 123 4567 today"}]})),
              "a phone number in prose is still refused", "phone in claim")

        # 13. status, read-only reconcile, revoke
        rc, out = child("status", "--json")
        try:
            rows = json.loads(out)
        except ValueError:
            rows = []
        check(rc == 0 and len(rows) >= 5, "status lists every row", out[:400])
        rc, out = child("reconcile", "--base", stub.base, "--key-file", key_file, "--readonly")
        check(rc == 0 and "still live" in out, "reconcile (read-only) confirms the pages", out)
        rc, out = child("revoke", gtm_id, "--base", stub.base, "--key-file", key_file)
        r = row(gtm_id)
        check(rc == 0 and r and r[1] == "revoked", "revoke marks the artifact revoked", out)

        # 12. validation is enforced by the validator, not by the prompt
        art = json.load(open(os.path.join(scratch, "pow", "artifacts", "%s.json" % ai_id),
                             encoding="utf-8"))
        art["deliverable"]["results"] = [{"name": "smoke", "input": "x", "expected": "x",
                                          "observed": "x", "status": "passed",
                                          "evidence_ids": []}]
        path = os.path.join(scratch, "forged.json")
        json.dump(art, open(path, "w", encoding="utf-8"))
        rc, out = child("validate", path)
        check(rc == 1 and "may only be `passed`" in out,
              "a passed result without execution is refused", out)
    finally:
        stub.stop()
        if not args.keep:
            shutil.rmtree(scratch, ignore_errors=True)
    failed = [r for r in results if not r[0]]
    print("\nsmoke: %d/%d passed%s" % (len(results) - len(failed), len(results),
                                       "" if failed else ""))
    if args.keep:
        print("state kept at %s" % scratch)
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def cmd_eval_step():
    """`__eval`: read one spec on stdin, score it, print one JSON line. Called in a child only."""
    try:
        spec = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except ValueError as ex:
        print(json.dumps({"ok": False, "error": "spec is not JSON: %s" % ex}))
        return 2
    errors = validate_eval_spec(spec)
    if errors:
        print(json.dumps({"ok": False, "error": "; ".join(errors[:4])}))
        return 2
    print(json.dumps(execute_eval(spec)))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="PoW artifacts: signals, generate, validate, run, publish, attach, revoke, "
                    "status, reconcile.")
    sub = ap.add_subparsers(dest="cmd")

    g = sub.add_parser("generate", help="produce one grounded artifact for a posting")
    g.add_argument("--url", help="public posting URL (or the apply URL the trace run used)")
    g.add_argument("--key", help="url_key already in postings (alternative to --url)")
    g.add_argument("--company")
    g.add_argument("--role")
    g.add_argument("--recipe", choices=P.RECIPES)
    g.add_argument("--candidate", help="validate/store this candidate instead of running the agent")
    g.add_argument("--jd-file", help="job description text, for the private-text leak check")
    g.add_argument("--signals", help="signal file to ground a gtm_signal candidate against "
                                     "(default: state/pow/signals/<token>.jsonl)")
    g.add_argument("--force", action="store_true", help="regenerate although an artifact exists")
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--quiet", action="store_true")

    v = sub.add_parser("validate", help="check an artifact or a bare candidate, store nothing")
    v.add_argument("file")
    v.add_argument("--url")
    v.add_argument("--company")
    v.add_argument("--role")
    v.add_argument("--recipe", choices=P.RECIPES)
    v.add_argument("--jd-file")
    v.add_argument("--signals", help="signal file the candidate's rows must come from")

    sg = sub.add_parser("signals", help="fetch the postings a gtm_signal artifact may cite")
    sg.add_argument("--titles", required=False,
                    help="comma-separated title keywords (e.g. \"driver recruiter,fleet safety manager\")")
    sg.add_argument("--days", type=int, help="posted within N days (default pow.signal_window_days)")
    sg.add_argument("--countries", help="comma-separated country codes (default query.country_code)")
    sg.add_argument("--keywords", help="comma-separated keywords matched against the job description")
    sg.add_argument("--icp-keywords", dest="icp_keywords",
                    help="comma-separated keywords matched against the company's own text")
    sg.add_argument("--sizes", help="company size buckets (1-10,11-50,51-200,201-500,501-1000,"
                                    "1001-5000,5001-10000,10001+)")
    sg.add_argument("--industries", help="exact company industries (see the endpoint's enum)")
    sg.add_argument("--limit", type=int, default=TREG_LIMIT_MAX,
                    help="results per call, 1..%d (default %d)" % (TREG_LIMIT_MAX, TREG_LIMIT_MAX))
    sg.add_argument("--pages", type=int, default=1,
                    help="cursor pages per run, 1..%d (each page is one paid call)"
                    % TREG_PAGES_MAX)
    sg.add_argument("--token", help="signal-store token (default: from state/pow/task.json)")
    sg.add_argument("--emit-sources", dest="emit_sources", action="store_true",
                    help="print the stored rows as ready-to-copy sources entries (no call)")
    sg.add_argument("--fixture", help="ingest a saved response instead of calling treg "
                                      "(offline smoke only)")
    sg.add_argument("--dry-run", action="store_true", help="print the request body, call nothing")

    r = sub.add_parser("run", help="produce execution evidence for one artifact")
    r.add_argument("target", help="artifact id, or a JSON file")
    r.add_argument("--execute", action="store_true",
                   help="run the artifact's own declared code (untrusted; operator consent)")

    p = sub.add_parser("publish", help="publish one validated artifact")
    p.add_argument("artifact_id")
    p.add_argument("--base", help="override tracehire.base (local smoke only)")
    p.add_argument("--key-file", help="override tracehire.key_file (local smoke only)")

    a = sub.add_parser("attach", help="append the artifact line to a run's existing Gmail draft")
    a.add_argument("artifact_id")
    a.add_argument("--run", required=True, help="TraceHire run id (must match this posting)")
    a.add_argument("--base")
    a.add_argument("--key-file")

    rv = sub.add_parser("revoke", help="delete the public page")
    rv.add_argument("artifact_id")
    rv.add_argument("--base")
    rv.add_argument("--key-file")

    st = sub.add_parser("status", help="one line per artifact")
    st.add_argument("--url")
    st.add_argument("--limit", type=int, default=50)
    st.add_argument("--json", action="store_true")

    rc = sub.add_parser("reconcile", help="confirm published pages and list attachable runs")
    rc.add_argument("--base")
    rc.add_argument("--key-file")
    rc.add_argument("--readonly", action="store_true",
                    help="report only; never change a local row (what daily.py uses)")
    rc.add_argument("--dry-run", action="store_true")

    dl = sub.add_parser("daily", help="the after-verify stage: generate for confirmed applications")
    dl.add_argument("--dry-run", action="store_true")

    sm = sub.add_parser("smoke", help="offline end-to-end scenario (temp DB, stub host)")
    sm.add_argument("--keep", action="store_true")

    sub.add_parser("__eval")

    args = ap.parse_args(argv)
    if args.cmd == "__eval":
        return cmd_eval_step()
    handlers = {"generate": cmd_generate, "validate": cmd_validate, "signals": cmd_signals,
                "run": cmd_run,
                "publish": cmd_publish, "attach": cmd_attach, "revoke": cmd_revoke,
                "status": cmd_status, "reconcile": cmd_reconcile, "daily": cmd_daily,
                "smoke": cmd_smoke}
    if args.cmd not in handlers:
        ap.print_help()
        return 2
    try:
        return handlers[args.cmd](args)
    except RuntimeError as ex:
        print("pow: %s" % ex)
        return 2


if __name__ == "__main__":
    sys.exit(main())