#!/usr/bin/env python3
"""JD keyword-coverage engine for the job harness.

Answers one question per target job: for every technology/keyword the posting
names, what are we allowed to put on the resume, and what is still missing?

Tiers come from keyword_facts.json. The engine never invents a claim:
a Tier A entry with basis=null is BLOCKED and counts as neither covered
nor claimable until a basis line is recorded in the facts file.

Usage:
  python keyword_coverage.py --jd-json FILE --key NAME --variant FILE
  python keyword_coverage.py --jd FILE.txt --variant FILE
  python keyword_coverage.py --scan-json FILE [FILE...]

Output is plain ASCII (Windows console safe).
"""

import argparse
import json
import os
import re
import sys
from collections import OrderedDict

ROOT = os.path.dirname(os.path.abspath(__file__))
FACTS_PATH = os.path.join(ROOT, "keyword_facts.json")

REQ_HEADINGS = (
    r"requirements?|required|basic qualifications?|minimum qualifications?|"
    r"what we'?re looking for|what we are looking for|what you'?ll need|"
    r"what you will bring|who you are|you have|you'?ll need|you should have|"
    r"must have|must-haves|qualifications?|about you|your background|"
    r"required skills|minimum requirements|ideal candidate|skills"
)
BONUS_HEADINGS = (
    r"bonus(?: points)?|nice[- ]to[- ]have[s]?|preferred qualifications?|"
    r"good to have|plus(?: points)?|preferred|additional|nice to haves"
)

# Unmistakably application-form furniture; everything from here on is not JD body.
FORM_CUT = re.compile(
    r"(?i)(indicates a required field|submit application|autofill my application|"
    r"u\.s\. standard demographic questions|voluntary self-identification|"
    r"create a job alert)"
)

# Contexts where a capitalised name is a customer/investor, not a technology.
NAME_CONTEXT = re.compile(
    r"(?i)(trusted by|backed by|customers? (?:like|include|such as)|investors|"
    r"powered by|our customers|used by|employers like|partners? like)\s*$"
)

MARKERS = (
    r"experience (?:with|in|using|building|developing|working with)|"
    r"familiar(?:ity)? with|proficien(?:cy|t) in|knowledge of|expertise in|"
    r"comfortable (?:with|working with)|working knowledge of|"
    r"hands-on (?:experience )?with|fluency (?:with|in)|built with|"
    r"powered by|such as|including|e\.g\.,?|i\.e\.,?"
)

STOPWORDS = {
    "experience", "years", "year", "plus", "strong", "excellent", "great",
    "good", "ability", "skills", "skill", "team", "teams", "work", "working",
    "role", "job", "company", "candidate", "candidates", "you", "your", "our",
    "we", "the", "and", "or", "with", "for", "that", "this", "these", "those",
    "their", "them", "it", "its", "a", "an", "of", "in", "on", "to", "at",
    "as", "by", "is", "are", "be", "have", "has", "will", "can", "who",
    "which", "what", "when", "where", "how", "why", "all", "any", "both",
    "other", "others", "such", "well", "new", "own", "end", "end-to-end",
    "background", "understanding", "knowledge", "familiarity", "proficiency",
    "expertise", "comfortable", "proven", "demonstrated", "track", "record",
    "degree", "bachelor", "bachelors", "master", "masters", "computer",
    "science", "engineering", "related", "field", "equivalent", "environments",
    "environment", "systems", "system", "tools", "tooling", "platforms",
    "platform", "applications", "application", "products", "product",
    "solutions", "solution", "services", "service", "data", "customer",
    "customers", "business", "teams", "owners", "collaboration", "communication",
    "written", "verbal", "time", "management", "project", "projects",
    "development", "design", "architecture", "fast", "paced", "startup",
    "ownership", "bias", "action", "growth", "revenue", "sales", "marketing",
    "operations", "strategy", "impact", "high", "low", "large", "scale",
    "enterprise", "modern", "deep", "real", "production", "production-grade",
    "quality", "best", "practices", "processes", "process", "workflows",
    "workflow", "internal", "external", "cross", "functional", "wide",
    "responsibilities", "you'll", "we're", "we'll", "you're", "it's", "don't",
    "position", "role's", "opportunity", "mission", "vision", "culture",
    "benefits", "compensation", "salary", "equity", "remote", "hybrid",
    "office", "onsite", "on-site", "travel", "location", "based", "preferred",
    "preference", "senior", "staff", "principal", "junior", "lead", "manager",
}

