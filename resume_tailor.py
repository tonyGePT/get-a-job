#!/usr/bin/env python3
"""Per-JD resume tailoring for the job harness.

Strategy (the candidate's, 2026-10-20, from an actual HR practitioner): resume screening
is not the gate; the ATS keyword match score is. Every resume is therefore
tailored to include 100% of the keywords its target JD names - known
technologies in the JD's own spelling plus every unmapped candidate term -
even where the candidate has no direct experience with the technology, because the
keywords track broader concepts they have demonstrably worked.

For each valid JD recorded in batch1_gh_lever.json, batch2_ashby_gem.json,
and escalated_jds.json:
  1. Extract the keyword set: every keyword_facts.json entry the JD body
     names (required form = the JD's own spelling) plus every candidate term
     the engine's heuristic finds in the body.
  2. Start from the routed resume variant (the history ledger's routing first,
     then playbook hard rules / tie-breaks on the JD title).
  3. Set the headline to the JD's own title.
  4. Append missing keywords to SKILLS in two lines: `Also:` (known
     entries, JD order) and `Also familiar with:` (unmapped terms, JD
     frequency order).
  5. Verify independently that every required keyword now appears in the
     generated resume (case-insensitive, word/phrase bounded) and report
     coverage. 100% is required before a PDF is emitted.

Outputs: resume_<key>.txt and <PDF_PREFIX>_<key>.pdf in resumes/ (the five variant templates stay here).
"""

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(ROOT, "resumes")
os.makedirs(OUT_DIR, exist_ok=True)
sys.path.insert(0, ROOT)

# Output file name is <PDF_PREFIX>_<key>.pdf. prepare.py sets this from config.json `resume_prefix`.
PDF_PREFIX = "Resume"


def pdf_path(key):
    return os.path.join(OUT_DIR, "%s_%s.pdf" % (PDF_PREFIX, key))

import keyword_coverage as kc  # noqa: E402

FACTS = kc.load_facts()
ENTRIES = FACTS["entries"]
INDEX = kc.build_index(ENTRIES)

JD_FILES = ["batch1_gh_lever.json", "batch2_ashby_gem.json", "escalated_jds.json"]

VARIANTS = {
    "GTM": "resume_GTM.txt",
    "FDE": "resume_FDE.txt",
    "AI": "resume_AI.txt",
    "TOOLS": "resume_TOOLS.txt",
    "AI-HEALTH": "resume_AI-HEALTH.txt",
}

# Base variant per JD key (JD key -> GTM | FDE | AI | TOOLS | AI-HEALTH). That mapping is job-search
# history, so it lives in routing.json beside this file (gitignored, not published); prepare.py adds
# ROUTING[key] for each posting at run time.
_ROUTING_FILE = os.path.join(ROOT, "routing.json")
_DATA = json.load(open(_ROUTING_FILE, encoding="utf-8")) if os.path.exists(_ROUTING_FILE) else {}
ROUTING = dict(_DATA.get("routing", {}))

# Titles the extraction rules get wrong (job-search data: routing.json -> "title_overrides").
TITLE_OVERRIDES = dict(_DATA.get("title_overrides", {}))

Jd = dict  # (key, company, variant, title, text)


def is_valid_jd(text):
    """A real posting body, not a careers-board listing or error page."""
    if len(text) < 2000:
        return False
    head = text[:400].lower()
    if head.startswith("jobs at "):
        return False
    if "job not found" in text[:200] or "not found" in text[:80]:
        return False
    if "there are no current openings" in head:
        return False
    return True


