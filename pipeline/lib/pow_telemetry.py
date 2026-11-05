#!/usr/bin/env python3
"""PoW attribution: immutable, evidence-backed events for proof-of-work artifacts.

A proof-of-work artifact is only worth something if its outcome is attributable, and an
attribution is only worth something if it rests on evidence. This module is the only place that
writes `pow_events`, and it refuses to derive an outcome from anything weaker than a named
external reference:

  * a Gmail draft is never a send (`gmail_draft_id` only ever produces an "unknown" count),
  * a published artifact URL is never a view (there is no open or read tracking at all),
  * a published or attached event is never a send *and never proof that the message carried the
    link*: a send is credited to an artifact only when the operator supplies the link evidence
    (`--sent-artifact-url`, which must be this artifact's published URL) plus `--evidence-ref` for
    the sent message itself. Without that, the send is stored as an unverified control send
    (artifact_id NULL, details.pow_unverified) and never reaches the artifact's denominator,
  * an artifact id alone is never an outcome (the five outcome kinds need --evidence-ref).

What it reports is descriptive: counts with the denominator they came from, per cohort, per
recipe and per artifact, split into sent / matured / censored / pending / unsent. Absence of a
reply event means "nothing recorded", never "no reply", and nothing here claims that sending an
artifact caused an interview.

The storage layer belongs to lib/pow_store.py: it creates the pow_artifacts/pow_events tables and
this module asks it for them. This module never writes `pow_artifacts`; it inserts events and
reads both tables. `cohort` is fixed when the artifact is generated/published/attached and is
never taken from whoever records an outcome later, so a cohort cannot be relabelled after the
fact; a send takes the last cohort fixed at or before its own timestamp.

Posting identity is the host's `powUrlKey` (lib/pow_store.url_key), which `url_key` stores on both
tables, so `?gh_jid=1` and `?gh_jid=2` stay two postings and no join ever folds them through
S.norm_url. Each send gets its own observation window: an outcome that arrives after it is
reported as a late outcome and stays out of the rate and the latency.

A `sent` is credited to an artifact only when the operator attests — and the attestation is
checked against the stored `published_url` — that the message actually sent contained that
artifact's public URL (`--sent-artifact-url`), together with real sent-message evidence. That is
the whole proof, and it is the operator's word: an evidence reference that happens to name the
link is recorded as a note and never promoted into independent verification. A published or
attached event proves a page exists, not what the recipient received, so it is not link evidence.
Every other send is stored as an unverified control event (artifact_id NULL, details.pow_unverified
plus details.unverified_reason), never as that artifact's success.

There is no Gmail reconciliation: the pipeline's existing scopes cannot prove that an artifact's
send was this artifact's, so `pow_metrics.py pending` prints the exact record command a human runs
after observing the send instead of guessing from a draft.

CLI: pow_metrics.py (record | report | status | pending). See its docstring.
"""

import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from statistics import median

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import state as S  # noqa: E402

try:                                    # the DDL owner; its absence must not break a reader
    import pow_store  # noqa: E402
    _STORE_ERROR = None
except Exception as _e:                 # noqa: BLE001 - reported by ensure_schema below
    pow_store, _STORE_ERROR = None, _e

# ---------------------------------------------------------------------------
# schema: lib/pow_store.py owns it, this module only asks for it
# ---------------------------------------------------------------------------

# An outcome needs a real reference. These five are the funnel; everything else is context.
OUTCOME_KINDS = ("sent", "reply", "positive_reply", "interview", "offer")
CONTEXT_KINDS = ("generated", "published", "attached", "revoked", "blocked")
KNOWN_KINDS = OUTCOME_KINDS + CONTEXT_KINDS
EVIDENCE_REQUIRED = OUTCOME_KINDS

# A reference that says "draft" cannot prove a send; a draft id is the exact thing this module
# must not promote into an outcome.
DRAFT_RE = re.compile(r"(^|[^a-z])(gmail[_ -]?)?draft([^a-z]|$)", re.I)

# Kinds whose rate shares one denominator (matured sends). `sent` is the denominator itself.
RATE_KINDS = ("reply", "positive_reply", "interview", "offer")

CAVEATS = (
    "Counts, not proof: absence of a reply event means nothing was recorded, not that nobody replied.",
    "No open/read/view tracking exists, so a published artifact is never counted as seen.",
    "A Gmail draft is never counted as sent; a send needs an external evidence reference.",
    "A credited send rests on the operator's attestation that the message carried the artifact's "
    "URL, checked against the stored page; nothing here verifies what the message contained.",
    "Rates are per artifact with one observation window per send; an outcome that arrives after "
    "that window is reported as late and kept out of the rate and the latency.",
    "Unverified sends are excluded from every artifact rate; only operator-attested, matured "
    "sends are a denominator.",
    "These samples are tiny by design; no causal claim is made about PoW changing any outcome.",
)


# ---------------------------------------------------------------------------
# time
# ---------------------------------------------------------------------------