# Generic tech nouns: not themselves a claimable keyword, but they mark a
# sentence as being about technology, so unknown tokens beside them get flagged.
FUNCTION_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "being", "by", "can",
    "could", "did", "do", "does", "for", "from", "had", "has", "have", "if",
    "in", "including", "into", "is", "it", "its", "like", "may", "might",
    "must", "no", "not", "of", "on", "or", "our", "should", "such", "than",
    "that", "the", "their", "them", "then", "there", "these", "they", "this",
    "those", "to", "was", "we", "were", "when", "where", "will", "with",
    "would", "you", "your",
}

TECH_MARKERS = {
    "api", "apis", "sdk", "sdks", "framework", "frameworks", "library",
    "libraries", "database", "databases", "cloud", "stack", "stacks",
    "protocol", "protocols", "pipeline", "pipelines", "infrastructure",
    "model", "models", "language", "languages", "backend", "frontend",
    "full-stack", "fullstack", "typescript", "python", "java", "golang",
    "rust", "ruby", "php", "swift", "kotlin", "scala",
}


def load_facts(path=FACTS_PATH):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def build_index(entries):
    """[(compiled_regex, canonical, entry), ...] longest alias first."""
    pairs = []
    for canonical, entry in entries.items():
        for alias in entry["aliases"]:
            pairs.append((alias, canonical, entry))
    pairs.sort(key=lambda p: -len(p[0]))
    index = []
    for alias, canonical, entry in pairs:
        rx = re.compile(r"(?<![A-Za-z0-9])" + re.escape(alias) + r"(?![A-Za-z0-9])", re.I)
        index.append((rx, canonical, entry))
    return index


def match_all(text, index):
    """canonical -> sorted list of every occurrence offset."""
    hits = OrderedDict()
    for rx, canonical, entry in index:
        found = [m.start() for m in rx.finditer(text)]
        if not found:
            continue
        hits.setdefault(canonical, [])
        hits[canonical].extend(found)
    for canonical in hits:
        hits[canonical] = sorted(set(hits[canonical]))
    return hits


_SMART = {
    "\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "-", "\u00a0": " ", "\u2026": "...",
}


def normalize_text(text):
    for bad, good in _SMART.items():
        text = text.replace(bad, good)
    return text.replace("&nbsp;", " ").replace("&amp;", "&")


def heading_map(text):
    """sorted [(offset, 'req'|'bonus')] of section headings."""
    marks = []
    for rx, kind in (
        (re.compile(REQ_HEADINGS, re.I), "req"),
        (re.compile(BONUS_HEADINGS, re.I), "bonus"),
    ):
        for m in rx.finditer(text):
            line_start = text.rfind("\n", 0, m.start()) + 1
            line_end = text.find("\n", m.start())
            line = text[line_start: line_end if line_end > 0 else len(text)]
            # headings are short lines, not mid-sentence prose
            if len(line) <= 80 and m.start() - line_start < 12:
                marks.append((line_start, kind))
    marks.sort()
    return marks


def classify_section(offset, marks):
    kind = None
    for start, k in marks:
        if start <= offset:
            kind = k
        else:
            break
    return kind or "other"


def best_section(offsets, marks):
    """A term counts as a requirement if it is named there at least once."""
    kinds = {classify_section(o, marks) for o in offsets}
    for kind in ("req", "bonus", "other"):
        if kind in kinds:
            return kind
    return "other"


def split_form(text):
    """(jd_body, application_form_tail). Screening questions live in the tail."""
    m = FORM_CUT.search(text)
    if not m:
        return text, ""
    return text[: m.start()], text[m.start():]


def candidate_terms(text, index):
    """Heuristic unknown-technology terms named in the JD."""
    matched_spans = []
    for rx, _, _ in index:
        for m in rx.finditer(text):
            matched_spans.append((m.start(), m.end()))

    def covered(a, b):
        return any(s <= a and b <= e for s, e in matched_spans)

    rx = re.compile(r"(?i)\b(?:" + MARKERS + r")\s+([^.;:)\n]{2,120})")
    out = OrderedDict()
    for m in rx.finditer(text):
        chunk = m.group(1)
        for part in re.split(r",|/|\band\b|\bor\b|\bwith\b", chunk):
            term = part.strip(" \t-*").strip()
            if not term or len(term) > 40 or len(term.split()) > 3:
                continue
            low = term.lower()
            if low in STOPWORDS or low in TECH_MARKERS:
                continue
            if not re.match(r"^[A-Za-z0-9][A-Za-z0-9 .+&-]*$", term):
                continue
            if any(w.lower() in FUNCTION_WORDS for w in term.split()):
                continue
            if len(term) < 3:
                continue
            start = text.find(term, m.start())
            if start < 0:
                start = m.start()
            if covered(start, start + len(term)):
                continue
            if NAME_CONTEXT.search(text[max(0, start - 60): start]):
                continue
            out.setdefault(term, (start, 1))
            out[term] = (out[term][0], out[term][1] + 1)
    return out