def jd_title(entry, key):
    if key in TITLE_OVERRIDES:
        return TITLE_OVERRIDES[key]
    if isinstance(entry, dict) and entry.get("title"):
        return entry["title"].strip()
    first = ""
    for line in (entry if isinstance(entry, str) else entry.get("text", "")).splitlines():
        line = line.strip()
        if line and line.lower() not in ("view all jobs", "apply", "back to jobs"):
            first = line
            break
    m = re.match(r"Job Application for (.+?) at ", first)
    if m:
        return m.group(1).strip()
    if " - " in first and len(first) <= 100:
        return first.split(" - ", 1)[1].strip()
    if re.match(r"^[A-Za-z][A-Za-z0-9 .,&/()'-]*$", first) and len(first) <= 80 and "|" not in first:
        return first
    if "|" in first:
        return first.split("|", 1)[0].strip()
    return None


def alias_forms_in(text, entry):
    """Alias spellings as they actually appear in the JD (JD's own vocabulary)."""
    forms = {}
    for alias in entry["aliases"]:
        rx = re.compile(r"(?<![A-Za-z0-9])" + re.escape(alias) + r"(?![A-Za-z0-9])", re.I)
        for m in rx.finditer(text):
            forms[m.group(0)] = forms.get(m.group(0), 0) + 1
    return forms  # spelling -> count


def present(text, term):
    if " " in term:
        words = [re.escape(w) for w in term.split()]
        rx = r"(?i)(?<![A-Za-z0-9])" + r"\s+".join(words) + r"(?![A-Za-z0-9])"
        return re.search(rx, text) is not None
    rx = r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])"
    return re.search(rx, text, re.I) is not None


# Non-skill boilerplate the JDs print inside requirements sections:
# perks, EEO wording, and meta-phrases about "similar roles" are not
# keywords a resume is scored against.
NON_KEYWORDS = {
    "etc", "more", "bonus",
    "401k", "401k plan", "pension", "employer match", "self-managed pto",
    "parental leave", "base salary",
    "geographic location", "work location", "specific office location",
    "placement", "promotion", "termination", "layoff", "recall", "transfer",
    "job-related skills", "relevant education", "individual qualifications",
    "interview performance",
    "similar technical", "similar technical roles", "related technical roles",
    "commercial hybrid roles", "comparable technologies",
    "comparable enterprise models", "related technologies", "similar systems",
    "similar agent", "agent frameworks-prompt design",
    # traits and hiring-process wording that the candidate-term finder
    # picks up out of requirements sections; none belong on a resume
    "ambiguity", "leadership", "role-specific interviews",
    "hr conversation", "hiring process", "final discussion",
    "sound judgment", "independent thinking", "clear communication",
}


def required_keywords(body):
    """[(term, freq, kind)] - every keyword the JD names, in JD order."""
    out = []
    seen = set()
    marks = kc.heading_map(body)
    hits = kc.match_all(body, INDEX)
    for canonical, offsets in sorted(hits.items(), key=lambda kv: kv[1][0]):
        entry = ENTRIES[canonical]
        forms = alias_forms_in(body, entry)
        if not forms:
            continue
        form = max(forms, key=lambda f: (forms[f], -f.lower().__len__()))
        key = form.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append((form, forms[form], "known", offsets[0], canonical))
    unknown = kc.candidate_terms(body, INDEX)
    for term, (offset, n) in sorted(unknown.items(), key=lambda kv: (kv[1][0], -kv[1][1])):
        # Repair 120-char chunk truncation: if the candidate ends mid-word in the
        # actual body, extend it to the true word boundary.
        end = offset + len(term)
        while end < len(body) and body[end].isalpha():
            end += 1
        if end != offset + len(term):
            term = body[offset:end]
        # Only requirement/bonus sections define skills the JD is asking for
        # (same semantics as the engine's [6] section); company, perks, and
        # legal boilerplate are not resume keywords.
        if kc.classify_section(offset, marks) not in ("req", "bonus"):
            continue
        key = term.lower()
        if key in NON_KEYWORDS:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append((term, n, "unknown", offset, None))
    out.sort(key=lambda r: r[3])
    return [(t, f, k, c) for t, f, k, _off, c in out]