def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value):
    """A timestamp from this database as an aware UTC datetime, or None.

    The pipeline writes naive local time (state.now()), the artifacts may carry 'Z', and a human
    may type '+00:00'. A naive value is read as local time, which is what wrote it.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00").replace("z", "+00:00"))
        except ValueError:
            dt = None
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S",
                        "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(s[:26], fmt)
                    break
                except ValueError:
                    continue
            if dt is None:
                return None
    if dt.tzinfo is None:
        dt = dt.astimezone()          # naive: the writer used local time
    return dt.astimezone(timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# exact URL identity
# ---------------------------------------------------------------------------

def url_identity(u):
    """The exact posting identity a URL stands for: pow_store.url_key, the host's `powUrlKey`.

    S.norm_url() folds the whole query away, so `?gh_jid=1` and `?gh_jid=2` collapse into one key.
    Attribution must not do that: `pow_artifacts.url_key` and `pow_events.url_key` both hold this
    identity (host + path + identifying query; fragment, trailing slash and `utm_*` dropped), so
    every join below is an exact-URL join, and S.norm_url() is used only to find the posting in
    `postings` (its primary key is the folded key, not this identity).

    `lib/pow_store.url_key()` is the single implementation of that rule: a copy here would drift
    from the host's and pass locally while the host refuses the same posting.

    Returns "" for an empty value. A bare url_key (no scheme) is not a URL and is used as-is by
    callers instead of being passed through here.
    """
    s = (u or "").strip()
    if not s:
        return ""
    if pow_store is None:
        raise RuntimeError("lib/pow_store.py is required for exact URL identity (it owns the "
                           "host's powUrlKey rule): %s" % _STORE_ERROR)
    return pow_store.url_key(s)


def _details(blob):
    try:
        d = json.loads(blob or "{}")
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def _postings_by_identity(conn):
    """Company/title lookup keyed by exact identity, because `postings` is keyed by norm_url.

    pow_artifacts.url_key is the host identity and `postings.url_key` is S.norm_url, so joining
    them by equality would silently lose every artifact whose posting URL carries a query. Both
    the posting's url and apply_url index into the same row; the folded key is kept as the
    last-resort match for artifacts written from a bare --key.
    """
    by_ident, by_key = {}, {}
    for p in conn.execute("SELECT url_key, url, apply_url, company, title FROM postings"):
        by_key.setdefault(p["url_key"] or "", p)
        for cand in (p["url"], p["apply_url"]):
            ident = url_identity(cand)
            if ident:
                by_ident.setdefault(ident, p)
    return by_ident, by_key


def _posting_of(row, by_ident, by_key):
    """The postings row behind an artifact or event url_key, or a blank stand-in."""
    hit = by_ident.get(row["url_key"] or "") or by_key.get(row["url_key"] or "")
    if hit is not None:
        return hit
    return {"company": row["company"] if "company" in row.keys() else "",
            "title": row["title"] if "title" in row.keys() else ""}


# ---------------------------------------------------------------------------
# schema presence
# ---------------------------------------------------------------------------

def ensure_schema(conn):
    """Ask lib/pow_store.py for its tables. It is the single owner of the pow_* DDL."""
    if pow_store is None:
        raise RuntimeError("lib/pow_store.py is required for the pow_* tables: %s" % _STORE_ERROR)
    return pow_store.ensure_schema(conn)


def tables_present(conn):
    """True when both tables already exist, so readers never create state as a side effect."""
    try:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('pow_artifacts','pow_events')")}
    except sqlite3.Error:
        return False
    return names == {"pow_artifacts", "pow_events"}


# ---------------------------------------------------------------------------
# recording (the only writer in this module)
# ---------------------------------------------------------------------------

def _artifact(conn, artifact_id):
    return conn.execute("SELECT * FROM pow_artifacts WHERE artifact_id=?",
                        (artifact_id,)).fetchone()


def _has_event(conn, artifact_id, kinds):
    marks = ",".join("?" * len(kinds))
    return conn.execute("SELECT 1 FROM pow_events WHERE artifact_id=? AND kind IN (%s) LIMIT 1"
                        % marks, [artifact_id] + list(kinds)).fetchone() is not None


def _same_url(a, b):
    """Two public URLs that denote the same page: identical except for surrounding space and one
    trailing slash. Anything else is a different URL and therefore different evidence."""
    return (a or "").strip().rstrip("/") == (b or "").strip().rstrip("/")


def _artifacts_by_published_url(conn, url):
    """Every artifact whose stored public page is exactly this URL (trailing slash aside).

    A published/attached *event* proves a page exists; it does not prove the sent message carried
    that page's link. The operator's `--sent-artifact-url` claim is checked against this stored
    value, and only an exact, unambiguous match credits the send.
    """
    hits = []
    for row in conn.execute("SELECT artifact_id, published_url FROM pow_artifacts "
                            "WHERE published_url IS NOT NULL AND published_url <> ''"):
        if _same_url(row["published_url"], url):
            hits.append(row["artifact_id"])
    return hits


def _ref_mentions(ref, url):
    """True when the evidence reference itself names the artifact's public URL or its slug.

    The pipeline has no Gmail read scope, so the only content check it can make is on what the
    operator wrote down: a receipt/thread reference such as `gmail:18f2aa#/p/abc123` names the
    slug, while a bare message id cannot prove what the message contained. This is a substring
    test, not a claim of independent verification.
    """
    ref = (ref or "").strip().lower()
    url = (url or "").strip().lower()
    if not ref or not url:
        return False
    if url in ref or url.rstrip("/") in ref:
        return True
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    return len(slug) >= 6 and slug in ref


def _cohort_for(conn, artifact_id, url_key, at=None):
    """The cohort fixed when the artifact was generated/published/attached, at or before `at`.

    Never supplied by whoever records an outcome later: a cohort decided after seeing the reply
    would be re labelling, and every cohort split in the report would inherit that bias. For a
    `sent` this is the last cohort fixed at or before the send (`at`), so a publish that happens
    after the send can never become the send's cohort. A url-level cohort only counts when the
    url_key has exactly one artifact behind it — otherwise it labels a different artifact.
    """
    cutoff = " AND occurred_at <= ?" if at else ""
    if artifact_id:
        row = conn.execute(
            "SELECT cohort FROM pow_events WHERE artifact_id=? AND cohort IS NOT NULL "
            "AND cohort<>''" + cutoff + " ORDER BY occurred_at DESC, event_id DESC LIMIT 1",
            [artifact_id] + ([at] if at else [])).fetchone()
        if row and row["cohort"]:
            return row["cohort"]
    if url_key:
        n = conn.execute("SELECT COUNT(*) AS n FROM pow_artifacts WHERE url_key=?",
                         (url_key,)).fetchone()["n"]
        if n == 1:
            row = conn.execute(
                "SELECT cohort FROM pow_events WHERE url_key=? AND cohort IS NOT NULL "
                "AND cohort<>''" + cutoff + " ORDER BY occurred_at DESC, event_id DESC LIMIT 1",
                [url_key] + ([at] if at else [])).fetchone()
            if row and row["cohort"]:
                return row["cohort"]
    return None


def record_event(conn, kind, url_key=None, artifact_id=None, evidence_ref=None,
                 sent_artifact_url=None, occurred_at=None, cohort=None, details=None,
                 allow_duplicate=False):
    """Record one attribution event. Returns {"event_id", "duplicate", "unverified", ...}.

    Rejections are deliberate: an unknown kind, an outcome without evidence, a draft id offered
    as evidence, an unknown artifact id, an unparseable timestamp, the artifact's own published
    URL as proof, `cohort` on an outcome (cohort is fixed at publish/attach time), a reply-style
    outcome before its send, a `--sent-artifact-url` that contradicts the cited artifact, and a
    re-record that would credit a message already stored as an unverified send (the ledger is
    append-only, so that is refused instead of silently mutated).

    `url_key` is normalised to the exact posting identity (pow_store.url_key): a query-distinct
    URL is a different posting, so it neither joins an artifact nor credits its send. A `sent`
    with only `sent_artifact_url` resolves the artifact from the stored `published_url`; with no
    artifact id and exactly one artifact behind the URL, a url-level event joins it.

    A `sent` is credited to an artifact only when the caller attests — and the attestation is
    checked against the stored `pow_artifacts.published_url` — that the *actually sent message*
    carried that public URL (`sent_artifact_url`), together with the real message evidence. That
    attestation is the whole proof: an evidence reference that happens to name the link is
    recorded as `details.reference_names_link` and never promoted into independent verification. A
    published or attached event proves a page exists; it does not prove what the recipient's copy
    contained, so it is not accepted as link evidence. Every other send is stored as an unverified
    control event (`artifact_id` NULL, `details.pow_unverified`), never as the artifact's success.
    """
    ensure_schema(conn)
    kind = (kind or "").strip().lower()
    if kind not in KNOWN_KINDS:
        raise ValueError("unknown kind %r (expected one of: %s)" % (kind, ", ".join(KNOWN_KINDS)))

    ref = (evidence_ref or "").strip() or None
    if kind in EVIDENCE_REQUIRED:
        if not ref:
            raise ValueError("%s needs --evidence-ref: the external record that proves it "
                             "(message id, receipt subject, sheet row), not a draft id" % kind)
        if DRAFT_RE.search(ref):
            raise ValueError("%s cannot cite %r: a draft id is not evidence of sending or of a "
                             "reply. Record `sent` only after observing the send itself." % (kind, ref))

    link_url = (sent_artifact_url or "").strip() or None
    if link_url and kind != "sent":
        raise ValueError("--sent-artifact-url is only meaningful on `sent`; %s is not a send" % kind)

    details = dict(details or {})
    raw_url = (url_key or "").strip()
    art = None
    if artifact_id:
        art = _artifact(conn, artifact_id)
        if art is None:
            raise ValueError("unknown artifact_id %r: events must cite an artifact row that "
                             "already exists" % artifact_id)

    # url_key holds the exact posting identity (pow_store.url_key), never S.norm_url: two
    # query-distinct URLs are two postings, and folding them here would credit one's send to the
    # other. S.norm_url is only how `postings` is looked up.
    url_key = url_identity(raw_url) if "://" in raw_url else raw_url
    if kind == "sent" and link_url and not artifact_id:
        # The documented way to record a send is to paste the URL out of the message that was
        # actually sent. It has to identify the artifact on its own, without --url/--artifact-id.
        hits = _artifacts_by_published_url(conn, link_url)
        if len(hits) == 1:
            artifact_id = hits[0]
            art = _artifact(conn, artifact_id)
    if not url_key and art is not None:
        url_key = art["url_key"] or ""
    if not url_key and not artifact_id:
        if kind == "sent" and link_url:
            raise ValueError("--sent-artifact-url %r does not identify exactly one artifact with "
                             "that stored published_url; pass --url/--url-key or --artifact-id"
                             % link_url)
        raise ValueError("an event needs --url/--url-key, --artifact-id, or --sent-artifact-url")
    if raw_url and "://" in raw_url:
        # Remote joins (host attach/revoke, draft matching) use the exact posting URL, so keep it
        # verbatim in the evidence; url_key is only the local join key.
        details.setdefault("posting_url", raw_url)

    if (kind in EVIDENCE_REQUIRED and ref and art is not None and art["published_url"]
            and _same_url(ref, art["published_url"])):
        raise ValueError("the artifact's own published URL is not evidence of a send or a reply")

    explicit_cohort = (cohort or "").strip() or None
    if explicit_cohort and kind not in CONTEXT_KINDS:
        raise ValueError("cohort is fixed when the artifact is published/attached, not when a "
                         "%s event is recorded; remove --cohort" % kind)

    when = occurred_at or utc_now()
    ts = parse_ts(when)
    if ts is None:
        raise ValueError("--occurred-at %r is not ISO 8601 (2026-11-04T18:30:00Z)" % (when,))
    occurred = _iso(ts)

    # URL-level events belong to an artifact only when exactly one exists behind that URL.
    if not artifact_id and url_key:
        rows = conn.execute("SELECT artifact_id FROM pow_artifacts WHERE url_key=?",
                            (url_key,)).fetchall()
        if len(rows) == 1:
            artifact_id = rows[0]["artifact_id"]
            art = _artifact(conn, artifact_id)

    requested_id = artifact_id
    unverified = False
    unverified_reason = None
    if kind == "sent":
        if link_url:
            if (artifact_id and art is not None and art["published_url"]
                    and not _same_url(art["published_url"], link_url)):
                raise ValueError("--sent-artifact-url %r does not match %s's published URL %r"
                                 % (link_url, artifact_id, art["published_url"]))
            hits = _artifacts_by_published_url(conn, link_url)
            if not hits:
                unverified_reason = ("no artifact has that published URL, so the sent message's "
                                     "link cannot be tied to one")
            elif len(hits) > 1:
                unverified_reason = ("that published URL belongs to %d artifacts" % len(hits))
            elif artifact_id and hits[0] != artifact_id:
                unverified_reason = ("the artifact with that published URL is %s, not the cited %s"
                                     % (hits[0], artifact_id))
            else:
                if not artifact_id:
                    artifact_id, art = hits[0], _artifact(conn, hits[0])
                if (raw_url and "://" in raw_url and art is not None
                        and art["url_key"] != url_key):
                    unverified_reason = ("--url %r is a different posting from %s's (%s): the same "
                                         "host and path with another query is another posting, so "
                                         "this send is not credited to it"
                                         % (raw_url, artifact_id, art["url_key"]))
                else:
                    details["sent_artifact_url"] = link_url
                    # One proof class, and it is the operator's word: they attest that the message
                    # that was actually sent carried this URL. The reference text naming the link
                    # is recorded as a note, never promoted into independent verification — the
                    # pipeline cannot read the message at all.
                    details["link_proof"] = "operator-attested"
                    details["attestation"] = ("operator attests the sent message carries this "
                                              "artifact's public URL")
                    if _ref_mentions(ref, link_url):
                        details["reference_names_link"] = True
        else:
            unverified_reason = ("no --sent-artifact-url: nothing proves the sent message carried "
                                 "the artifact's link")
        if unverified_reason:
            unverified = True
            details["pow_unverified"] = True
            details["unverified_reason"] = unverified_reason
            if link_url:
                details["sent_artifact_url"] = link_url
            if requested_id:
                details["requested_artifact_id"] = requested_id
            artifact_id, art = None, None

    if kind in RATE_KINDS and artifact_id and not _has_event(conn, artifact_id, ("sent",)):
        raise ValueError("%s for %s needs a recorded, evidence-backed send first: record "
                         "`sent` with the message id, then the outcome" % (kind, artifact_id))

    fixed_cohort = explicit_cohort if kind in CONTEXT_KINDS else _cohort_for(
        conn, artifact_id, url_key, at=occurred)
    blob = json.dumps(details, ensure_ascii=False, sort_keys=True) if details else None

    def _existing():
        if ref:
            return conn.execute(
                "SELECT event_id, kind, url_key, artifact_id, occurred_at, IFNULL(evidence_ref,'') "
                "AS evidence_ref, IFNULL(cohort,'') AS cohort, details_json FROM pow_events "
                "WHERE kind=? AND evidence_ref=? ORDER BY event_id LIMIT 1", (kind, ref)).fetchone()
        return conn.execute(
            "SELECT event_id, kind, url_key, artifact_id, occurred_at, IFNULL(evidence_ref,'') "
            "AS evidence_ref, IFNULL(cohort,'') AS cohort, details_json FROM pow_events "
            "WHERE kind=? AND occurred_at=? AND IFNULL(evidence_ref,'')='' "
            "AND IFNULL(artifact_id,'')=? AND url_key=?",
            (kind, occurred, artifact_id or "", url_key)).fetchone()

    if not allow_duplicate:
        stored = _existing()
        if stored is not None:
            if kind == "sent" and not unverified and not stored["artifact_id"]:
                # The stored row is an unverified send for this exact message. The ledger is
                # append-only, so crediting it now would either rewrite that row (a silent
                # mutation of evidence) or add a second, contradictory claim on one message.
                raise ValueError(
                    "%s %r is already stored as an unverified send (event %d, artifact_id NULL: "
                    "%s). Nothing was written and the stored row stands. Re-recording the same "
                    "reference cannot credit it; record the send under the reference that names "
                    "the link (for example the message id plus the artifact's slug) if that is "
                    "what you actually have."
                    % (kind, ref, stored["event_id"],
                       _details(stored["details_json"]).get("unverified_reason")
                       or "unverified"))
            return _stored_result(stored)

    with S.tx(conn):
        cur = conn.execute(
            "INSERT OR IGNORE INTO pow_events (artifact_id, url_key, kind, occurred_at, "
            "evidence_ref, cohort, details_json) VALUES (?,?,?,?,?,?,?)",
            (artifact_id, url_key, kind, occurred, ref, fixed_cohort, blob))
        event_id = cur.lastrowid if cur.rowcount == 1 else None
    if event_id is None:                      # this (kind, evidence_ref) already existed
        stored = _existing()
        if stored is None:
            raise RuntimeError("%s %r was not written and no stored row matches it" % (kind, ref))
        return _stored_result(stored)
    return {"event_id": event_id, "duplicate": False, "unverified": unverified, "kind": kind,
            "url_key": url_key, "artifact_id": artifact_id, "occurred_at": occurred,
            "evidence_ref": ref, "cohort": fixed_cohort}


def _stored_result(row):
    """A duplicate reports what is stored, never what the caller asked for.

    An unverified send stays unverified in the answer even when the re-record asked for credit:
    saying `unverified: false` about an `artifact_id: null` row is a false success.
    """
    det = _details(row["details_json"])
    return {"event_id": row["event_id"], "duplicate": True,
            "unverified": bool(det.get("pow_unverified")), "kind": row["kind"],
            "url_key": row["url_key"] or "", "artifact_id": row["artifact_id"],
            "occurred_at": row["occurred_at"], "evidence_ref": row["evidence_ref"],
            "cohort": row["cohort"] or None}


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------

def _blank_bucket():
    return {"artifacts": 0, "published": 0, "sent": 0, "matured_sent": 0, "censored": 0,
            "pending": 0, "unsent": 0, "reply": 0, "positive_reply": 0, "interview": 0,
            "offer": 0, "no_outcome_recorded": 0, "cost_usd": 0.0, "cost_unknown": 0,
            "control_sent": 0, "unverified_sent": 0, "sent_link_attested": 0,
            "sent_ref_names_link": 0, "drafts_without_send": 0,
            "late_reply": 0, "late_positive_reply": 0, "late_interview": 0, "late_offer": 0}


def _rates(bucket):
    of = bucket["matured_sent"]
    return {k: {"n": bucket[k], "of": of, "pct": round(100.0 * bucket[k] / of, 1) if of else None}
            for k in RATE_KINDS}


def _latency(pairs):
    """median/min/max hours for [(sent_dt, outcome_dt), ...]."""
    hours = sorted((b - a).total_seconds() / 3600.0 for a, b in pairs if b >= a)
    if not hours:
        return {"n": 0, "median_h": None, "min_h": None, "max_h": None}
    return {"n": len(hours), "median_h": round(median(hours), 1),
            "min_h": round(hours[0], 1), "max_h": round(hours[-1], 1)}


def _cost_of(details_blob):
    try:
        d = json.loads(details_blob or "{}")
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    for key in ("cost_usd", "cost", "spend_usd"):
        v = d.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    return None


def _artifact_index(conn, days, horizon_days, cohort, now):
    """Artifacts in the window, their evidence timeline, and URL-level/control events.

    Every join is on the exact posting identity stored in url_key (pow_store.url_key), never on
    S.norm_url: a send recorded against `?gh_jid=2` is not the artifact generated for `?gh_jid=1`.
    `postings` is keyed by the folded S.norm_url, so company/title are resolved through the
    posting URL instead of raw equality.

    Returns (arts, control, events_in_window, start, horizon) where `arts` is a list of dicts.
    """
    start = now - timedelta(days=days)
    horizon = now - timedelta(days=horizon_days)
    rows = conn.execute("SELECT * FROM pow_artifacts").fetchall()
    ev_rows = conn.execute("SELECT * FROM pow_events ORDER BY occurred_at").fetchall()
    by_ident, by_key = _postings_by_identity(conn)

    per_artifact, per_url = {}, {}
    for r in ev_rows:
        det = _details(r["details_json"])
        ev = {"event_id": r["event_id"], "kind": r["kind"], "occurred_at": r["occurred_at"],
              "ts": parse_ts(r["occurred_at"]), "evidence_ref": r["evidence_ref"],
              "cohort": r["cohort"], "details_json": r["details_json"],
              "unverified": bool(det.get("pow_unverified")),
              # a note on the reference text, never a second proof class: older rows wrote
              # "reference-contains-artifact-url" for the same fact
              "reference_names_link": bool(det.get("reference_names_link"))
                                     or det.get("link_proof") == "reference-contains-artifact-url",
              "posting_url": det.get("posting_url") or None,
              "artifact_id": r["artifact_id"], "url_key": r["url_key"] or ""}
        if r["artifact_id"]:
            per_artifact.setdefault(r["artifact_id"], []).append(ev)
        else:
            per_url.setdefault(ev["url_key"], []).append(ev)

    url_counts = {}
    for r in rows:
        key = r["url_key"] or ""
        url_counts[key] = url_counts.get(key, 0) + 1

    # An artifact belongs to the window when it was created in it OR when it produced an event
    # in it: a reply that lands five weeks after the send is the whole point of attribution, so
    # the artifact must not vanish from the report before its outcome arrives.
    active_ids, active_urls = set(), set()
    for evs in per_artifact.values():
        if any(e["ts"] is not None and e["ts"] >= start for e in evs):
            active_ids.update(e["artifact_id"] for e in evs)
    for key, evs in per_url.items():
        if any(e["ts"] is not None and e["ts"] >= start for e in evs):
            active_urls.add(key)

    arts = []
    for r in rows:
        key = r["url_key"] or ""
        posting = _posting_of(r, by_ident, by_key)
        created = parse_ts(r["created_at"])
        if created is not None and created < start:
            active = (r["artifact_id"] in active_ids
                      or (url_counts.get(key, 0) == 1 and key in active_urls))
            if not active:
                continue
        evs = list(per_artifact.get(r["artifact_id"], []))
        # URL-level events fold into the artifact only when it is the single artifact behind that
        # exact URL — and never when they are unverified sends, which are attribution the artifact
        # has not earned.
        url_evs = [e for e in per_url.get(key, [])
                   if url_counts.get(key, 0) == 1 and not e["unverified"]]
        evs += url_evs
        evs.sort(key=lambda e: (e["ts"] or now, e["event_id"]))
        sent = next((e for e in evs if e["kind"] == "sent"), None)
        sent_ts = sent["ts"] if sent else None
        age_days = round((now - created).total_seconds() / 86400.0, 1) if created else None
        # The cohort is the last one fixed at or before the send: an event that happens after the
        # send can never become the send's cohort. Without a send, the artifact's own last context
        # event labels it.
        cohort_of = (sent or {}).get("cohort") or None
        if not cohort_of:
            seen = [e for e in evs if e["kind"] in CONTEXT_KINDS and e["cohort"] and e["ts"]
                    and (sent_ts is None or e["ts"] <= sent_ts)]
            cohort_of = seen[-1]["cohort"] if seen else None
        window_end = sent_ts + timedelta(days=horizon_days) if sent_ts is not None else None
        after = [e for e in evs if sent_ts is not None and e["ts"] and e["ts"] >= sent_ts]
        in_window = [e for e in after if window_end is not None and e["ts"] <= window_end]
        late = [e for e in after if window_end is not None and e["ts"] > window_end]
        outcomes, late_outcomes, latency = {}, {}, {}
        for k in RATE_KINDS:
            hits = [e for e in in_window if e["kind"] == k]
            if hits:
                outcomes[k] = len(hits)
                latency[k] = _latency([(sent_ts, e["ts"]) for e in hits if e["ts"]])
            late_hits = [e for e in late if e["kind"] == k]
            if late_hits:
                late_outcomes[k] = len(late_hits)
        cost_vals = [c for c in (_cost_of(e["details_json"]) for e in evs) if c is not None]
        arts.append({
            "artifact_id": r["artifact_id"], "url_key": key,
            "company": posting["company"] or "", "title": posting["title"] or "",
            "recipe": r["recipe"] or "", "status": r["status"] or "",
            "created_at": r["created_at"], "age_days": age_days,
            "published": bool(r["published_url"]), "published_url": r["published_url"],
            "gmail_draft_id": r["gmail_draft_id"], "cohort": cohort_of,
            "posting_url": next((e["posting_url"] for e in evs if e["posting_url"]), None),
            "sent": sent_ts is not None, "sent_at": _iso(sent_ts) if sent_ts else None,
            "link_proof": ((sent or {}).get("link_proof") or "operator-attested")
                          if sent_ts else None,
            "reference_names_link": bool((sent or {}).get("reference_names_link")),
            "observation_end": _iso(window_end) if window_end else None,
            "matured": bool(sent_ts is not None and sent_ts <= horizon),
            "censored": bool(sent_ts is not None and sent_ts > horizon),
            "outcomes": outcomes, "late_outcomes": late_outcomes, "latency_hours": latency,
            "event_kinds": _counts(e["kind"] for e in evs),
            "cost_usd": round(sum(cost_vals), 4) if cost_vals else None,
            "cost_unknown": bool(sent_ts is not None and not cost_vals),
            "unlinked_events": len(url_evs),
            "unverified_sends": sum(1 for e in per_url.get(key, [])
                                    if e["kind"] == "sent" and e["unverified"]),
            "url_events": [{"kind": e["kind"], "occurred_at": e["occurred_at"],
                            "evidence_ref": e["evidence_ref"], "cohort": e["cohort"],
                            "unverified": False} for e in url_evs],
        })
    reported_keys = {a["url_key"] for a in arts}
    arts = [a for a in arts if not cohort or (a["cohort"] or "") == cohort]

    # URL-level sends that are not folded into a reported artifact: no artifact exists behind the
    # exact URL (a plain control send), several do (picking one would be invention), or the one
    # behind it is outside this window.
    control = {"sends": 0, "unverified": 0, "with_artifact_ambiguous": 0, "outside_window": 0,
               "urls": []}
    for key, evs in per_url.items():
        sends = [e for e in evs if e["kind"] == "sent" and (not cohort or e["cohort"] == cohort)]
        if not sends:
            continue
        url = next((e["posting_url"] for e in sends if e["posting_url"]), key or "(no url)")
        bad = [e for e in sends if e["unverified"]]
        sends = [e for e in sends if not e["unverified"]]
        if bad:
            control["unverified"] += len(bad)
            control["urls"].append({"url": url, "url_key": key or "(no url)", "sent": len(bad),
                                    "note": "unverified: no proof the sent message carried an "
                                            "artifact URL"})
        if not sends:
            continue
        n = url_counts.get(key, 0)
        if n > 1:
            control["with_artifact_ambiguous"] += len(sends)
            control["urls"].append({"url": url, "url_key": key or "(no url)",
                                    "sent": len(sends),
                                    "note": "%d artifacts share this URL; not attributed" % n})
        elif n == 1 and key in reported_keys:
            continue          # already folded into that artifact above
        elif n == 1:
            control["outside_window"] += len(sends)
            control["urls"].append({"url": url, "url_key": key or "(no url)",
                                    "sent": len(sends),
                                    "note": "its artifact is outside this window or cohort"})
        else:
            control["sends"] += len(sends)
            control["urls"].append({"url": url, "url_key": key or "(no url)",
                                    "sent": len(sends),
                                    "note": "no artifact behind this exact URL: control send"})

    events_in_window = [{"kind": r["kind"], "occurred_at": r["occurred_at"],
                         "evidence_ref": r["evidence_ref"], "artifact_id": r["artifact_id"],
                         "url_key": r["url_key"] or "", "cohort": r["cohort"]}
                        for r in ev_rows
                        if (parse_ts(r["occurred_at"]) or datetime.min.replace(tzinfo=timezone.utc)) >= start
                        and (not cohort or r["cohort"] == cohort)]
    return arts, control, events_in_window, start, horizon


def _counts(seq):
    out = {}
    for x in seq:
        out[x] = out.get(x, 0) + 1
    return out


def _fill_bucket(bucket, art, horizon):
    """One artifact into one bucket.

    An artifact with a send is matured (rate base) or censored (sent inside the window, so it has
    not had the full observation window to produce an outcome). An artifact without a send is
    pending (younger than the window: it could still be sent) or unsent (matured without one).
    Only matured sends are a denominator, and a reply counts once per artifact, not per email.
    Outcomes that arrived after the send's own observation window are counted in `late_*`, so
    they stay visible without entering the rate or the latency.
    """
    bucket["artifacts"] += 1
    bucket["published"] += 1 if art["published"] else 0
    created = parse_ts(art["created_at"])
    if art["sent"]:
        bucket["sent"] += 1
        # Every credited send rests on the operator's attestation and nothing stronger; the
        # reference merely naming the link is a note, counted separately and never as proof.
        bucket["sent_link_attested"] += 1
        bucket["sent_ref_names_link"] += 1 if art.get("reference_names_link") else 0
        bucket["cost_usd"] = round(bucket["cost_usd"] + (art["cost_usd"] or 0.0), 4)
        bucket["cost_unknown"] += 1 if art["cost_unknown"] else 0
        if art["matured"]:
            bucket["matured_sent"] += 1
            for k in RATE_KINDS:
                bucket[k] += 1 if art["outcomes"].get(k) else 0
                bucket["late_" + k] += 1 if art["late_outcomes"].get(k) else 0
            if not any(art["outcomes"].get(k) for k in RATE_KINDS):
                bucket["no_outcome_recorded"] += 1
        else:
            bucket["censored"] += 1
    elif created is not None and created > horizon:
        bucket["pending"] += 1
    else:
        # No parsed created_at means age is unknown; that is not youth, so it is not "pending".
        bucket["unsent"] += 1
    if art["gmail_draft_id"] and not art["sent"]:
        bucket["drafts_without_send"] += 1


def snapshot(conn, days=30, horizon_days=14, cohort=None, now=None, create=False):
    """The attribution report: counts, denominators, cohorts, latencies, unknowns.

    `create=False` keeps readers (digest, retro) from creating tables as a side effect: when the
    PoW tables do not exist yet the report says so instead of inventing zeroes.
    """
    now = now or datetime.now(timezone.utc)
    if create:
        ensure_schema(conn)
    elif not tables_present(conn):
        return {"available": False, "reason": "pow tables not created yet (pow.py has never run)",
                "generated": _iso(now), "days": days, "horizon_days": horizon_days}

    arts, control, events_in_window, start, horizon = _artifact_index(
        conn, days, horizon_days, cohort, now)

    totals = _blank_bucket()
    by_cohort, by_recipe, by_status = {}, {}, {}
    for art in arts:
        _fill_bucket(totals, art, horizon)
        label = art["cohort"] or "unassigned"
        _fill_bucket(by_cohort.setdefault(label, _blank_bucket()), art, horizon)
        by_recipe[art["recipe"] or "unknown"] = by_recipe.get(art["recipe"] or "unknown", 0) + 1
        by_status[art["status"] or "unknown"] = by_status.get(art["status"] or "unknown", 0) + 1
    totals["control_sent"] = control["sends"]
    totals["unverified_sent"] = control["unverified"]

    lat = {}
    for art in arts:
        for kind in RATE_KINDS:
            stats = art["latency_hours"].get(kind)
            if not stats or not stats["n"]:
                continue
            entry = lat.setdefault(kind, {"n": 0, "per_artifact_h": []})
            entry["n"] += stats["n"]
            if stats["median_h"] is not None:
                entry["per_artifact_h"].append(stats["median_h"])
    # An artifact is the unit of attribution, so the window median is the median of the
    # per-artifact medians; n is the number of observed sent→outcome gaps.
    for kind, entry in lat.items():
        vals = entry.pop("per_artifact_h")
        entry["median_h"] = round(median(vals), 1) if vals else None
        entry["min_h"] = min((a["latency_hours"][kind]["min_h"] for a in arts
                              if a["latency_hours"].get(kind)), default=None)
        entry["max_h"] = max((a["latency_hours"][kind]["max_h"] for a in arts
                              if a["latency_hours"].get(kind)), default=None)

    by_kind = _counts(e["kind"] for e in events_in_window)
    evidence_missing = [e for e in events_in_window
                        if e["kind"] in EVIDENCE_REQUIRED and not e["evidence_ref"]]
    late = {k: totals["late_" + k] for k in RATE_KINDS}
    late_total = sum(late.values())

    cohorts = []
    for label in sorted(by_cohort):
        b = by_cohort[label]
        cohorts.append({"cohort": label, "counts": b, "rates": _rates(b)})

    unknowns = []
    if totals["unverified_sent"]:
        unknowns.append({
            "code": "sent_unverified", "count": totals["unverified_sent"],
            "what": "%d send(s) lack proof that the sent message contained the artifact URL"
                    % totals["unverified_sent"],
            "note": "They are stored as control sends (artifact_id NULL, pow_unverified, with the "
                    "reason) and are never credited to an artifact. Record --sent-artifact-url "
                    "matching the stored published_url to credit a send."})
    if totals["drafts_without_send"]:
        unknowns.append({
            "code": "draft_without_send", "count": totals["drafts_without_send"],
            "what": "%d artifact(s) have a Gmail draft but no evidence-backed send"
                    % totals["drafts_without_send"],
            "note": "The draft may have been sent by hand; record it with the message id when observed."})
    if totals["no_outcome_recorded"]:
        unknowns.append({
            "code": "no_outcome_recorded", "count": totals["no_outcome_recorded"],
            "what": "%d matured send(s) have no reply/interview/offer event"
                    % totals["no_outcome_recorded"],
            "note": "Not evidence of no reply: the pipeline has no open tracking and records only "
                    "explicit observations."})
    if totals["cost_unknown"]:
        unknowns.append({
            "code": "cost_unknown", "count": totals["cost_unknown"],
            "what": "%d sent artifact(s) have no recorded generation cost" % totals["cost_unknown"],
            "note": "Cost is only shown when the generator (or the operator) recorded cost_usd in "
                    "event details."})
    if control["with_artifact_ambiguous"]:
        unknowns.append({
            "code": "url_level_events", "count": control["with_artifact_ambiguous"],
            "what": "%d send(s) sit on a URL with several artifacts and were not attributed"
                    % control["with_artifact_ambiguous"],
            "note": "Record with --artifact-id to attribute a specific artifact."})
    if evidence_missing:
        unknowns.append({
            "code": "missing_evidence", "count": len(evidence_missing),
            "what": "%d outcome event(s) in the window carry no evidence reference"
                    % len(evidence_missing),
            "note": "Written outside this module; treated as unverified by any later report."})
    if totals["pending"]:
        unknowns.append({
            "code": "pending", "count": totals["pending"],
            "what": "%d artifact(s) are younger than the %d-day observation window"
                    % (totals["pending"], horizon_days),
            "note": "Too young to classify as sent or unsent; excluded from every rate."})
    if late_total:
        unknowns.append({
            "code": "late_outcome", "count": late_total,
            "what": "%d outcome(s) arrived after their send's %d-day observation window"
                    % (late_total, horizon_days),
            "note": "Counted separately (counts.late_*, late_outcomes) and excluded from every "
                    "rate and latency, which are in-window rates. They are never silently "
                    "dropped."})

    return {
        "available": True,
        "generated": _iso(now),
        "days": days,
        "horizon_days": horizon_days,
        "window": {"start": _iso(start), "end": _iso(now),
                   "observation_start": _iso(horizon),
                   "basis": "artifact created in the window, or with an event in it"},
        "cohort_filter": cohort,
        "counts": totals,
        "rates": _rates(totals),
        "late_outcomes": late,
        "latency_hours": lat,
        "cohorts": cohorts,
        "recipes": by_recipe,
        "statuses": by_status,
        "events": {"total": len(events_in_window), "by_kind": by_kind,
                   "with_evidence": len(events_in_window) - len(evidence_missing),
                   "without_evidence": len(evidence_missing)},
        "control": control,
        "unknowns": unknowns,
        "caveats": list(CAVEATS),
        "artifacts": sorted(arts, key=lambda a: (a["created_at"] or "", a["artifact_id"]),
                            reverse=True)[:200],
    }


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def _pct(rate):
    return "n/a" if rate["pct"] is None else "%.1f%%" % rate["pct"]


def _rate_line(rate, label):
    return "%s %d/%d (%s)" % (label, rate["n"], rate["of"], _pct(rate))


def render_text(snap):
    """Human report. Counts first, percentages second, denominators always visible."""
    if not snap.get("available"):
        return "PoW attribution unavailable: %s" % snap.get("reason")
    c, r = snap["counts"], snap["rates"]
    L = ["PoW attribution — %d-day artifact window, %d-day observation window"
         % (snap["days"], snap["horizon_days"]),
         "generated %s · window %s → %s · cohort %s"
         % (snap["generated"], snap["window"]["start"], snap["window"]["end"],
            snap["cohort_filter"] or "all"),
         "window rule: %s · observation window starts %s"
         % (snap["window"].get("basis") or "created in the window",
            snap["window"].get("observation_start") or "?"),
         "",
         "artifacts %d · published %d · sent %d · matured %d · censored %d · pending %d · unsent %d"
         % (c["artifacts"], c["published"], c["sent"], c["matured_sent"], c["censored"],
            c["pending"], c["unsent"]),
         "outcomes of matured sends: " + " · ".join(_rate_line(r[k], k) for k in RATE_KINDS),
         "recipes: " + (", ".join("%s %d" % kv for kv in sorted(snap["recipes"].items())) or "none")
         + " · statuses: " + (", ".join("%s %d" % kv for kv in sorted(snap["statuses"].items()))
                              or "none")]
    if snap["control"]["sends"] or snap["control"]["unverified"]:
        L.append("control sends: %d unlinked-artifact · %d unverified (sent-message link unproven)"
                 % (snap["control"]["sends"], snap["control"]["unverified"]))
    if c["sent"]:
        L.append("send-link proof: all %d credited send(s) are operator-attested — the pipeline "
                 "cannot read the message; %d evidence reference(s) also name the artifact URL, "
                 "which is a note, not independent proof"
                 % (c["sent_link_attested"], c["sent_ref_names_link"]))
    if snap["cohorts"]:
        L.append("")
        L.append("cohorts:")
        for co in snap["cohorts"]:
            b = co["counts"]
            L.append("  %s: artifacts %d · sent %d · matured %d · censored %d · reply %d/%d · "
                     "positive %d/%d · interview %d/%d · offer %d/%d · unsent %d · pending %d"
                     % (co["cohort"], b["artifacts"], b["sent"], b["matured_sent"], b["censored"],
                        b["reply"], b["matured_sent"], b["positive_reply"], b["matured_sent"],
                        b["interview"], b["matured_sent"], b["offer"], b["matured_sent"],
                        b["unsent"], b["pending"]))
    for kind in RATE_KINDS:
        s = snap["latency_hours"].get(kind)
        if s and s["n"]:
            L.append("latency sent→%s: n=%d median %.1fh (min %.1f, max %.1f)"
                     % (kind, s["n"], s["median_h"] or 0, s["min_h"] or 0, s["max_h"] or 0))
    late = snap.get("late_outcomes") or {}
    if sum(late.values()):
        L.append("late outcomes (arrived after the %d-day window; excluded from every rate and "
                 "latency): %s"
                 % (snap["horizon_days"],
                    " · ".join("%s %d" % (k, late[k]) for k in RATE_KINDS if late.get(k))))
    if c["sent"]:
        L.append("cost: $%.2f recorded · %d sent artifact(s) without a recorded cost"
                 % (c["cost_usd"], c["cost_unknown"]))
    if snap["events"]["total"]:
        L.append("events in window: " + " · ".join("%s %d" % kv for kv in
                                                   sorted(snap["events"]["by_kind"].items())))
    if snap["unknowns"]:
        L.append("")
        L.append("unknowns (reported, never counted as zero):")
        for u in snap["unknowns"]:
            L.append("- %s — %s" % (u["what"], u["note"]))
    L.append("")
    L.append("caveats: " + " ".join(CAVEATS))
    return "\n".join(L)


def render_markdown(snap, heading="PoW attribution (descriptive)"):
    """Compact Markdown section for the retro brief and the digest. Never a KPI to optimise."""
    if not snap or not snap.get("available"):
        return "## %s\n\n%s\n" % (heading, (snap or {}).get("reason") or "no PoW data")
    c, r = snap["counts"], snap["rates"]
    L = ["## %s" % heading, ""]
    L.append("artifacts %d (published %d) · sent %d · matured %d · censored %d · pending %d · "
             "unsent %d · control sends %d · unverified sends %d"
             % (c["artifacts"], c["published"], c["sent"], c["matured_sent"], c["censored"],
                c["pending"], c["unsent"], c["control_sent"], c["unverified_sent"]))
    L.append("matured-send outcomes: " + " · ".join(_rate_line(r[k], k) for k in RATE_KINDS))
    if snap["cohorts"]:
        L.append("cohorts: " + " · ".join(
            "%s (artifacts %d, sent %d, matured %d, reply %d/%d)"
            % (co["cohort"], co["counts"]["artifacts"], co["counts"]["sent"],
               co["counts"]["matured_sent"], co["counts"]["reply"],
               co["counts"]["matured_sent"]) for co in snap["cohorts"][:8]))
    if snap["recipes"]:
        L.append("recipes: " + ", ".join("%s %d" % kv for kv in sorted(snap["recipes"].items())))
    if c["sent"]:
        L.append("cost: $%.2f recorded · %d sent artifact(s) without a recorded cost"
                 % (c["cost_usd"], c["cost_unknown"]))
        L.append("send-link proof: %d credited send(s), all operator-attested · %d reference(s) "
                 "also name the artifact URL (a note, not proof) · %d unverified excluded"
                 % (c["sent_link_attested"], c["sent_ref_names_link"], c["unverified_sent"]))
    late = snap.get("late_outcomes") or {}
    if sum(late.values()):
        L.append("late outcomes (after the %d-day window, excluded from rates): %s"
                 % (snap["horizon_days"],
                    " · ".join("%s %d" % (k, late[k]) for k in RATE_KINDS if late.get(k))))
    if snap.get("today"):
        t = snap["today"]
        L.append("today: %d artifact(s) created · events %s"
                 % (t.get("artifacts_created", 0),
                    ", ".join("%s %d" % kv for kv in sorted((t.get("events") or {}).items()))
                    or "none"))
    for u in snap["unknowns"]:
        L.append("- unknown: %s (%s)" % (u["what"], u["note"]))
    L.append("")
    L.append("_Descriptive only: no open tracking, absence of a reply event is not a negative "
             "reply, and nothing here supports a causal or optimisation claim from this sample._")
    L.append("")
    return "\n".join(L)


def summary_line(snap):
    """One line for the daily digest, or None when there is nothing to say."""
    if not snap or not snap.get("available") or not snap["counts"]["artifacts"]:
        return None
    c, r = snap["counts"], snap["rates"]
    line = ("**PoW:** %d artifact(s) (%s) · sent %d · matured %d · replies %d/%d"
            % (c["artifacts"],
               ", ".join("%s %d" % kv for kv in sorted(snap["recipes"].items())) or "no recipe",
               c["sent"], c["matured_sent"], r["reply"]["n"], r["reply"]["of"]))
    tail = []
    if c["pending"]:
        tail.append("%d too young to judge" % c["pending"])
    if c["unsent"]:
        tail.append("%d never sent" % c["unsent"])
    if c["drafts_without_send"]:
        tail.append("%d draft(s) with unverified send" % c["drafts_without_send"])
    if c["unverified_sent"]:
        tail.append("%d unverified send(s)" % c["unverified_sent"])
    late = sum((snap.get("late_outcomes") or {}).values())
    if late:
        tail.append("%d outcome(s) after the observation window" % late)
    if tail:
        line += " · " + " · ".join(tail)
    return line


def status_rows(conn, limit=50, create=False):
    """Artifacts with their evidence timeline, newest first: what exists and what is proven.

    The URL-level events come from the report itself, so this timeline folds them by exactly the
    same rule (single artifact behind the exact URL, never an unverified send) instead of keeping
    a second, drifting join here.
    """
    if create:
        ensure_schema(conn)
    elif not tables_present(conn):
        return []
    snap = snapshot(conn, days=3650, horizon_days=14, create=create)
    rows = []
    for art in snap["artifacts"][:limit]:
        evs = [{"kind": e["kind"], "occurred_at": e["occurred_at"],
                "evidence_ref": e["evidence_ref"], "cohort": e["cohort"],
                "unverified": False}
               for e in conn.execute(
                   "SELECT kind, occurred_at, IFNULL(evidence_ref,'') AS evidence_ref, "
                   "IFNULL(cohort,'') AS cohort, details_json FROM pow_events "
                   "WHERE artifact_id=? ORDER BY occurred_at", (art["artifact_id"],))]
        evs += art.get("url_events") or []
        evs.sort(key=lambda e: e["occurred_at"] or "")
        rows.append(dict(art, events=evs))
    return rows


def pending_rows(conn, days=30, create=False):
    """Artifacts whose outcome is unverified, with the exact command that would record it.

    No Gmail call, no inference from a draft: this only prints what a human must confirm.
    """
    if create:
        ensure_schema(conn)
    elif not tables_present(conn):
        return []
    snap = snapshot(conn, days=days, create=create)
    horizon = datetime.now(timezone.utc) - timedelta(days=snap["horizon_days"])
    out = []
    for art in snap["artifacts"]:
        if art["sent"]:
            continue
        created = parse_ts(art["created_at"])
        unverified = art.get("unverified_sends", 0)
        if unverified:
            why = ("%d send(s) recorded without proof that the sent message carried the artifact "
                   "URL (pow_unverified); the ledger is append-only, so those rows stay unverified "
                   "and cannot be re-credited under the same reference" % unverified)
        elif not art["published"]:
            why = "no public page yet: a send cannot be credited until it has a published URL"
        elif art["gmail_draft_id"]:
            why = "draft exists, send unverified"
        elif created is not None and created <= horizon:
            why = "never sent"
        else:
            why = "too young to judge"
        commands = []
        if not art["published"]:
            # Publishing is pow.py's job: recording a `published` event does not set
            # pow_artifacts.published_url, which is the URL the send must be proven against.
            commands.append("python pipeline/pow.py publish %s   # requires pow.publish_enabled"
                            % art["artifact_id"])
            link = "<published_url from pow.py publish>"
        else:
            link = art["published_url"]
        commands.append("python pow_metrics.py record --kind sent --artifact-id %s "
                        "--sent-artifact-url %s --evidence-ref gmail:<message id>"
                        % (art["artifact_id"], link))
        out.append({
            "artifact_id": art["artifact_id"], "company": art["company"],
            "title": art["title"], "recipe": art["recipe"], "published": art["published"],
            "age_days": art["age_days"], "why": why, "unverified_sends": unverified,
            "commands": commands,
        })
    return out


# ---------------------------------------------------------------------------
# one-shot helpers for other stages
# ---------------------------------------------------------------------------

def read_snapshot(days=30, horizon_days=14, cohort=None):
    """snapshot() on its own short-lived connection, never creating tables."""
    conn = S.db()
    try:
        return snapshot(conn, days=days, horizon_days=horizon_days, cohort=cohort)
    finally:
        conn.close()