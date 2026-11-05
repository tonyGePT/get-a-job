#!/usr/bin/env python3
"""PoW artifacts: additive SQLite tables, atomic JSON files, canonical hashes.

One artifact is one grounded proof-of-work page for one confirmed application. Its identity is
its content: `content_id()` hashes the candidate body that pow.py accepts, plus the posting key
and the recipe, so regenerating the same evidence gives the same local id, and two runs cannot
silently drift apart. The host's dedupe key is the canonical sha256 of the published artifact
(`canonical_bytes`), which is recomputed here and compared against the sha256 the host returns.

Files live under `state/pow/` (gitignored like the rest of `state/`):

    state/pow/artifacts/<artifact_id>.json   accepted envelopes: exactly what publish sends
    state/pow/candidates/<token>.json        the agent's raw candidate, kept as the record
    state/pow/rejected/<artifact_id>.json    what the validator refused (reasons are in SQLite)
    state/pow/tmp/                           isolated execution dirs, removed after each run
    state/pow/signals/<token>.jsonl          every posting `pow.py signals` fetched for this run:
                                             the only rows a gtm_signal artifact may cite
    state/pow/signals/<token>.meta.json      the fetch ledger (one entry per treg call + spend),
                                             so cost survives a crash and is recorded on the event
    state/pow/task.json                      the current agent pass: recipe, targets, token,
                                             candidate path and where its signal file lives

The two tables are created additively here and nowhere else. `pow_metrics.py` (telemetry) reads
them and inserts `pow_events`; both modules carry the same DDL with IF NOT EXISTS, so whichever
runs first on a fresh state.db wins and the other is a no-op. Contract:

  pow_artifacts   one row per artifact (including rejected and abstained candidates, so a failure
                  is visible instead of vanishing); `content_sha256` is the host's hash once the
                  artifact is published, the local canonical hash before that.
  pow_events      append-only attribution evidence (generated/published/attached/revoked/blocked
                  context, and sent/reply/interview/offer outcomes recorded by pow_metrics.py).
                  It is never a retro change proposal and never a grading target.

Status vocabulary, in order of a healthy artifact:
  abstained   the recipes could not be supported honestly for this posting; `error` holds why
  rejected    a candidate failed deterministic validation; `error` holds the reasons
  accepted    validated and stored locally, not public yet
  published   the host accepted it; `remote_id` + `published_url` are set
  attached    the artifact line was appended to the trace run's existing Gmail draft
  revoked     the public page was deleted on the host; 404s forever
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hashlib  # noqa: E402
import json  # noqa: E402
import time  # noqa: E402
import urllib.parse  # noqa: E402

import state as S  # noqa: E402

RECIPES = ("gtm_signal", "fde_integration", "ai_eval")

STATUSES = ("abstained", "rejected", "accepted", "published", "attached", "revoked")

# `pow_store` is the sole schema owner. `lib/pow_telemetry.py` delegates to `ensure_schema`; it only inserts events.
SCHEMA_POW = """
CREATE TABLE IF NOT EXISTS pow_artifacts (
  artifact_id     TEXT PRIMARY KEY,          -- p_<24 hex>, derived from recipe+url_key+body
  url_key         TEXT NOT NULL DEFAULT '',  -- url_key(posting url): the host's posting identity
  recipe          TEXT,                      -- gtm_signal | fde_integration | ai_eval
  status          TEXT,                      -- see STATUSES above
  artifact_path   TEXT,                      -- state-relative path of the accepted JSON
  content_sha256  TEXT,                      -- host hash after publish, local canonical before
  created_at      TEXT,
  updated_at      TEXT,
  published_url   TEXT,                      -- public page, once published
  remote_id       TEXT,                      -- host artifact_id used by attach/revoke
  gmail_draft_id  TEXT,                      -- draft that carries the artifact line (never: sent)
  error           TEXT                       -- why it was refused or abstained
);
CREATE INDEX IF NOT EXISTS pow_artifacts_url ON pow_artifacts (url_key);

