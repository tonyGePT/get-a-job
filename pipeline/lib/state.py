#!/usr/bin/env python3
"""Shared state for the daily job pipeline: one SQLite database plus atomic JSON
sidecars for the artefacts a human reads (candidates, verdicts, evidence, digest).

Why a database: every stage used to load `state/ledger.json`, hold it for
minutes, and rewrite the whole file, so any concurrent command lost writes.
Now a stage opens a connection, writes one row in one transaction, and closes.

Set `JOBHUNT_STATE` to point at another state directory (tests run against a copy).
"""

import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
from contextlib import contextmanager

# Every stage prints Gmail subjects, posting titles, etc. When stdout is a file or pipe Python
# uses the cp1252 locale and one emoji crashes the stage (verify.py did, 2026-11-01 and 11-02).
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
STATE = os.environ.get("JOBHUNT_STATE") or CONFIG["state_dir"]
TODAY = time.strftime("%Y-%m-%d")

DB_NAME = "state.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS postings (
  url_key     TEXT PRIMARY KEY,          -- norm_url of the discovered posting
  company     TEXT NOT NULL DEFAULT '',
  title       TEXT NOT NULL DEFAULT '',
  url         TEXT NOT NULL DEFAULT '',
  company_key TEXT NOT NULL DEFAULT '',
  title_key   TEXT NOT NULL DEFAULT '',
  first_seen  TEXT,
  verdict     TEXT,                      -- APPLY | SKIP | ESCALATE (null: never decided)
  reason      TEXT,
  decided_at  TEXT,
  apply_url   TEXT,
  ats         TEXT,
  jd_file     TEXT,
  blockers    TEXT,                      -- JSON array of hard blockers
  app_status  TEXT,                      -- submitted|unverified|failed|escalated|needs_code
  app_at      TEXT,
  app_proof   TEXT,                      -- receipt subject, or confirmation URL
  location    TEXT,                      -- as the qualify agent read it from the JD
  remote      TEXT,                      -- remote-us | hybrid | on-site | unknown
  salary      TEXT                       -- published pay as read from the JD, free text; NULL if none
);
CREATE INDEX IF NOT EXISTS postings_pair ON postings (company_key, title_key);
CREATE INDEX IF NOT EXISTS postings_decided ON postings (decided_at);

CREATE TABLE IF NOT EXISTS runs (
  url_key         TEXT PRIMARY KEY,      -- one extension run per posting URL, ever (private trace extension)
  company         TEXT NOT NULL DEFAULT '',
  title           TEXT NOT NULL DEFAULT '',
  url             TEXT NOT NULL DEFAULT '',
  run_id          TEXT,
  status          TEXT,                  -- queued|running|done|skipped|failed|killed|budget
                                         -- |stale (no terminal state in 6h) |start_failed
  stop_reason     TEXT,
  recipient       TEXT,
  recipient_title TEXT,
  email_verdict   TEXT,
  draft_id        TEXT,
  started         TEXT,                  -- latest start (also set by retry: the staleness clock)
  first_started   TEXT,
  restarted       TEXT,
  finished        TEXT,
  retries         INTEGER NOT NULL DEFAULT 0,
  attempts        INTEGER NOT NULL DEFAULT 1,
  previous_run    TEXT,
  error           TEXT,
  note            TEXT
);
CREATE INDEX IF NOT EXISTS runs_status ON runs (status);
CREATE INDEX IF NOT EXISTS runs_started ON runs (started);

