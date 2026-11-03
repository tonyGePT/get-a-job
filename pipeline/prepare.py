#!/usr/bin/env python
"""Step 3: turn APPLY verdicts into tailored, keyword-verified resumes.

Input  : state/verdicts/<date>.json  (written by the qualification agent)
         state/jd/<key>.txt          (raw JD text, one per posting)
Output : <resume_prefix>_<key>.pdf + resume_<key>.txt in <harness>/resumes/
         state/apply-queue.json      (what the apply agent must submit, in order)

Routing follows resume_routing_context.md section 3 hard rules.
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import state as S  # noqa: E402

HARNESS = S.CONFIG["harness"]
sys.path.insert(0, HARNESS)


def decide_variant(title, jd_text):
    t = (title or "").lower()
    j = (jd_text or "").lower()
    if re.search(r"gtm|go-to-market|growth engineer|revenue (systems )?engineer|revops|"
                 r"revenue operations|sales operations|marketing operations|marketing automation|"
                 r"marketing technolog|crm manager|(hubspot|salesforce) admin|"
                 r"(growth|performance) marketing", t):
        return "GTM"
    if re.search(r"forward deployed|solutions architect|customer engineer|deployment engineer|"
                 r"implementation engineer|solutions engineer", t):
        return "FDE"
    if re.search(r"internal tools|business (systems|operations)|admin panel|bi analyst|"
                 r"reporting analyst|workflow automation", t):
        return "TOOLS"
    if re.search(r"(healthcare|clinical|phi|ehr)", j) and re.search(r"(document|extract|ocr)", j):
        return "AI-HEALTH"
    return "AI"


def main():
    day = S.TODAY
    S.merge_verdict_parts(day)
    verdicts = S.read_json(S.path("verdicts", "%s.json" % day), None)
    if verdicts is None:
        raise SystemExit("no verdicts file for %s" % day)
    items = verdicts if isinstance(verdicts, list) else verdicts.get("items", [])

    # Record every verdict on its posting before anything else can fail: this is
    # what discover.py dedupes on tomorrow, so a crash in resume rendering must
    # not lose today's decisions.
    stored = S.ingest_verdicts(day)
    print("recorded %d verdict(s) in state.db" % stored)

    import resume_tailor as rt  # harness module, imported after sys.path setup
    rt.PDF_PREFIX = S.CONFIG.get("resume_prefix", rt.PDF_PREFIX)

    queue, skipped = [], []
    for v in items:
        if v.get("verdict") != "APPLY":
            continue
        key = v["key"]
        jd_path = v.get("jd_file") or os.path.join(S.STATE, "jd", "%s.txt" % key)
        if not os.path.exists(jd_path):
            skipped.append((key, "missing JD text at %s" % jd_path))
            continue
        text = open(jd_path, encoding="utf-8").read()
        if not rt.is_valid_jd(text):
            skipped.append((key, "JD body too short to tailor"))
            continue
        entry = {"status": 200, "url": v.get("apply_url") or v.get("url"),
                 "title": v.get("title"), "text": text}
        variant = decide_variant(v.get("title"), text)
        rt.ROUTING[key] = variant
        res = rt.tailor(key, entry)
        missing = rt.verify(entry, key)
        if missing:
            skipped.append((key, "coverage gap: %s" % ", ".join(missing[:5])))
            continue
        rt.render_pdf(res["txt"], rt.pdf_path(key))
        queue.append({
            "key": key, "company": v.get("company"), "title": v.get("title"),
            "variant": variant, "apply_url": v.get("apply_url") or v.get("url"),
            "ats": v.get("ats"), "resume_pdf": rt.pdf_path(key),
            "url_key": S.norm_url(v.get("url")),
            "coverage": "%d/%d" % (res["required"], res["required"]),
        })

    lim = S.CONFIG["limits"]["max_applications_per_day"]
    if len(queue) > lim:
        skipped.extend((q["key"], "over daily application cap %d" % lim) for q in queue[lim:])
        queue = queue[:lim]

    S.write_json(S.path("apply-queue.json"), {"generated": S.now(), "queue": queue,
                                              "skipped": skipped})
    print("queued %d applications, %d skipped" % (len(queue), len(skipped)))
    for q in queue:
        print("  %-12s %-8s %s | %s" % (q["key"], q["variant"], q["company"], q["title"]))
    for k, why in skipped:
        print("  SKIP %-12s %s" % (k, why))


if __name__ == "__main__":
    main()