def variant_covers(variant_text, entry):
    for alias in entry["aliases"]:
        rx = re.compile(r"(?<![A-Za-z0-9])" + re.escape(alias) + r"(?![A-Za-z0-9])", re.I)
        if rx.search(variant_text):
            return alias
    return None


def status_of(entry):
    tier = entry["tier"]
    if tier == "A":
        return "claimable" if entry.get("basis") else "BLOCKED"
    if tier == "B":
        return "exposure" if entry.get("basis") else "BLOCKED"
    return "never"


def report(jd_text, label, variant_text, variant_name, facts):
    entries = facts["entries"]
    index = build_index(entries)
    jd_text = normalize_text(jd_text)
    jd_text, form_tail = split_form(jd_text)
    marks = heading_map(jd_text)
    jd_hits = match_all(jd_text, index)

    rows = []
    for canonical, offsets in sorted(jd_hits.items(), key=lambda kv: kv[1][0]):
        entry = entries[canonical]
        rows.append(
            {
                "canonical": canonical,
                "section": best_section(offsets, marks),
                "status": status_of(entry),
                "tier": entry["tier"],
                "claim": entry["aliases"][0],
                "covered": variant_covers(variant_text, entry) if variant_text else None,
            }
        )

    def count(pred):
        return sum(1 for r in rows if pred(r))

    active = [r for r in rows if r["status"] in ("claimable", "exposure")]
    req_active = [r for r in active if r["section"] == "req"]
    blocked = [r for r in rows if r["status"] == "BLOCKED"]
    never_req = [r for r in rows if r["status"] == "never" and r["section"] == "req"]
    unknown = candidate_terms(jd_text, index)

    line = "=" * 78
    print(line)
    print("JD KEYWORD COVERAGE -- %s" % label)
    if variant_name:
        print("resume: %s" % variant_name)
    print(line)

    print("\n[1] TIER SUMMARY (keywords this posting names)")
    print("  claimable now (tier A, basis recorded) : %d" % count(lambda r: r["status"] == "claimable"))
    print("  exposure only (tier B, basis recorded) : %d" % count(lambda r: r["status"] == "exposure"))
    print("  BLOCKED (basis missing)                : %d" % len(blocked))
    print("  NEVER CLAIM (tier C)                   : %d" % count(lambda r: r["status"] == "never"))
    print("  unmapped terms (need adjudication)     : %d" % len(unknown))

    if variant_text:
        cov = sum(1 for r in active if r["covered"])
        tot = len(active) if active else 0
        print("\n[2] COVERAGE OF %s" % variant_name)
        print("  claimable/exposure keywords in JD : %d" % tot)
        print("  present on this resume            : %d" % cov)
        if tot:
            print("  coverage                          : %.0f%%" % (100.0 * cov / tot))
        print("  requirements-section coverage     : %d/%d"
              % (sum(1 for r in req_active if r["covered"]), len(req_active)))

    if blocked:
        print("\n[3] BLOCKED -- need one basis line each in keyword_facts.json")
        for r in blocked:
            print("  - %-24s tier %s  section=%s" % (r["canonical"], r["tier"], r["section"]))
        print("  A blanket confirmation is not a basis. Write what was actually done")
        print("  (NDA-safe: tool + kind of work, no specifics).")

    exposure = [r for r in rows if r["status"] == "exposure" and r["section"] == "req"]
    if exposure:
        print("\n[4] EXPOSURE -- 'Also familiar with:' line only, never a bullet")
        for r in exposure:
            print("  - %s" % r["canonical"])

    if never_req:
        print("\n[5] NEVER CLAIM + NAMED AS A REQUIREMENT -> SKIP OR ESCALATE")
        for r in never_req:
            print("  - %s" % r["canonical"])
    elif count(lambda r: r["status"] == "never"):
        print("\n[5] tier C keywords present but not in the requirements section")

    if unknown:
        kept = []
        suppressed = 0
        for term, (offset, n) in sorted(unknown.items(), key=lambda kv: -kv[1][1]):
            if classify_section(offset, marks) in ("req", "bonus"):
                kept.append((term, offset, n))
            else:
                suppressed += 1
        if kept:
            print("\n[6] UNMAPPED JD TERMS -- do not claim; adjudicate into keyword_facts.json")
            for term, offset, n in kept[:25]:
                print("  - %-38s section=%s  x%d" % (term, classify_section(offset, marks), n))
            if suppressed:
                print("  (%d further terms outside requirement sections suppressed)" % suppressed)
        elif suppressed:
            print("\n[6] No unmapped terms in requirement sections (%d elsewhere)." % suppressed)

    if variant_text:
        missing = [r for r in req_active if not r["covered"]]
        if missing:
            print("\n[7] ACTIONABLE TAILORING GAPS (requirements section, claimable, not on resume)")
            for r in missing:
                print("  - %-24s claim as: %s" % (r["canonical"], r["claim"]))
        else:
            print("\n[7] No claimable requirements-section keyword is missing from this resume.")

    if form_tail:
        qhits = match_all(form_tail, index)
        if qhits:
            print("\n[8] FORM SCREENING QUESTIONS NAME THESE -- required answers, not resume text")
            for canonical in sorted(qhits):
                st = status_of(entries[canonical])
                print("  - %-24s tier %s  status=%s" % (canonical, entries[canonical]["tier"], st))
            print("  A BLOCKED entry here cannot be answered 'yes'. Resolve the basis or")
            print("  answer truthfully in the negative; do not invert the answer to pass a filter.")
    print("")