CREATE TABLE IF NOT EXISTS applications (
  key               TEXT PRIMARY KEY,    -- evidence key (company/role slug)
  company           TEXT NOT NULL DEFAULT '',
  role              TEXT NOT NULL DEFAULT '',
  company_key       TEXT NOT NULL DEFAULT '',
  title_key         TEXT NOT NULL DEFAULT '',
  url_key           TEXT,                -- posting this application belongs to, when known
  final_url         TEXT,
  status            TEXT,                -- submitted|unverified|failed|escalated|needs_code
  confirmed         INTEGER NOT NULL DEFAULT 0,   -- receipt, or confirmation_text+final_url
  confirmation_text TEXT,
  blocking_question TEXT,
  note              TEXT,
  applied_at        TEXT,                -- first time it was seen submitted
  updated_at        TEXT,
  evidence_mtime    REAL
);
CREATE INDEX IF NOT EXISTS applications_status ON applications (status);
CREATE INDEX IF NOT EXISTS applications_posting ON applications (url_key);

CREATE TABLE IF NOT EXISTS receipts (
  msg_id   TEXT PRIMARY KEY,             -- Gmail message id: append-only, never re-keyed
  key      TEXT NOT NULL,
  company  TEXT,
  from_addr TEXT,
  subject  TEXT,
  msg_date TEXT,
  found_at TEXT
);
CREATE INDEX IF NOT EXISTS receipts_key ON receipts (key);

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);

-- The ledger that replaced application_log.md. Rows are the log's Applications table, plus rows
-- added by hand-run sessions through ledger.py. The pipeline's own outcomes live in postings/applications.
CREATE TABLE IF NOT EXISTS history (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  date           TEXT,
  company        TEXT NOT NULL DEFAULT '',
  title          TEXT NOT NULL DEFAULT '',
  url            TEXT NOT NULL DEFAULT '',
  url_key        TEXT NOT NULL DEFAULT '',
  company_key    TEXT NOT NULL DEFAULT '',
  title_key      TEXT NOT NULL DEFAULT '',
  resume         TEXT,                   -- resume variant / ID
  confidence     TEXT,
  signals        TEXT,
  action         TEXT,                   -- applied | unapplied | skipped | escalated
  status         TEXT,                   -- same vocabulary, plus duplicate
  status_updated TEXT,
  notes          TEXT,
  source         TEXT NOT NULL DEFAULT 'manual'   -- log-import | manual
);
CREATE INDEX IF NOT EXISTS history_url ON history (url_key);
CREATE INDEX IF NOT EXISTS history_pair ON history (company_key, title_key);

CREATE TABLE IF NOT EXISTS outreach (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  date          TEXT,
  company       TEXT NOT NULL DEFAULT '',
  recipient     TEXT,
  role          TEXT,
  email_verdict TEXT,
  channel       TEXT,                    -- e.g. "Gmail draft", "Gmail sent"
  notes         TEXT
);

CREATE TABLE IF NOT EXISTS resume_changes (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  date        TEXT,
  resume_id   TEXT,
  what        TEXT,
  why         TEXT
);

CREATE TABLE IF NOT EXISTS leads (          -- soft-signal hiring leads (replaced outreach_leads.md); not applications
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  date_logged   TEXT,
  signal_date   TEXT,
  person        TEXT,
  company       TEXT NOT NULL DEFAULT '',
  signal        TEXT,                    -- role / what the signal says
  source        TEXT,                    -- public URL
  fit           TEXT,                    -- fit / constraints
  status        TEXT                     -- e.g. "Research only", "Candidate lead", "Closed/expired"
);

