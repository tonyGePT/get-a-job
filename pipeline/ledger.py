#!/usr/bin/env python
"""The job-harness ledger: query and append to state/state.db.

This replaces application_log.md (retired 2026-11-02; its rows were imported into the
`history`, `outreach`, `resume_changes` and `log_notes` tables). The pipeline's own decisions and
outcomes are already in `postings` / `applications`; `check` looks in all of them.

  python ledger.py check --company "Acme" --title "Forward Deployed Engineer" [--url URL]
        duplicate review before opening any application form. Exit 1 = a prior record matches
        (the decision about whether it is the same requisition stays with the caller), 0 = clean.
  python ledger.py add --company C --title T --url U --action applied|unapplied|skipped|escalated
        [--status S] [--resume V] [--confidence high|low] [--signals S] [--notes N] [--date D]
        record a hand-run decision or outcome (one row; never claim `applied` without an observed
        ATS confirmation).
  python ledger.py list [--days N] [--action A] [--company SUBSTR] [--limit N]
  python ledger.py outreach add --company C [--recipient R] [--role R] [--verdict V] [--channel X] [--notes N]
  python ledger.py outreach list [--company SUBSTR]
  python ledger.py resume-change add --resume ID --what TEXT --why TEXT
  python ledger.py resume-change list
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import state as S  # noqa: E402

ACTIONS = ("applied", "unapplied", "skipped", "escalated")


def same_company(a, b):
    """'acme' == 'acme inc', 'acme' == 'acme technology'."""
    if not a or not b:
        return False
    if a == b:
        return True
    ta, tb = a.split(), b.split()
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return long_[:len(short)] == short


def line(*parts):
    return " | ".join(str(p) for p in parts if p not in (None, ""))


def cmd_check(a):
    conn = S.db()
    url_key = S.norm_url(a.url) if a.url else ""
    ckey, tkey = S.norm_text(a.company), S.norm_text(a.title)
    hits, employer = [], []

    def classify(source, row_company, row_title, row_urls, desc):
        by_url = bool(url_key) and url_key in row_urls
        by_pair = same_company(ckey, row_company) and row_title == tkey
        if by_url or by_pair:
            hits.append("[%s] %s%s" % (source, desc, "  (same URL)" if by_url else ""))
        elif same_company(ckey, row_company):
            employer.append("[%s] %s" % (source, desc))

    for r in conn.execute("SELECT * FROM history ORDER BY date"):
        classify("history", r["company_key"], r["title_key"], {r["url_key"]},
                 line(r["date"], r["company"], r["title"], "action=%s" % r["action"],
                      "status=%s" % r["status"], r["notes"][:110] if r["notes"] else ""))
    for r in conn.execute("SELECT * FROM postings WHERE verdict IS NOT NULL OR app_status IS NOT NULL"):
        classify("pipeline posting", r["company_key"], r["title_key"],
                 {r["url_key"], S.norm_url(r["apply_url"])},
                 line((r["decided_at"] or "")[:10], r["company"], r["title"], "verdict=%s" % r["verdict"],
                      "app=%s" % r["app_status"] if r["app_status"] else "",
                      (r["reason"] or "")[:110]))
    for r in conn.execute("SELECT * FROM applications"):
        classify("pipeline application", r["company_key"], r["title_key"], {S.norm_url(r["final_url"])},
                 line((r["updated_at"] or "")[:10], r["company"], r["role"], "status=%s" % r["status"]))

    if hits:
        print("PLAUSIBLE DUPLICATE (%d):" % len(hits))
        for h in hits:
            print("  " + h)
    else:
        print("no matching record for %s / %s" % (a.company, a.title))
    if employer:
        print("same employer, other roles (%d):" % len(employer))
        for h in employer[:15]:
            print("  " + h)
        if len(employer) > 15:
            print("  ... %d more" % (len(employer) - 15))
    return 1 if hits else 0


def cmd_add(a):
    conn = S.db()
    rid = S.history_add(conn, a.company, a.title, a.url or "", resume=a.resume or "",
                        confidence=a.confidence or "", signals=a.signals or "", action=a.action,
                        status=a.status or a.action, notes=a.notes or "", date=a.date)
    print("history row %d added" % rid)
    return 0


def cmd_list(a):
    conn = S.db()
    where, params = [], []
    if a.days:
        where.append("date >= date('now', ?)")
        params.append("-%d day" % a.days)
    if a.action:
        where.append("action=?")
        params.append(a.action)
    if a.company:
        where.append("company LIKE ?")
        params.append("%" + a.company + "%")
    sql = "SELECT * FROM history %s ORDER BY date DESC, id DESC LIMIT ?" % (
        "WHERE " + " AND ".join(where) if where else "")
    for r in conn.execute(sql, params + [a.limit]):
        print(line(r["date"], r["company"], r["title"], r["action"], r["status"], r["url"]))
    return 0


def cmd_outreach(a):
    conn = S.db()
    if a.sub == "add":
        with S.tx(conn):
            conn.execute("INSERT INTO outreach (date, company, recipient, role, email_verdict, channel, notes) "
                         "VALUES (?,?,?,?,?,?,?)",
                         (a.date or S.TODAY, a.company, a.recipient, a.role, a.verdict, a.channel, a.notes))
        print("outreach row added")
        return 0
    where, params = ("WHERE company LIKE ?", ["%" + a.company + "%"]) if a.company else ("", [])
    for r in conn.execute("SELECT * FROM outreach %s ORDER BY date DESC, id DESC" % where, params):
        print(line(r["date"], r["company"], r["recipient"], r["role"], r["email_verdict"], r["channel"],
                   r["notes"]))
    return 0


def cmd_resume_change(a):
    conn = S.db()
    if a.sub == "add":
        with S.tx(conn):
            conn.execute("INSERT INTO resume_changes (date, resume_id, what, why) VALUES (?,?,?,?)",
                         (a.date or S.TODAY, a.resume, a.what, a.why))
        print("resume change added")
        return 0
    for r in conn.execute("SELECT * FROM resume_changes ORDER BY date DESC, id DESC"):
        print(line(r["date"], r["resume_id"], r["what"], r["why"]))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check")
    c.add_argument("--company", required=True)
    c.add_argument("--title", required=True)
    c.add_argument("--url")
    c.set_defaults(fn=cmd_check)

    c = sub.add_parser("add")
    c.add_argument("--company", required=True)
    c.add_argument("--title", required=True)
    c.add_argument("--url")
    c.add_argument("--action", required=True, choices=ACTIONS)
    c.add_argument("--status")
    c.add_argument("--resume")
    c.add_argument("--confidence")
    c.add_argument("--signals")
    c.add_argument("--notes")
    c.add_argument("--date")
    c.set_defaults(fn=cmd_add)

    c = sub.add_parser("list")
    c.add_argument("--days", type=int)
    c.add_argument("--action", choices=ACTIONS)
    c.add_argument("--company")
    c.add_argument("--limit", type=int, default=30)
    c.set_defaults(fn=cmd_list)

    c = sub.add_parser("outreach")
    c.add_argument("sub", choices=("add", "list"))
    c.add_argument("--company")
    c.add_argument("--recipient")
    c.add_argument("--role")
    c.add_argument("--verdict")
    c.add_argument("--channel")
    c.add_argument("--notes")
    c.add_argument("--date")
    c.set_defaults(fn=cmd_outreach)

    c = sub.add_parser("resume-change")
    c.add_argument("sub", choices=("add", "list"))
    c.add_argument("--resume")
    c.add_argument("--what")
    c.add_argument("--why")
    c.add_argument("--date")
    c.set_defaults(fn=cmd_resume_change)

    a = ap.parse_args()
    if a.cmd == "outreach" and a.sub == "add" and not a.company:
        ap.error("outreach add needs --company")
    if a.cmd == "resume-change" and a.sub == "add" and not (a.resume and a.what and a.why):
        ap.error("resume-change add needs --resume, --what and --why")
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