CREATE TABLE IF NOT EXISTS pow_events (
  event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
  artifact_id  TEXT,                         -- NULL = url-level (control) event
  url_key      TEXT NOT NULL DEFAULT '',
  kind         TEXT NOT NULL,                -- generated|published|attached|revoked|blocked|sent|…
  occurred_at  TEXT NOT NULL,
  evidence_ref TEXT,                         -- external proof for outcome kinds, never a draft id
  cohort       TEXT,
  details_json TEXT
);
CREATE INDEX IF NOT EXISTS pow_events_url ON pow_events (url_key);
CREATE INDEX IF NOT EXISTS pow_events_kind ON pow_events (kind);
CREATE INDEX IF NOT EXISTS pow_events_artifact ON pow_events (artifact_id);
CREATE INDEX IF NOT EXISTS pow_events_occurred ON pow_events (occurred_at);
-- One row per kind + external reference: pow_telemetry inserts with INSERT OR IGNORE, so this
-- index is what makes event dedupe airtight instead of best-effort.
CREATE UNIQUE INDEX IF NOT EXISTS pow_events_evidence ON pow_events (kind, evidence_ref)
  WHERE evidence_ref IS NOT NULL;
"""

ARTIFACT_FIELDS = ("artifact_id", "url_key", "recipe", "status", "artifact_path",
                   "content_sha256", "created_at", "updated_at", "published_url", "remote_id",
                   "gmail_draft_id", "error")

SUBDIRS = ("", "artifacts", "candidates", "rejected", "tmp", "signals")


# ---------------------------------------------------------------------------
# time and hashing
# ---------------------------------------------------------------------------

def now_iso():
    """UTC, second precision, explicit Z: the format the host's artifact schema accepts."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def canonical_bytes(obj):
    """The one serialization used for every hash: recursively sorted keys, no whitespace, UTF-8.

    Sorted keys are recursive by construction, and `ensure_ascii=False` keeps real UTF-8 rather
    than \\u escapes, so the same content always hashes the same. A lone surrogate cannot be
    encoded as UTF-8 and is rejected instead of being silently mangled: the artifact's own
    validator refuses it before this point, and an accidental unpaired surrogate must not be able
    to make two different artifacts share a hash.
    """
    # allow_nan=False: NaN/Infinity are not JSON, and a canonical hash must never be computed over
    # a document a different JSON implementation would reject or spell differently.
    text = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError as ex:
        raise ValueError("artifact contains an unpaired surrogate at %s: %s"
                         % (text[max(0, ex.start - 20):ex.end + 20], ex.reason))


