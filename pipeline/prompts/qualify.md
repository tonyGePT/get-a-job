orchestrate

# Daily qualification pass

You are qualifying today's job candidates for the candidate whose playbook and answers are listed below. Work only from
`state/candidates-latest.json`; do not source new postings.

## Inputs
- `state/candidates-latest.json` — candidate list (url, company, title, posted, location, ai_summary).
- `C:/tmp/jobharness/resume_routing_context.md` — the playbook: read sections 3 (hard routing), 6 (location/comp), 7 (skip/escalate), 9 (truthfulness).
- `C:/tmp/jobharness/blitz_answers.md` — the facts you may rely on.
- `config.json` — caps (`limits.max_candidates`).

## Per candidate
1. `read` the LinkedIn URL (public guest view returns the full JD). If it fails, try `https://www.linkedin.com/jobs/view/<id>` once more with the id extracted from the URL.
2. Save the raw JD text, exactly as read, to `state/jd/<key>.txt`. Key = company slug + role slug, lowercase, dashes (e.g. `acme-forward-deployed-engineer`). These become resume-tailoring inputs, so include title, location, salary and the full body.
3. Find the employer's OWN application URL by reading their careers page and the usual ATS hosts (`job-boards.greenhouse.io`, `jobs.ashbyhq.com`, `jobs.lever.co`, `*.workable.com`, `jobs.gem.com`, `applytojob.com`, `*.teamtailor.com`, `*.myworkdayjobs.com`). Verify the page shows this role. LinkedIn/Indeed/aggregator links are never the apply URL.
4. Decide a verdict using the playbook AND `C:/tmp/jobharness/profile/qualify_rules.md`. Read that file in full before deciding any candidate: it holds the candidate's binding SKIP / ESCALATE / APPLY rules and location rule, and nothing in this prompt overrides it. In short:
   - `APPLY` — an employer ATS page exists and no hard rule is violated.
   - `SKIP` — a hard skip rule in the rules file applies.
   - `ESCALATE` — the rules file says the decision belongs to the candidate (exact-salary demand, legal representation, mandatory cover letter, paid assessment, AI-attestation, ...).
5. Apply the location rule from the rules file; a role that requires living somewhere the rules do not allow is a SKIP even if LinkedIn says "remote".

## Output
Write `state/verdicts/<today>.json` (today = `date +%F`) as:

```json
{"generated": "<iso>", "items": [
 {"key": "", "company": "", "title": "", "url": "<linkedin url>",
  "apply_url": "", "ats": "greenhouse|ashby|lever|workable|gem|jazzhr|teamtailor|workday|other|none",
  "verdict": "APPLY|SKIP|ESCALATE", "reason": "<=25 words",
  "hard_blockers": [], "location": "", "remote": "remote-us|hybrid|on-site|unknown",
  "salary": null, "jd_file": "C:/tmp/jobharness/pipeline/state/jd/<key>.txt"}
]}
```

## Rules
- Use `workpool("task")` (or `scout`) with one item per candidate, all in ONE call so candidates are qualified in parallel (concurrency is capped at 8; excess items queue) — never read candidates one after another.
- Never invent a JD, an apply URL, a salary, or a blocker. If the page cannot be read, verdict `ESCALATE` with reason "unreadable posting".
- Do not apply to anything, do not write to `state/state.db` or the `history` ledger, do not send email.
- Print a one-line summary per candidate when done.
