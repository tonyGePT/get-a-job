#!/usr/bin/env python
"""PoW attribution CLI: record evidence-backed events, report them, list what is unverified.

  python pow_metrics.py record --kind sent --sent-artifact-url <artifact url>
        --evidence-ref gmail:<message id>  [--artifact-id <id> | --url <posting url>]
        [--occurred-at <iso>] [--detail k=v ...] [--allow-duplicate]
        [--cohort <name>]   (only with generated/published/attached)
        The only writer. The five outcome kinds (sent, reply, positive_reply, interview, offer)
        all require --evidence-ref: the message id, receipt subject or sheet row that proves the
        observation. A draft id is rejected, and so is the artifact's own published URL, because
        neither proves that anything was sent or answered. A `sent` credits an artifact only when
        --sent-artifact-url exactly matches that artifact's stored published_url (trailing slash
        aside) — that link alone identifies the artifact — plus the real sent-message evidence.
        The proof is the operator's attestation that the message actually sent carried the link:
        nothing in the reference text promotes it to independent verification, because the
        pipeline cannot read the message. A published or attached event is not proof of the
        message's content either. Anything weaker is stored as an unverified control send
        (artifact_id NULL, details.pow_unverified and details.unverified_reason) and never
        inflates the artifact's numbers. When you also pass --url, it is matched against the
        artifact's posting URL as the host identifies postings (identifying query parameters
        kept, utm_*/fragment/trailing slash dropped), so a query-distinct URL is a different
        posting and does not credit this artifact. `cohort` is fixed when the artifact is
        published/attached, and a send takes the last cohort fixed at or before its own
        timestamp. Recording is insert-only: an identical (kind, evidence_ref) returns the stored
        row as it stands (a duplicate of an unverified send still reports unverified), and
        re-recording an unverified message in order to credit it is refused with an error instead
        of quietly succeeding. No Gmail call is made here and nothing is inferred from a draft.

  python pow_metrics.py report [--days 30] [--horizon-days 14] [--cohort C] [--json] [--details]
        Counts with their denominators: artifacts in the window, sent, matured sends (the rate
        base), censored sends, pending vs unsent, reply/positive_reply/interview/offer per cohort
        and recipe, latency sent→outcome, recorded cost, and every unknown stated as an unknown.
        `--days` is the artifact window; `--horizon-days` is the per-send observation window. An
        outcome that arrives after its send's window is reported as a late outcome and kept out
        of the rate and the latency. Read-only: it never creates a table and never contacts Gmail.

  python pow_metrics.py status [--limit 50] [--json]
        Artifacts newest first with their evidence timeline (kind, time, evidence reference).

  python pow_metrics.py pending [--days 3650] [--json]
        Artifacts whose send is unverified (no credited `sent` event), each with the exact
        commands to run: `pow.py publish` when the artifact has no public page yet (only that
        sets published_url), then `record --kind sent` with --sent-artifact-url and the message
        id. No Gmail call is made.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import state as S  # noqa: E402
import pow_telemetry as PT  # noqa: E402


def _detail(pairs):
    """k=v, with a JSON value when it parses (cost_usd=0.42, attempts=3, flag=true)."""
    out = {}
    for raw in pairs or ():
        if "=" not in raw:
            raise ValueError("--detail wants k=v, got %r" % raw)
        k, v = raw.split("=", 1)
        k = k.strip()
        if not k:
            raise ValueError("--detail wants k=v, got %r" % raw)
        try:
            out[k] = json.loads(v)
        except ValueError:
            out[k] = v
    return out


def cmd_record(a):
    conn = S.db()
    try:
        res = PT.record_event(
            conn, a.kind, url_key=a.url_key or a.url, artifact_id=a.artifact_id,
            evidence_ref=a.evidence_ref, sent_artifact_url=a.sent_artifact_url,
            occurred_at=a.occurred_at, cohort=a.cohort,
            details=_detail(a.detail), allow_duplicate=a.allow_duplicate)
    finally:
        conn.close()
    if res.get("unverified"):
        print("pow_metrics: stored as unverified control send — no proof that the sent message "
              "carried an artifact URL (artifact_id is NULL; see details.unverified_reason)",
              file=sys.stderr)
    print(json.dumps(res, ensure_ascii=False, sort_keys=True))
    return 0


def cmd_report(a):
    conn = S.db()
    try:
        snap = PT.snapshot(conn, days=a.days, horizon_days=a.horizon_days, cohort=a.cohort)
    finally:
        conn.close()
    if a.json:
        print(json.dumps(snap, ensure_ascii=False, indent=1))
        return 0
    print(PT.render_text(snap))
    if a.details and snap.get("available"):
        print("")
        print("| artifact | company | recipe | status | created | age d | sent | matured | outcomes |")
        print("|---|---|---|---|---|---|---|---|---|")
        for art in snap["artifacts"]:
            print("| %s | %s | %s | %s | %s | %s | %s | %s | %s |"
                  % (art["artifact_id"][:20], (art["company"] or "?")[:28], art["recipe"] or "?",
                     art["status"] or "?", art["created_at"] or "?", art["age_days"],
                     "yes" if art["sent"] else "no", "yes" if art["matured"] else "no",
                     ", ".join("%s×%d" % kv for kv in sorted(art["outcomes"].items())) or "—"))
    return 0


def cmd_status(a):
    conn = S.db()
    try:
        rows = PT.status_rows(conn, limit=a.limit)
    finally:
        conn.close()
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0
    if not rows:
        print("no PoW artifacts recorded (pow.py has not created one, or the tables are absent)")
        return 0
    for r in rows:
        print("%s  %s · %s [%s] %s · age %s d"
              % (r["artifact_id"], (r["company"] or "?")[:30], (r["title"] or "?")[:40],
                 r["recipe"] or "?", r["status"] or "?", r["age_days"]))
        print("  url %s · published %s%s"
              % (r["url_key"] or "—", "yes" if r["published"] else "no",
                 " · draft %s" % r["gmail_draft_id"] if r["gmail_draft_id"] else ""))
        for e in r["events"]:
            print("  %s %-14s %s%s" % (e["occurred_at"], e["kind"], e["evidence_ref"] or "(no ref)",
                                       "  [unverified]" if e["unverified"] else ""))
    return 0


def cmd_pending(a):
    conn = S.db()
    try:
        rows = PT.pending_rows(conn, days=a.days)
    finally:
        conn.close()
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0
    if not rows:
        print("no artifact is awaiting a verified send")
        return 0
    for r in rows:
        print("%s · %s · %s · %s" % (r["artifact_id"], r["company"] or "?", r["recipe"] or "?",
                                     r["why"]))
        for cmd in r["commands"]:
            print("  %s" % cmd)
    return 0


def main():
    ap = argparse.ArgumentParser(description="PoW attribution: evidence-backed events and counts")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("record", help="record one evidence-backed event (the only writer)")
    r.add_argument("--kind", required=True, choices=PT.KNOWN_KINDS,
                   help="outcome kinds need --evidence-ref; context kinds do not")
    r.add_argument("--url", help="posting URL (normalised to url_key)")
    r.add_argument("--url-key", help="url_key already normalised by state.norm_url")
    r.add_argument("--artifact-id")
    r.add_argument("--evidence-ref", help="external proof: message id, receipt subject, sheet row")
    r.add_argument("--sent-artifact-url",
                   help="the artifact's public URL exactly as it appeared in the message you "
                        "actually sent; must match the stored published_url to credit the send")
    r.add_argument("--occurred-at", help="ISO 8601; default now (UTC stored)")
    r.add_argument("--cohort", help="grouping label set when the artifact is published/attached "
                                    "(not accepted for outcome kinds)")
    r.add_argument("--detail", action="append", metavar="K=V",
                   help="extra facts, e.g. cost_usd=0.42 or role=gtm")
    r.add_argument("--allow-duplicate", action="store_true",
                   help="record even if an identical event exists")
    r.set_defaults(fn=cmd_record)

    p = sub.add_parser("report", help="counts, denominators, cohorts, unknowns")
    p.add_argument("--days", type=int, default=30, help="artifact window (default 30)")
    p.add_argument("--horizon-days", type=int, default=14,
                   help="observation window a send needs before it can be judged (default 14)")
    p.add_argument("--cohort")
    p.add_argument("--json", action="store_true")
    p.add_argument("--details", action="store_true", help="append the per-artifact table")
    p.set_defaults(fn=cmd_report)

    s = sub.add_parser("status", help="artifacts with their evidence timeline")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_status)

    q = sub.add_parser("pending", help="artifacts whose send is still unverified")
    q.add_argument("--days", type=int, default=3650,
                   help="artifact window (default 3650: an old unverified send is still work)")
    q.add_argument("--json", action="store_true")
    q.set_defaults(fn=cmd_pending)

    a = ap.parse_args()
    try:
        return a.fn(a)
    except ValueError as e:
        print("pow_metrics: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())