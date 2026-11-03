#!/usr/bin/env python
"""Step 5: confirm submissions independently of what the apply agent claimed.

A submission counts only when the employer (or its ATS) actually emailed a
receipt, or the evidence file carries a confirmation URL/text that the agent
observed. Anything else is downgraded so it is never traced or logged as applied.

Two invariants, both learned the hard way:
  * receipts are append-only, keyed by Gmail message id. Re-querying a narrower
    window can never delete a receipt that was already found.
  * a submission confirmed once is never downgraded later; only unconfirmed
    submissions are re-checked.

Cost: one Gmail list query per run for all companies, then one `messages get`
per candidate message (it used to be one list plus up to 20 gets per company
per day, for every application ever).

Usage: python verify.py [--days 2]
"""

import argparse
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import state as S  # noqa: E402

G = S.CONFIG["gws"]

OWN = S.CONFIG["notify"]["email"].lower()
ATS_DOMAINS = (
    "ashbyhq.com", "greenhouse-mail.io", "greenhouse.io", "teamtailor.com", "lever.co",
    "workable.com", "gem.com", "myworkdayjobs.com", "workday.com", "gusto.com",
    "applytojob.com", "jazzhr.com", "smartrecruiters.com", "rippling.com", "icims.com",
    "jobvite.com", "breezy.hr", "recruitee.com", "dover.com", "wellfound.com",
    "paylocity.com", "ultipro.com", "bamboohr.com", "comeet.com", "zohorecruit.com",
)
RECEIPT = re.compile(
    r"(thank you for (your )?appl|application (was )?(received|submitted)|"
    r"we (have )?received your application|received your application|"
    r"your application (was|has been)|application received)", re.I)

CHECKED = ("submitted", "unverified")


def sender_ok(frm, company):
    """A receipt comes from the ATS or from the employer's own domain — never
    from the user's own address (mail they send themselves) and never from a newsletter."""
    low = (frm or "").lower()
    if OWN in low:
        return False
    m = re.search(r"[a-z0-9._%+-]+@([a-z0-9.-]+\.[a-z]{2,})", low)
    if not m:
        return False
    dom = m.group(1)
    if any(dom.endswith(a) for a in ATS_DOMAINS):
        return True
    token = re.sub(r"[^a-z0-9]", "", (company or "").split()[0].lower())
    return bool(token) and token in re.sub(r"[^a-z0-9]", "", dom)


def gws(args):
    p = subprocess.run([G["node"], G["cli"]] + args, capture_output=True, text=True, timeout=180)
    out = p.stdout
    i = out.find("{")
    if i < 0:
        return {}
    obj, _ = json.JSONDecoder().raw_decode(out[i:])
    return obj


def list_candidates(days):
    """One list query per run: every recent mail that could be an application receipt."""
    q = 'newer_than:%dd (subject:("thank you for" OR "application" OR "received your")) -from:me' % days
    res = gws(["gmail", "users", "messages", "list",
               "--params", json.dumps({"userId": "me", "q": q, "maxResults": 50})])
    return [m["id"] for m in res.get("messages", [])]


def fetch(mid):
    msg = gws(["gmail", "users", "messages", "get",
               "--params", json.dumps({"userId": "me", "id": mid, "format": "full"})])
    payload = msg.get("payload") or {}
    hdrs = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    return {"id": mid, "from": hdrs.get("from", ""), "subject": hdrs.get("subject", ""),
            "date": hdrs.get("date", ""), "snippet": msg.get("snippet", "")}


def matches(msg, company):
    if not sender_ok(msg["from"], company):
        return False
    body = (msg["subject"] + " " + json.dumps(msg["snippet"])).lower()
    if not (RECEIPT.search(msg["subject"]) or RECEIPT.search(body)):
        return False
    needle = (company or "").split()[0].lower()
    return not needle or needle in body or needle in msg["from"].lower()


def write_evidence(key, receipts, status):
    """Mirror the DB outcome back into the evidence JSON, append-only."""
    p = S.path("evidence", "%s.json" % key)
    ev = S.read_json(p, None)
    if ev is None:
        return
    known = {r.get("id") for r in ev.get("receipts") or []}
    merged = list(ev.get("receipts") or [])
    for r in receipts:
        if r["id"] not in known:
            merged.append({k: r[k] for k in ("id", "from", "subject", "date")})
    ev["receipts"] = merged
    ev["receipt_confirmed"] = bool(merged)
    if not (ev.get("status") == "submitted" and bool(ev.get("confirmation_text") and ev.get("final_url"))):
        ev["status"] = status
    st = os.stat(p)
    S.write_json(p, ev)
    os.utime(p, (st.st_atime, st.st_mtime))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=2)
    args = ap.parse_args()

    S.ensure_dirs()
    conn = S.db()
    try:
        S.sync_evidence(conn)
        todo = [r for r in conn.execute(
                    "SELECT * FROM applications WHERE confirmed=0 AND status IN (%s)"
                    % ",".join("?" for _ in CHECKED), CHECKED).fetchall()]
        msgs = []
        if todo:
            ids = list_candidates(args.days)
            msgs = [fetch(mid) for mid in dict.fromkeys(ids)]

        for app in todo:
            hits = [m for m in msgs if matches(m, app["company"])]
            for m in hits:
                S.add_receipt(conn, app["key"], dict(m, company=app["company"]))
            has_proof = bool(hits) or bool(app["confirmation_text"] and app["final_url"])
            status = app["status"]
            if hits:
                status = "submitted"
            elif not has_proof:
                status = "unverified"
            with S.tx(conn):
                conn.execute(
                    "UPDATE applications SET status=?, confirmed=?, updated_at=? WHERE key=?",
                    (status, 1 if has_proof else 0, S.now(), app["key"]))
                if app["url_key"]:
                    proof = (hits[0]["subject"] if hits
                             else (app["final_url"] if app["confirmation_text"] and app["final_url"]
                                   else None))
                    S.posting_application(conn, app["url_key"], status, proof=proof)
            write_evidence(app["key"], hits, status)

        rows = conn.execute("SELECT * FROM applications ORDER BY COALESCE(applied_at, updated_at)").fetchall()
        results = []
        for r in rows:
            rs = S.application_receipts(conn, r["key"])
            results.append({"key": r["key"], "company": r["company"], "status": r["status"],
                            "confirmed": bool(r["confirmed"]),
                            "receipts": [{"id": x["msg_id"], "from": x["from_addr"],
                                          "subject": x["subject"], "date": x["msg_date"]}
                                         for x in rs]})
    finally:
        conn.close()

    S.write_json(S.path("verification-%s.json" % S.TODAY),
                 {"generated": S.now(), "items": results})
    submitted = [r for r in results if r["status"] == "submitted"]
    print("%d submitted (%d with a mail receipt), %d unconfirmed, %d Gmail message(s) read"
          % (len(submitted), sum(1 for r in submitted if r["receipts"]),
             sum(1 for r in results if r["status"] == "unverified"), len(msgs)))
    for r in results:
        mark = "MAIL" if r["receipts"] else ("app " if r["status"] == "submitted" else "----")
        print("  %-4s %-14s %-12s %s" % (mark, r["key"], r["status"],
                                         (r["receipts"][0]["subject"][:48] if r["receipts"] else "")))


if __name__ == "__main__":
    main()