CREATE TABLE IF NOT EXISTS log_notes (     -- free-text sections of the old log, kept verbatim
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  heading  TEXT,
  body     TEXT
);
"""


def ensure_dirs():
    for sub in ("", "logs", "jd", "evidence", "verdicts"):
        os.makedirs(os.path.join(STATE, sub), exist_ok=True)


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def path(*parts):
    return os.path.join(STATE, *parts)


def read_json(p, default=None):
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        # a half-written file is a bug, not a state value
        raise SystemExit("corrupt JSON: %s" % p)


def write_json(p, obj):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, p)


def norm_url(u):
    """Identity of a posting: host+path, no scheme, no query, no tracking."""
    if not u:
        return ""
    p = urllib.parse.urlsplit(u.strip())
    host = (p.netloc or "").lower().replace("www.", "")
    path_ = p.path.rstrip("/").lower()
    return host + path_


def norm_text(s):
    s = re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------------------
# database
# ---------------------------------------------------------------------------

def db():
    """A connection to state/state.db. WAL so a reader never blocks a writer."""
    ensure_dirs()
    conn = sqlite3.connect(path(DB_NAME), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn):
    """Bring a state.db created by an older version up to the current schema."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(postings)")}
    for col in ("location", "remote", "salary"):
        if col not in have:
            try:
                conn.execute("ALTER TABLE postings ADD COLUMN %s TEXT" % col)
            except sqlite3.OperationalError:  # another process added it first
                pass
    # record.py's bookkeeping columns: the log it kept is gone
    for table in ("postings", "applications"):
        have = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
        for col in ("logged_at", "log_status"):
            if col in have:
                try:
                    conn.execute("ALTER TABLE %s DROP COLUMN %s" % (table, col))
                except sqlite3.OperationalError:
                    pass