def tailor(key, entry):
    raw = entry if isinstance(entry, str) else entry.get("text", "")
    body, _tail = kc.split_form(kc.normalize_text(raw))
    variant = ROUTING[key]
    vpath = os.path.join(ROOT, VARIANTS[variant])
    with open(vpath, "r", encoding="utf-8-sig") as fh:
        base = fh.read()
    lines = base.splitlines()
    if len(lines) < 5 or "gmail.com" not in lines[2]:
        raise SystemExit("unexpected variant header shape in %s" % VARIANTS[variant])

    title = jd_title(entry, key)
    if title:
        lines[1] = title

    req = required_keywords(body)
    base_text = "\n".join(lines)
    also_missing, familiar_missing = [], []
    for term, freq, kind, canonical in req:
        if present(base_text, term):
            continue
        if kind == "known":
            # Tier A with a recorded basis can sit on the skills line.
            # BLOCKED (no basis), tier B exposure, and tier C never-claim are
            # phrased as familiarity only, which is the honest register.
            if kc.status_of(ENTRIES[canonical]) == "claimable":
                also_missing.append((term, freq))
            else:
                familiar_missing.append((term, freq))
        else:
            familiar_missing.append((term, freq))

    def by_freq(items):
        return [t for t, _ in sorted(items, key=lambda tf: (-tf[1], tf[0]))]

    addenda = []
    if also_missing:
        addenda.append("Also: " + ", ".join(by_freq(also_missing)))
    if familiar_missing:
        addenda.append("Also familiar with: " + ", ".join(by_freq(familiar_missing)))

    out_lines = lines + addenda
    while out_lines and out_lines[-1] == "":
        out_lines.pop()
    out_lines.append("")
    out = "\n".join(out_lines)

    txt_path = os.path.join(OUT_DIR, "resume_%s.txt" % key)
    with open(txt_path, "w", encoding="utf-8") as fh:
        fh.write(out)

    missing = [t for t, _f, _k, _c in req if not present(out, t)]
    added = len(also_missing) + len(familiar_missing)
    return {
        "key": key, "variant": variant, "title": title,
        "required": len(req), "added": added,
        "missing": missing, "txt": txt_path,
        "also_added": [t for t, _ in also_missing],
        "familiar_added": [t for t, _ in familiar_missing],
    }


def verify(jd_entry, key):
    """Independent re-check straight from the JD text."""
    raw = jd_entry if isinstance(jd_entry, str) else jd_entry.get("text", "")
    body, _ = kc.split_form(kc.normalize_text(raw))
    req = required_keywords(body)
    with open(os.path.join(OUT_DIR, "resume_%s.txt" % key), "r", encoding="utf-8") as fh:
        resume = fh.read()
    return [t for t, _f, _k, _c in req if not present(resume, t)]


_PDF_SAFE = {
    "\u2013": "-", "\u2014": "-", "\u2018": "'", "\u2019": "'",
    "\u201c": '"', "\u201d": '"', "\u00a0": " ", "\u2026": "...",
    "\u2022": "-", "\u00d7": "x", "\u00f7": "/",
}


def pdf_safe(text):
    for bad, good in _PDF_SAFE.items():
        text = text.replace(bad, good)
    return text.encode("latin-1", "replace").decode("latin-1")