def sha256_hex(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def url_key(url):
    """The host's posting identity (`powUrlKey`), mirrored exactly.

    Drops only: the fragment, trailing slashes on the path, and query parameters whose name starts
    with `utm_` (case-insensitive). Every other query parameter is kept in its original order —
    `gh_jid`, `id`, `s`, `ref` can identify different postings. Scheme is ignored and the host is
    lowercased; the path keeps its case. A local guard built on a different rule can pass while the
    host answers 409 posting_url_mismatch, so there is exactly one implementation, here.

    The local key is scheme-less while the host's `artifact.url_key` keeps the scheme: every
    comparison must be locally computed key vs locally computed key, never against the host's
    returned `url_key` field.
    """
    if not url:
        return ""
    try:
        parts = urllib.parse.urlsplit(url.strip())
        port = ":%d" % parts.port if parts.port else ""
    except ValueError:  # unparseable port: fall back to the raw text, never crash a guard
        return (url or "").strip()
    host = (parts.hostname or "").lower()
    path = (parts.path or "").rstrip("/")
    kept = []
    for chunk in (parts.query or "").split("&"):
        if not chunk:
            continue
        if chunk.split("=", 1)[0].lower().startswith("utm_"):
            continue
        kept.append(chunk)
    key = host + port + path
    return key + ("?" + "&".join(kept) if kept else "")


def content_id(recipe, url_key, body):
    """Stable local id for one artifact: content-derived, not random.

    `body` is the artifact with artifact_id and generated_at removed, so regenerating identical
    evidence lands on the same id (and the host sees the same hash), while any real change mints
    a new one.
    """
    return "p_" + sha256_hex(canonical_bytes({"body": body, "recipe": recipe,
                                              "url_key": url_key}))[:24]


# ---------------------------------------------------------------------------
# database
# ---------------------------------------------------------------------------

def ensure_schema(conn):
    conn.executescript(SCHEMA_POW)
    return conn


def db():
    """S.db() (WAL, busy timeout, the existing tables) plus the two additive PoW tables."""
    return ensure_schema(S.db())


def artifact_get(conn, artifact_id):
    return conn.execute("SELECT * FROM pow_artifacts WHERE artifact_id=?",
                        (artifact_id,)).fetchone()


def artifact_for(conn, url_key, recipe=None, statuses=None):
    """The newest row for a posting (optionally one recipe / one set of statuses)."""
    sql = "SELECT * FROM pow_artifacts WHERE url_key=?"
    params = [url_key]
    if recipe:
        sql += " AND recipe=?"
        params.append(recipe)
    if statuses:
        sql += " AND status IN (%s)" % ",".join("?" * len(statuses))
        params.extend(statuses)
    sql += " ORDER BY created_at DESC, artifact_id LIMIT 1"
    return conn.execute(sql, params).fetchone()


def artifact_upsert(conn, rec):
    """Insert or update one row; created_at is never rewritten."""
    values = {k: rec.get(k) for k in ARTIFACT_FIELDS}
    if not values.get("created_at"):
        values["created_at"] = now_iso()
    values["updated_at"] = rec.get("updated_at") or now_iso()
    marks = ",".join("?" * len(ARTIFACT_FIELDS))
    updates = ",".join("%s=excluded.%s" % (k, k) for k in ARTIFACT_FIELDS
                       if k not in ("artifact_id", "created_at"))
    conn.execute(
        "INSERT INTO pow_artifacts (%s) VALUES (%s) "
        "ON CONFLICT(artifact_id) DO UPDATE SET %s"
        % (",".join(ARTIFACT_FIELDS), marks, updates),
        [values[k] for k in ARTIFACT_FIELDS])
    return values["artifact_id"]


def artifact_set(conn, artifact_id, **fields):
    bad = set(fields) - set(ARTIFACT_FIELDS) - {"artifact_id"}
    if bad:
        raise ValueError("unknown pow_artifacts column(s): %s" % ", ".join(sorted(bad)))
    fields = dict(fields, updated_at=fields.get("updated_at") or now_iso())
    conn.execute("UPDATE pow_artifacts SET %s WHERE artifact_id=?"
                 % ",".join("%s=?" % k for k in fields),
                 list(fields.values()) + [artifact_id])


def artifacts(conn, where="1=1", params=(), limit=None):
    sql = "SELECT * FROM pow_artifacts WHERE %s ORDER BY created_at DESC" % where
    if limit:
        sql += " LIMIT %d" % int(limit)
    return conn.execute(sql, params).fetchall()


def created_on(conn, day):
    """Rows created on one local day: the daily generation cap counts attempts, not successes."""
    return conn.execute("SELECT COUNT(*) AS n FROM pow_artifacts "
                        "WHERE substr(created_at, 1, 10)=?", (day,)).fetchone()["n"]


def attachable(conn):
    """Published artifacts whose trace run has a live draft and no artifact line yet.

    The run is matched on the host's posting identity (`url_key`), not on runs.url_key: the
    pipeline's own key strips the whole query, which would confuse `?gh_jid=` variants. Reconcile
    only *reports* these rows; attaching stays an explicit operator command.
    """
    rows = conn.execute(
        "SELECT p.artifact_id, p.url_key, p.published_url, p.recipe, r.url AS run_url, "
        "       r.run_id, r.draft_id "
        "FROM pow_artifacts p JOIN runs r "
        "ON r.draft_id IS NOT NULL AND r.draft_id <> '' "
        "WHERE p.status='published' AND p.gmail_draft_id IS NULL "
        "AND COALESCE(r.status,'') IN ('done','running') ORDER BY p.created_at").fetchall()
    return [row for row in rows if url_key(row["run_url"]) == row["url_key"]]


def event_insert(conn, kind, url_key="", artifact_id=None, evidence_ref=None, cohort=None,
                 details=None, occurred_at=None):
    """Bare INSERT into pow_events. Policy (which kinds need evidence) lives in pow_telemetry."""
    cur = conn.execute(
        "INSERT INTO pow_events (artifact_id, url_key, kind, occurred_at, evidence_ref, cohort, "
        "details_json) VALUES (?,?,?,?,?,?,?)",
        (artifact_id, url_key or "", kind, occurred_at or now_iso(), evidence_ref, cohort,
         json.dumps(details, sort_keys=True) if details is not None else None))
    return cur.lastrowid


def record_event(conn, **kw):
    """Record through pow_telemetry when it is installed (it owns validation and dedupe).

    Generation must never fail because telemetry is absent or unhappy: attribution is evidence,
    not a dependency of publishing. pow_telemetry.record_event(conn, kind=..., artifact_id=...,
    url_key=..., cohort=..., evidence_ref=..., details=...) -> {"event_id", "duplicate", ...}.
    """
    try:
        import pow_telemetry as PT
    except Exception:  # not installed (or broken): generation still works
        return None
    try:
        return PT.record_event(conn, **kw)
    except Exception as e:  # a rejected event must not abort a publish
        print("pow: event %r not recorded (%s)" % (kw.get("kind"), e))
        return None


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------

def pow_dir(*parts):
    return S.path("pow", *parts)


def ensure_dirs():
    for sub in SUBDIRS:
        os.makedirs(pow_dir(sub), exist_ok=True)


def artifact_file(artifact_id, subdir="artifacts"):
    return pow_dir(subdir, "%s.json" % artifact_id)


def save_artifact(art, subdir="artifacts"):
    """Write one artifact atomically (tmp + os.replace) and return its state-relative path."""
    ensure_dirs()
    p = artifact_file(art["artifact_id"], subdir)
    S.write_json(p, art)
    return os.path.relpath(p, S.STATE).replace("\\", "/")


def load_artifact(artifact_id):
    return S.read_json(artifact_file(artifact_id), None)


def candidate_file(token):
    return pow_dir("candidates", "%s.json" % token)


def save_candidate(token, obj):
    ensure_dirs()
    p = candidate_file(token)
    S.write_json(p, obj)
    return p


def save_rejected(art):
    return save_artifact(art, subdir="rejected")


def task_file():
    """The current agent pass' brief (recipe, target, token, candidate and signal paths)."""
    return pow_dir("task.json")


# ---------------------------------------------------------------------------
# fetched signals: the grounding truth for a gtm_signal artifact
# ---------------------------------------------------------------------------

def signals_file(token):
    return pow_dir("signals", "%s.jsonl" % token)


def signals_meta_file(token):
    return pow_dir("signals", "%s.meta.json" % token)


def load_signals(token):
    """Every posting fetched for this token, in fetch order. Returns (rows, error).

    A malformed line is an error, never a silently shorter list: grounding that quietly loses
    rows would let an artifact cite something the fetch never returned.
    """
    p = signals_file(token)
    rows = []
    if not os.path.isfile(p):
        return rows, "no signal file at %s" % os.path.relpath(p, S.STATE).replace("\\", "/")
    with open(p, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                return rows, ("%s line %d is not JSON (the signal file was written by "
                              "`pow.py signals`; do not edit it by hand)"
                              % (os.path.basename(p), n))
            if not isinstance(obj, dict) or not isinstance(obj.get("url"), str):
                return rows, "%s line %d is not a posting object" % (os.path.basename(p), n)
            rows.append(obj)
    return rows, None


def save_signals(token, rows):
    """Rewrite the token's signal file atomically (jsonl, one posting per line)."""
    ensure_dirs()
    p = signals_file(token)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(tmp, p)
    return p


def append_signals(token, rows):
    """Append postings the file does not already hold. Returns (added, total).

    Identity is the posting (url_key) and the (company, title) pair, so the same job surfaced by
    two title keywords is stored once, while two roles at one company stay two rows: the artifact's
    `{n_multi}` count is derived from them.
    """
    existing, err = load_signals(token)
    if err and os.path.isfile(signals_file(token)):
        raise ValueError(err)
    existing = existing or []
    seen_urls = {url_key(r.get("url") or "") for r in existing if r.get("url")}
    seen_pairs = {((r.get("company") or ""), (r.get("title") or "")) for r in existing}
    added = []
    for row in rows:
        key = url_key(row.get("url") or "")
        pair = (row.get("company") or "", row.get("title") or "")
        if not key or key in seen_urls or pair in seen_pairs:
            continue
        seen_urls.add(key)
        seen_pairs.add(pair)
        added.append(row)
    if added:
        save_signals(token, existing + added)
    return len(added), len(existing) + len(added)


def signals_meta(token):
    """The fetch ledger for one token: {"token", "calls": [...], "cost_usd", "window_days", …}."""
    return S.read_json(signals_meta_file(token), None)


def signals_meta_save(token, meta):
    ensure_dirs()
    S.write_json(signals_meta_file(token), meta)
    return signals_meta_file(token)
