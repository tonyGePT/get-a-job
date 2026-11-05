#!/usr/bin/env python
"""Step 1: ask BlitzAPI (through treg) for postings surfaced since the last run,
drop everything already decided, and leave a candidate list for qualification.

Dedupe has two sources, both required:
  * state/state.db — postings that already carry a qualification verdict or an
    application outcome. This is what stops a posting being qualified and applied
    to twice; the ledger used to be written but never read for this.
  * state.db `history` — the ledger that replaced application_log.md (imported 2026-11-02), including
    anything applied to by hand outside the pipeline (added with ledger.py).
A posting that was surfaced but never got a verdict (qualify crashed) stays
eligible: no verdict, no drop.

Cost: ~$0.0005 per returned record. Empty result = free.

Usage:
  python discover.py                 # normal run, 1 day window
  python discover.py --days 3        # wider window
  python discover.py --dry-run       # print the payload, call nothing
  python discover.py --results F     # filter a saved treg body instead of calling treg
                                      #   (F: the treg JSON, {"results": [...]}, or a list;
                                      #    candidates-<date>.json shapes are accepted too)
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import state as S  # noqa: E402

# Titles that are keyword collisions, not roles to apply to.
TITLE_BLOCK = re.compile(
    r"\b(ui|ux|designer|design|recruiter|talent|sourcer|account executive|sales development|"
    r"sdr|bdr|intern|interns|internship|apprentice|director|head of|vp|svp|evp|vice president|chief|"
    r"officer|account manager|territory manager|project manager|program manager|"
    r"data entry|qa|test engineer|support engineer|helpdesk|technical writer)\b",
    re.I)

# "Manager" is blocked (engineering/account/program managers are not targets) unless the title
# is one of the ops/automation families the user asked for on 2026-11-02.
MANAGER_BLOCK = re.compile(r"\bmanager\b", re.I)
MANAGER_ALLOW = re.compile(
    r"(ai (enablement|adoption)|automation|digital transformation|process improvement|"
    r"operational excellence|innovation|revenue operations|sales operations|marketing operations|"
    r"crm|salesforce|hubspot|marketing technolog|business (systems|operations)|legal operations|"
    r"growth marketing|performance marketing|production operations|post-production)",
    re.I)


_OVERRIDES = None


def overrides():
    """Optional extra filters from state/discover-overrides.json.

    Written only by retro.py, after a filter category has been skipped by the LLM often enough
    to be worth a deterministic rule. Kept in state/ (not in code) so a bad rule is removable
    without a deploy; every pattern is compiled defensively, and a broken one is ignored
    rather than allowed to stop discovery.
    """
    global _OVERRIDES
    if _OVERRIDES is None:
        doc = S.read_json(S.path("discover-overrides.json"), {}) or {}
        def compile_all(items):
            pats = []
            for p in items or []:
                try:
                    pats.append(re.compile(p, re.I))
                except (re.error, TypeError):
                    print("discover-overrides: ignoring bad pattern %r" % (p,))
            return pats
        _OVERRIDES = {"title": compile_all(doc.get("title_block")),
                      "agency": compile_all(doc.get("agency_block"))}
    return _OVERRIDES


def title_blocked(title):
    if TITLE_BLOCK.search(title) or (MANAGER_BLOCK.search(title) and not MANAGER_ALLOW.search(title)):
        return True
    return any(p.search(title) for p in overrides()["title"])


# BlitzAPI's is_agency flag misses most staffing reposts, so also match the name.
# Word-bounded so "aditi" does not catch "Traditional", "talent" not "Talentful".
AGENCY_BLOCK = re.compile(
    r"\b(staffing|recruiting|recruiters?|recruitment|talent|placements?|resources?|resourcing|"
    r"outsourcing|consultancy|"
    r"manpower|jobgether|remotehunter|hiretalent|talenthop|jobot|insight global|"
    r"robert half|teksystems|kforce|cybercoders|collabera|intelliswift|net2source|"
    r"compunnel|damco|lancesoft|apex systems|modis|beacon hill|judge group|phaxis|"
    r"atyeti|aditi|motion recruitment|smith arnold|infosys|wipro|cognizant|"
    r"capgemini|accenture|hcl|tcs|virtusa|mindtree|birlasoft|syntel|mastech|"
    r"international services|it services|systems inc|solutions inc|technologies inc)\b",
    re.I)


def blocked_company(name):
    if AGENCY_BLOCK.search(name or ""):
        return True
    extra = S.read_json(S.path("blocklist.json"), {}) or {}
    if S.norm_text(name) in set(S.norm_text(x) for x in extra.get("companies", [])):
        return True
    return any(p.search(name or "") for p in overrides()["agency"])


def call_treg(payload):
    """POST through treg's HTTP proxy (same request the `treg` CLI makes; no CLI
    needed on Windows). Credentials: ~/.treg/config.json, as written by `treg login`."""
    cfg = json.load(open(os.path.expanduser("~/.treg/config.json"), encoding="utf-8"))
    req = urllib.request.Request(
        cfg["base_url"].rstrip("/") + "/call/blitzapi.jobs.search",
        data=json.dumps(payload).encode(), method="POST",
        headers={"X-Treg-Token": cfg["token"], "x-treg-org": cfg["active_org"],
                 "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            body = json.load(r)
            cost = r.headers.get("x-treg-cost-micro")
    except urllib.error.HTTPError as e:
        raise SystemExit("treg HTTP %d: %s" % (e.code, e.read()[:300]))
    if body.get("error"):
        raise SystemExit("treg error: %s" % body.get("error"))
    charge = "charged $%.6f" % (int(cost) / 1e6) if cost else ""
    return body, charge


def build_payloads(q, days):
    """One BlitzAPI request body per configured search (remote, local hybrid/on-site, ...)."""
    payloads = []
    for s in q["searches"]:
        location = {"country_code": {"include": q["country_code"]}}
        if s.get("cities"):
            location["city"] = {"include": s["cities"]}
        company = {"is_agency": q["is_agency"]}
        if q.get("exclude_hq_cities"):
            company["hq"] = {"city": {"exclude": q["exclude_hq_cities"]}}
        payloads.append((s["name"], {
            "job": {
                "title": {"include": q["titles"]},
                "location": location,
                "work_arrangement": {"include": s["work_arrangement"]},
                "date_posted": {"last_days": days},
            },
            "company": company,
            "max_results": q["page_size"],
        }))
    return payloads


def fetch_all(name, payload, max_pages):
    """Follow the cursor until the end or max_pages; returns (results, total, charges)."""
    results, charges, total, cursor = [], [], None, None
    for _ in range(max_pages):
        body, charge = call_treg(dict(payload, cursor=cursor) if cursor else payload)
        charges.append(charge)
        total = body.get("total_results", total)
        for r in body.get("results") or []:
            r["_search"] = name
            results.append(r)
        cursor = body.get("cursor")
        if not cursor:
            break
    return results, total, charges


def load_results_file(p):
    """A saved treg response, or a candidates-<date>.json (already filtered)."""
    doc = S.read_json(p)
    if isinstance(doc, list):
        return {"results": doc}
    if "results" in doc:
        return doc
    if "candidates" in doc:
        return {"total_results": doc.get("total_results"),
                "results": [dict(c, company_name=c.get("company"),
                                 date_posted=c.get("posted"),
                                 company_linkedin_url=c.get("company_linkedin"))
                            for c in doc["candidates"]]}
    raise SystemExit("cannot read results from %s" % p)


def filter_results(results, decided, seen, max_candidates):
    """Split raw postings into candidates + a per-posting drop list.

    `decided` is the pipeline's own record (postings/applications), `seen` the history ledger.
    Every exclusion carries a reason, so nothing disappears silently.
    """
    candidates = []
    detail = []
    dropped = {"already_seen": 0, "already_decided": 0, "title_blocked": 0,
               "duplicate_in_batch": 0, "agency": 0, "board": 0}

    def drop(reason, bucket, company, title):
        dropped[bucket] = dropped.get(bucket, 0) + 1
        detail.append({"company": company, "title": title, "reason": reason})

    # Same role in several cities collapses to one row first, so a single opening
    # posted in three cities is not mistaken for a job board below.
    unique = []
    batch_keys = set()
    for r in results:
        company = r.get("company_name") or r.get("company") or ""
        title = r.get("title") or ""
        pair = (S.norm_text(company), S.norm_text(title))
        if pair in batch_keys:
            drop("same role, another city or search", "duplicate_in_batch", company, title)
            continue
        batch_keys.add(pair)
        unique.append(r)

    # A company posting 3+ DIFFERENT roles in one window is a board, not an employer.
    per_company = {}
    for r in unique:
        c = S.norm_text(r.get("company_name") or r.get("company") or "")
        per_company[c] = per_company.get(c, 0) + 1
    boards = {c for c, n in per_company.items() if n >= 3 and c}

    for r in unique:
        url = r.get("url") or ""
        company = r.get("company_name") or r.get("company") or ""
        title = r.get("title") or ""
        key = S.norm_url(url)
        pair = (S.norm_text(company), S.norm_text(title))
        if key in decided["urls"] or pair in decided["pairs"]:
            drop("already decided (verdict or application recorded)", "already_decided",
                 company, title)
            continue
        if key in seen["urls"] or pair in seen["pairs"]:
            drop("already in history ledger", "already_seen", company, title)
            continue
        if title_blocked(title):
            drop("title is not an engineering role", "title_blocked", company, title)
            continue
        if blocked_company(company):
            drop("staffing/board blocklist", "agency", company, title)
            continue
        if S.norm_text(company) in boards:
            drop("company posted 3+ roles in this window (job board)", "board", company, title)
            continue
        candidates.append({
            "url": url,
            "url_key": key,
            "company": company,
            "title": title,
            "posted": r.get("date_posted") or r.get("posted"),
            "location": r.get("location") or {},
            "company_linkedin": r.get("company_linkedin_url") or r.get("company_linkedin"),
            "ai_summary": r.get("ai_summary") or "",
        })

    candidates.sort(key=lambda c: c.get("posted") or "", reverse=True)
    return candidates[:max_candidates], dropped, detail[:300]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--results", help="filter this saved response instead of calling treg")
    args = ap.parse_args()

    S.ensure_dirs()
    q = S.CONFIG["query"]
    lim = S.CONFIG["limits"]
    days = args.days or q["date_posted_days"]
    payloads = build_payloads(q, days)
    if args.dry_run:
        print(json.dumps([{"search": n, "payload": p} for n, p in payloads], indent=1))
        return

    S.heartbeat("discover", {"window_days": days})
    totals, charges = {}, []
    if args.results:
        charges, body = ["(saved results) %s" % args.results], load_results_file(args.results)
        results = body.get("results") or []
        totals["saved"] = body.get("total_results")
    else:
        results = []
        for name, payload in payloads:
            got, total, ch = fetch_all(name, payload, q["max_pages_per_search"])
            results.extend(got)
            totals[name] = "%d/%s" % (len(got), total)
            charges.extend(ch)

    conn = S.db()
    try:
        decided = S.decided_keys(conn)
        candidates, dropped, detail = filter_results(
            results, decided, S.seen_index(), lim["max_candidates"])
        with S.tx(conn):
            for c in candidates:
                S.posting_upsert(conn, c)
    finally:
        conn.close()

    out = {"generated": S.now(), "window_days": days,
           "total_results": totals, "returned": len(results), "dropped": dropped,
           "candidates": candidates}
    S.write_json(S.path("dropped-%s.json" % S.TODAY),
                 {"generated": S.now(), "dropped": dropped, "detail": detail})
    S.write_json(S.path("candidates-%s.json" % S.TODAY), out)
    S.write_json(S.path("candidates-latest.json"), out)

    for c in charges:
        if c:
            print(c)
    print("searches(returned/total)=%s returned=%d new=%d dropped=%s" %
          (totals, len(results), len(candidates), dropped))


if __name__ == "__main__":
    main()