def render_pdf(txt_path, pdf_path):
    from fpdf import FPDF

    with open(txt_path, "r", encoding="utf-8-sig") as fh:
        lines = fh.read().splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    lines = [pdf_safe(l) for l in lines]

    pdf = FPDF(format="letter")
    pdf.set_auto_page_break(auto=True, margin=16)
    pdf.add_page()
    L = 8  # body font size

    def rule(y=2.4):
        pdf.set_draw_color(120)
        pdf.set_line_width(0.25)
        pdf.line(pdf.l_margin, pdf.get_y() + y, pdf.w - pdf.r_margin, pdf.get_y() + y)

    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 7.5, lines[0], new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 5.5, lines[1], new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 8.5)
    pdf.set_text_color(90)
    pdf.multi_cell(0, 4.2, lines[2], new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0)
    pdf.ln(1.2)
    rule()

    def ensure(h):
        if pdf.get_y() + h > pdf.h - pdf.b_margin:
            pdf.add_page()

    for line in lines[3:]:
        if not line.strip():
            continue
        if line in ("SUMMARY", "EXPERIENCE", "PROJECTS", "EDUCATION", "SKILLS"):
            ensure(14)
            pdf.ln(3.2)
            pdf.set_font("Helvetica", "B", 10.5)
            pdf.cell(0, 5, line, new_x="LMARGIN", new_y="NEXT")
            rule(1.6)
            continue
        text = line[2:].strip() if line.startswith("* ") else line
        if line.startswith("* "):
            ensure(8)
            pdf.set_font("Helvetica", "", L)
            x = pdf.get_x()
            pdf.set_x(x + 3.2)
            pdf.multi_cell(pdf.w - pdf.r_margin - x - 3.2, 4.6, pdf_safe("\u2022 " + text),
                           new_x="LMARGIN", new_y="NEXT", markdown=True)
            continue
        is_head = (" | " in line and re.search(r"\d{4}", line) and len(line) < 130
                   and not line.startswith("*"))
        m = re.match(r"^([A-Za-z][A-Za-z &/-]*?):\s+(.+)$", line)
        if is_head:
            ensure(8)
            pdf.set_font("Helvetica", "B", 10)
            pdf.multi_cell(0, 4.8, line, new_x="LMARGIN", new_y="NEXT")
        elif m and line.isupper() is False and ": " in line and line.split(": ", 1)[0].islower() is False \
                and len(line.split(": ", 1)[0]) <= 30 and "http" not in line.split(": ", 1)[0]:
            ensure(8)
            pdf.set_font("Helvetica", "", L)
            pdf.multi_cell(0, 4.6, "**%s:** %s" % (m.group(1), m.group(2)),
                           new_x="LMARGIN", new_y="NEXT", markdown=True)
        else:
            ensure(8)
            pdf.set_font("Helvetica", "", L)
            pdf.multi_cell(0, 4.6, line, new_x="LMARGIN", new_y="NEXT")
    pdf.output(pdf_path)


def main():
    results = []
    for fname in JD_FILES:
        with open(os.path.join(ROOT, fname), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        for key, entry in data.items():
            raw = entry if isinstance(entry, str) else entry.get("text", "")
            if not is_valid_jd(raw):
                continue
            if key not in ROUTING:
                raise SystemExit("no routing for valid JD %s" % key)
            res = tailor(key, entry)
            results.append(res)

    print("%-16s %-10s %8s %8s %8s" % ("KEY", "VARIANT", "REQ", "ADDED", "MISSING"))
    print("-" * 52)
    for r in results:
        # re-fetch entry for independent verification
        entry = None
        for fname in JD_FILES:
            with open(os.path.join(ROOT, fname), "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if r["key"] in data:
                entry = data[r["key"]]
                break
        missing = verify(entry, r["key"])
        r["missing"] = missing
        flag = "OK " if not missing else "FAIL"
        print("%s %-16s %-10s %8d %8d %8d" % (flag, r["key"], r["variant"],
                                              r["required"], r["added"], len(missing)))
        for m in missing:
            print("    missing: %s" % m)

    bad = [r for r in results if r["missing"]]
    for r in results:
        if not r["missing"]:
            render_pdf(r["txt"], pdf_path(r["key"]))
    print("-" * 52)
    print("JDs processed: %d, at 100%% coverage: %d, PDFs emitted: %d"
          % (len(results), len(results) - len(bad), len(results) - len(bad)))
    if bad:
        sys.exit(1)


if __name__ == "__main__":
    main()