def load_jd(args, facts):
    if args.jd_json:
        with open(args.jd_json, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        value = data[args.key]
        text = value.get("text", "") if isinstance(value, dict) else value
        return text, "%s / %s" % (os.path.basename(args.jd_json), args.key)
    with open(args.jd, "r", encoding="utf-8") as fh:
        return fh.read(), os.path.basename(args.jd)


def scan(paths, facts, variant_dir=None):
    entries = facts["entries"]
    index = build_index(entries)
    print("=" * 100)
    print("PORTFOLIO SCAN -- which target JDs are blocked, off-limits, or uncovered")
    print("=" * 100)
    for path in paths:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        for key, value in data.items():
            text = value.get("text", "") if isinstance(value, dict) else value
            if not text or len(text) < 800:
                continue
            if not re.search(r"(?i)\b(apply|responsibilities|qualifications)\b", text):
                continue
            text, _form_tail = split_form(normalize_text(text))
            marks = heading_map(text)
            hits = match_all(text, index)
            stats = {"claimable": 0, "exposure": 0, "BLOCKED": 0, "never_req": 0, "never": 0}
            blocked_req = []
            never_req_detail = []
            for canonical, offsets in hits.items():
                entry = entries[canonical]
                st = status_of(entry)
                sec = best_section(offsets, marks)
                if st == "never":
                    stats["never"] += 1
                    if sec == "req":
                        stats["never_req"] += 1
                        req_offsets = [o for o in offsets if classify_section(o, marks) == "req"]
                        ctx = " ".join(text[max(0, o - 140): o + 120] for o in req_offsets)
                        never_req_detail.append((canonical, ctx))
                else:
                    stats[st] += 1
                    if st == "BLOCKED" and sec == "req":
                        blocked_req.append(canonical)
            unknown = candidate_terms(text, index)
            obtainable_clearance = re.compile(
                r"(?i)(ability to obtain|obtain and maintain|obtain a|clearance eligibility|eligible to obtain)")
            hard_never = [(c, ctx) for c, ctx in never_req_detail
                          if not (c == "security_clearance" and obtainable_clearance.search(ctx))]
            verdict = "OK"
            if hard_never:
                verdict = "SKIP (tier C hard requirement)"
            elif never_req_detail:
                verdict = "ESCALATE (clearance obtainable, confirm)"
            elif len(blocked_req) >= 4:
                verdict = "BASIS GAPS (%d in requirements)" % len(blocked_req)
            print("%-22s %-30s A=%-3d B=%-2d BLOCKED=%-3d C_req=%-2d unmapped=%-3d  %s"
                  % (os.path.basename(path).replace(".json", "")[:22], key[:30],
                     stats["claimable"], stats["exposure"], stats["BLOCKED"],
                     stats["never_req"], len(unknown), verdict))
            if blocked_req:
                print("    blocked in requirements: %s" % ", ".join(sorted(blocked_req)[:12]))
            if never_req_detail:
                print("    tier C in requirements: %s" % ", ".join(sorted(c for c, _ in never_req_detail)))
    print("")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--jd")
    ap.add_argument("--jd-json")
    ap.add_argument("--key")
    ap.add_argument("--variant")
    ap.add_argument("--scan-json", nargs="+")
    ap.add_argument("--facts", default=FACTS_PATH)
    args = ap.parse_args()

    facts = load_facts(args.facts)

    if args.scan_json:
        scan(args.scan_json, facts)
        return 0
    if not (args.jd or args.jd_json) or not args.key and args.jd_json:
        ap.error("need --jd or (--jd-json with --key); --variant optional")

    jd_text, label = load_jd(args, facts)
    variant_text, variant_name = None, None
    if args.variant:
        with open(args.variant, "r", encoding="utf-8") as fh:
            variant_text = fh.read()
        variant_name = os.path.basename(args.variant)
    report(jd_text, label, variant_text, variant_name, facts)
    return 0


if __name__ == "__main__":
    sys.exit(main())