@contextmanager
def tx(conn):
    """One record, one transaction: never a whole-state copy-and-rewrite."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def get_meta(conn, k, default=None):
    row = conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row["v"] if row else default


def set_meta(conn, k, v):
    with tx(conn):
        conn.execute("INSERT INTO meta (k, v) VALUES (?, ?) "
                     "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


# ---------------------------------------------------------------------------
# postings
# ---------------------------------------------------------------------------

# location / remote / salary are deliberately not here: discovery candidates carry a `location`
# dict of their own, and these three columns are written only by posting_decide (qualify verdicts).
POSTING_FIELDS = ("url_key", "company", "title", "url", "company_key", "title_key",
                  "first_seen", "verdict", "reason", "decided_at", "apply_url", "ats",
                  "jd_file", "blockers", "app_status", "app_at", "app_proof")


def posting_upsert(conn, rec):
    """Insert a posting, or fill in fields the row does not have yet.

    A discovered posting never overwrites a decision: the decision columns are
    only written through `posting_decide` / `posting_application`.
    """
    values = {k: rec.get(k) for k in POSTING_FIELDS}
    values["url_key"] = values.get("url_key") or norm_url(rec.get("url"))
    if not values["url_key"]:
        raise ValueError("posting without url_key: %r" % (rec,))
    values["company"] = values.get("company") or ""
    values["title"] = values.get("title") or ""
    values["url"] = values.get("url") or ""
    values["company_key"] = values.get("company_key") or norm_text(values["company"])
    values["title_key"] = values.get("title_key") or norm_text(values["title"])
    values["first_seen"] = values.get("first_seen") or now()
    cols = ", ".join(POSTING_FIELDS)
    marks = ", ".join("?" for _ in POSTING_FIELDS)
    conn.execute(
        "INSERT INTO postings (%s) VALUES (%s) ON CONFLICT(url_key) DO UPDATE SET "
        "company=COALESCE(NULLIF(excluded.company, ''), postings.company), "
        "title=COALESCE(NULLIF(excluded.title, ''), postings.title), "
        "url=COALESCE(NULLIF(excluded.url, ''), postings.url), "
        "company_key=COALESCE(NULLIF(excluded.company_key, ''), postings.company_key), "
        "title_key=COALESCE(NULLIF(excluded.title_key, ''), postings.title_key), "
        "first_seen=COALESCE(postings.first_seen, excluded.first_seen)"
        % (cols, marks), [values[k] for k in POSTING_FIELDS])


def posting_decide(conn, url_key, verdict, reason, apply_url=None, ats=None,
                   jd_file=None, blockers=None, decided_at=None, company=None, title=None,
                   location=None, remote=None, salary=None):
    """Record the qualification verdict on its posting (idempotent per day)."""
    posting_upsert(conn, {"url_key": url_key, "company": company, "title": title,
                          "url": None})
    conn.execute(
        "UPDATE postings SET verdict=?, reason=?, decided_at=?, apply_url=?, ats=?, "
        "jd_file=?, blockers=?, location=?, remote=?, salary=? WHERE url_key=?",
        (verdict, reason, decided_at or now(), apply_url or None, ats or None,
         jd_file or None, json.dumps(blockers or []), location or None, remote or None,
         salary or None, url_key))


def _txt(x):
    """Free-text verdict field: the agent may emit a number or an object instead of a string."""
    if x is None or isinstance(x, str):
        return x
    return json.dumps(x, ensure_ascii=False)


def posting_application(conn, url_key, status, proof=None, at=None):
    """Record the application outcome on its posting, without downgrading proof."""
    if not url_key:
        return
    conn.execute(
        "UPDATE postings SET app_status=?, app_at=COALESCE(app_at, ?), "
        "app_proof=COALESCE(?, app_proof) WHERE url_key=?",
        (status, at or now(), proof, url_key))


def decided_keys(conn):
    """URL keys and (company,title) pairs that already have a verdict or outcome.

    A posting that was surfaced but never decided (qualify crashed) is absent on
    purpose: it stays eligible for the next run.
    """
    urls, pairs = set(), set()
    for row in conn.execute("SELECT url_key, company_key, title_key FROM postings "
                            "WHERE verdict IS NOT NULL OR app_status IS NOT NULL"):
        if row["url_key"]:
            urls.add(row["url_key"])
        if row["company_key"] and row["title_key"]:
            pairs.add((row["company_key"], row["title_key"]))
    for row in conn.execute("SELECT url_key, company_key, title_key FROM applications"):
        if row["url_key"]:
            urls.add(row["url_key"])
        if row["company_key"] and row["title_key"]:
            pairs.add((row["company_key"], row["title_key"]))
    return {"urls": urls, "pairs": pairs}


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------

RUN_FIELDS = ("url_key", "company", "title", "url", "run_id", "status", "stop_reason",
              "recipient", "recipient_title", "email_verdict", "draft_id", "started",
              "first_started", "restarted", "finished", "retries", "attempts",
              "previous_run", "error", "note")


def run_get(conn, url_key):
    return conn.execute("SELECT * FROM runs WHERE url_key=?", (url_key,)).fetchone()


def run_upsert(conn, rec):
    """Insert or update one run record in its own transaction."""
    values = {k: rec.get(k) for k in RUN_FIELDS}
    values["url_key"] = values.get("url_key") or norm_url(rec.get("url"))
    if not values["url_key"]:
        raise ValueError("run without url_key: %r" % (rec,))
    values.setdefault("status", None)
    for col in ("company", "title", "url"):
        if values.get(col) is None:
            values[col] = ""
    for col, dflt in (("retries", 0), ("attempts", 1)):
        if values.get(col) is None:
            values[col] = dflt
    cols = ", ".join(RUN_FIELDS)
    marks = ", ".join("?" for _ in RUN_FIELDS)
    updates = ", ".join("%s=excluded.%s" % (k, k) for k in RUN_FIELDS if k != "url_key")
    with tx(conn):
        conn.execute("INSERT INTO runs (%s) VALUES (%s) ON CONFLICT(url_key) DO UPDATE SET %s"
                     % (cols, marks, updates), [values[k] for k in RUN_FIELDS])


def run_update(conn, url_key, **fields):
    if not fields:
        return
    bad = set(fields) - set(RUN_FIELDS)
    if bad:
        raise ValueError("unknown run fields: %s" % ", ".join(sorted(bad)))
    sets = ", ".join("%s=?" % k for k in fields)
    with tx(conn):
        conn.execute("UPDATE runs SET %s WHERE url_key=?" % sets,
                     list(fields.values()) + [url_key])


def runs(conn, where="1=1", params=()):
    return conn.execute("SELECT * FROM runs WHERE %s" % where, params).fetchall()


def runs_started_on(conn, day):
    """Starts and retries both stamp `started`, so this is the daily spend counter."""
    return conn.execute("SELECT COUNT(*) AS n FROM runs WHERE substr(started, 1, 10)=?",
                        (day,)).fetchone()["n"]


# ---------------------------------------------------------------------------
# applications + receipts
# ---------------------------------------------------------------------------

APP_FIELDS = ("key", "company", "role", "company_key", "title_key", "url_key", "final_url",
              "status", "confirmed", "confirmation_text", "blocking_question", "note",
              "applied_at", "updated_at", "evidence_mtime")


def application_get(conn, key):
    return conn.execute("SELECT * FROM applications WHERE key=?", (key,)).fetchone()


def application_upsert(conn, rec):
    values = {k: rec.get(k) for k in APP_FIELDS}
    if not values.get("key"):
        raise ValueError("application without key")
    values["company"] = values.get("company") or ""
    values["role"] = values.get("role") or ""
    values["company_key"] = values.get("company_key") or norm_text(values["company"])
    values["title_key"] = values.get("title_key") or norm_text(values["role"])
    values["confirmed"] = 1 if values.get("confirmed") else 0
    values["updated_at"] = values.get("updated_at") or now()
    cols = ", ".join(APP_FIELDS)
    marks = ", ".join("?" for _ in APP_FIELDS)
    skip = {"key", "applied_at"}
    updates = ", ".join("%s=excluded.%s" % (k, k) for k in APP_FIELDS if k not in skip)
    with tx(conn):
        conn.execute("INSERT INTO applications (%s) VALUES (%s) ON CONFLICT(key) DO UPDATE "
                     "SET %s, confirmed=MAX(applications.confirmed, excluded.confirmed), "
                     "applied_at=COALESCE(applications.applied_at, excluded.applied_at)"
                     % (cols, marks, updates), [values[k] for k in APP_FIELDS])


def application_receipts(conn, key):
    return conn.execute("SELECT * FROM receipts WHERE key=? ORDER BY msg_date", (key,)).fetchall()


def add_receipt(conn, key, receipt):
    """Receipts are append-only, keyed by Gmail message id: a re-query can never
    delete one because it fell outside the search window."""
    with tx(conn):
        conn.execute(
            "INSERT INTO receipts (msg_id, key, company, from_addr, subject, msg_date, found_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(msg_id) DO UPDATE SET key=excluded.key",
            (receipt["id"], key, receipt.get("company"), receipt.get("from"),
             receipt.get("subject"), receipt.get("date"), now()))


def receipt_subjects_by_key(conn):
    out = {}
    for row in conn.execute("SELECT key, subject FROM receipts ORDER BY msg_date"):
        out.setdefault(row["key"], row["subject"])
    return out


# ---------------------------------------------------------------------------
# ingest: agent-written artefacts -> database
# ---------------------------------------------------------------------------

def merge_verdict_parts(day=None):
    """Build state/verdicts/<day>.json from the per-candidate part files the qualify workers wrote.

    Workers write one verdict per candidate to verdicts/parts/<day>/<key>.json (older runs also left
    verdicts/parts/<day>-<id>.json). The final file is assembled here, in Python, so a qualify agent
    that never gets round to merging (or is killed by the stage timeout) cannot lose a day's
    verdicts. The file is rebuilt only when it is missing or older than its newest part, so a
    hand-edited verdicts file is never overwritten. Returns the number of verdicts merged (0 = left alone).
    """
    import glob
    day = day or TODAY
    parts = glob.glob(path("verdicts", "parts", day, "*.json")) + \
        glob.glob(path("verdicts", "parts", "%s-*.json" % day))
    if not parts:
        return 0
    final = path("verdicts", "%s.json" % day)
    newest = max(os.path.getmtime(p) for p in parts)
    if os.path.exists(final) and os.path.getmtime(final) >= newest:
        return 0
    by_url = {}
    for p in sorted(parts, key=os.path.getmtime):
        try:
            with open(p, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            continue
        items = doc if isinstance(doc, list) else doc.get("items") if isinstance(doc, dict) and "items" in doc \
            else [doc]
        for it in items:
            if isinstance(it, dict) and it.get("verdict") and (it.get("url") or it.get("apply_url")):
                by_url[norm_url(it.get("url") or it.get("apply_url"))] = it   # later part wins
    if not by_url:
        return 0
    write_json(final, {"generated": now(), "merged_from_parts": len(parts), "items": list(by_url.values())})
    return len(by_url)

def ingest_verdicts(day=None, conn=None):
    """Store today's qualification verdicts on their postings.

    The qualify agent writes state/verdicts/<date>.json; this is the only path
    from that file into state, and discover.py dedupes on the result. Postings
    that were surfaced but never verdicted are left with verdict NULL, so they
    stay eligible instead of being lost.
    """
    day = day or TODAY
    merge_verdict_parts(day)
    doc = read_json(path("verdicts", "%s.json" % day), None)
    if doc is None:
        return 0
    items = doc if isinstance(doc, list) else doc.get("items", [])
    own = conn is None
    conn = conn or db()
    n = 0
    try:
        for v in items:
            url = v.get("url") or v.get("apply_url")
            key = norm_url(url)
            if not key or not v.get("verdict"):
                continue
            with tx(conn):
                posting_decide(conn, key, v["verdict"], v.get("reason"),
                               apply_url=v.get("apply_url"), ats=v.get("ats"),
                               jd_file=v.get("jd_file"), blockers=v.get("hard_blockers"),
                               decided_at=doc.get("generated") or now(),
                               company=v.get("company"), title=v.get("title"),
                               location=_txt(v.get("location")), remote=_txt(v.get("remote")),
                               salary=_txt(v.get("salary")))
            n += 1
    finally:
        if own:
            conn.close()
    return n


def _queue_by_key():
    queue = (read_json(path("apply-queue.json"), {}) or {}).get("queue", [])
    return {q["key"]: q for q in queue if q.get("key")}


def _link_posting(conn, ev, queue):
    """Which posting does this application belong to?

    The apply queue carries the discovered URL key (prepare.py puts it there);
    fall back to an exact apply_url match, then to the (company, role) pair.
    """
    q = queue.get(ev.get("key"))
    if q and q.get("url_key"):
        if conn.execute("SELECT 1 FROM postings WHERE url_key=?", (q["url_key"],)).fetchone():
            return q["url_key"]
    if q and q.get("apply_url"):
        hit = norm_url(q["apply_url"])
        if conn.execute("SELECT 1 FROM postings WHERE url_key=?", (hit,)).fetchone():
            return hit
    ck, tk = norm_text(ev.get("company")), norm_text(ev.get("role"))
    if ck and tk:
        row = conn.execute("SELECT url_key FROM postings WHERE company_key=? AND title_key=?",
                           (ck, tk)).fetchone()
        if row:
            return row["url_key"]
    return None


def sync_evidence(conn=None):
    """Read state/evidence/*.json (the apply agent's output) into the database.

    Status is monotonic: a confirmed submission is never downgraded, and a
    receipt that was already recorded is never dropped.
    """
    own = conn is None
    conn = conn or db()
    queue = _queue_by_key()
    d = path("evidence")
    seen = 0
    try:
        for name in sorted(os.listdir(d) if os.path.isdir(d) else []):
            if not name.endswith(".json"):
                continue
            p = os.path.join(d, name)
            ev = read_json(p, None)
            if not ev or not ev.get("key"):
                continue
            key = ev["key"]
            prev = application_get(conn, key)
            status = ev.get("status") or (prev["status"] if prev else None)
            confirmed = (bool(ev.get("receipt_confirmed")) or bool(ev.get("receipts"))
                         or bool(ev.get("confirmation_text") and ev.get("final_url")))
            if prev:
                status = _monotonic_status(prev["status"], status, bool(prev["confirmed"]) or confirmed)
                confirmed = confirmed or bool(prev["confirmed"])
            mtime = os.path.getmtime(p)
            applied_at = None
            if status == "submitted":
                applied_at = (prev["applied_at"] if prev and prev["applied_at"]
                              else time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(mtime)))
            application_upsert(conn, {
                "key": key, "company": ev.get("company"), "role": ev.get("role"),
                "url_key": _link_posting(conn, ev, queue),
                "final_url": ev.get("final_url"), "status": status,
                "confirmed": confirmed, "confirmation_text": ev.get("confirmation_text"),
                "blocking_question": ev.get("blocking_question"), "note": ev.get("note"),
                "applied_at": applied_at, "evidence_mtime": mtime,
            })
            for r in ev.get("receipts") or []:
                add_receipt(conn, key, dict(r, company=ev.get("company")))
            row = application_get(conn, key)
            if row["url_key"]:
                posting_application(conn, row["url_key"], status,
                                    proof=_proof_for(conn, key))
            seen += 1
    finally:
        if own:
            conn.close()
    return seen


OUTCOME_RANK = {"escalated": 0, "failed": 1, "needs_code": 2, "unverified": 3, "submitted": 4}


def _monotonic_status(old, new, confirmed):
    """Never move a submission backwards: once proof exists, `submitted` is final."""
    if old == "submitted" and (confirmed or new in (None, "unverified")):
        return "submitted"
    if old and new and OUTCOME_RANK.get(new, -1) < OUTCOME_RANK.get(old, -1) and confirmed:
        return old
    return new or old


def _proof_for(conn, key):
    row = conn.execute("SELECT subject FROM receipts WHERE key=? ORDER BY msg_date LIMIT 1",
                       (key,)).fetchone()
    if row:
        return row["subject"]
    app = application_get(conn, key)
    if app and app["confirmation_text"] and app["final_url"]:
        return app["final_url"]
    return None


# ---------------------------------------------------------------------------
# history: the ledger that replaced application_log.md (imported once on 2026-11-02)
# ---------------------------------------------------------------------------

def history_add(conn, company, title, url="", resume="", confidence="", signals="",
                action="", status="", notes="", date=None, source="manual"):
    """Append one row to the history ledger (manual sessions and the one-time import)."""
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO history (date, company, title, url, url_key, company_key, title_key, resume, "
            "confidence, signals, action, status, status_updated, notes, source) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (date or TODAY, company, title, url, norm_url(url), norm_text(company), norm_text(title),
             resume, confidence, signals, action, status, TODAY, notes, source))
    return cur.lastrowid


def seen_index(conn=None):
    """URLs, (company,title) pairs and companies already in the history ledger."""
    own = conn is None
    conn = conn or db()
    try:
        urls, pairs, companies = set(), set(), set()
        for r in conn.execute("SELECT url_key, company_key, title_key FROM history"):
            if r["url_key"]:
                urls.add(r["url_key"])
            pairs.add((r["company_key"], r["title_key"]))
            companies.add(r["company_key"])
        return {"urls": urls, "pairs": pairs, "companies": companies}
    finally:
        if own:
            conn.close()


def heartbeat(stage, extra=None):
    rec = read_json(path("heartbeat.json"), {})
    rec["stage"] = stage
    rec["at"] = now()
    rec["pid"] = os.getpid()
    if extra:
        rec.update(extra)
    write_json(path("heartbeat.json"), rec)


if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) > 1 and _sys.argv[1] == "beat":
        ensure_dirs()
        heartbeat(_sys.argv[2] if len(_sys.argv) > 2 else "unknown")
        print("beat:", _sys.argv[2] if len(_sys.argv) > 2 else "unknown")
