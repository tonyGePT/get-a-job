# job-harness

A daily job-application pipeline for Windows: discover postings, have an LLM agent qualify them against your
rules, tailor a keyword-verified resume per posting, let browser agents submit the applications, verify each one
against ATS receipts, and post a summary to Discord. Deterministic Python does everything except judgement
(qualify) and form filling (apply); a bad model call cannot corrupt state.

```
discover -> qualify -> prepare -> apply -> verify -> trace -> digest
```

Start with [`pipeline/RUNBOOK.md`](pipeline/RUNBOOK.md): stages, commands, caps and guards, state schema.

## What is in this repo

| Path | What |
|---|---|
| `pipeline/` | The orchestrator (`daily.py`), one script per stage, `ledger.py` (duplicate checks and hand-run history), `lib/` (state, notifications, CAPTCHA solving), `prompts/` (agent contracts), `omp-pipeline.yml` (model, retry and fallback settings for `omp -p`), `config.example.json`. |
| `resume_tailor.py`, `keyword_coverage.py` | The resume tailoring engine: every resume carries 100% of the keywords its JD names, with each claim gated by a facts file. |

## What is deliberately NOT in this repo

Everything personal stays local and is gitignored (the `.gitignore` is an allowlist): `pipeline/config.json`,
`pipeline/state/` (SQLite state, evidence, job descriptions, logs), `profile/` (your decision rules and standing
answers), the resume templates, `keyword_facts.json`, `blitz_answers.md`, `resumes/`, `routing.json`, and all
secrets (webhook, API keys).

## Setting it up

1. Python 3.13 on Windows, `pip install -r requirements.txt`, the `omp` CLI on `PATH`, and the
   `gws` (Google Workspace) CLI for Gmail receipts.
2. `copy pipeline\config.example.json pipeline\config.json` and fill it in (paths, notification settings,
   `resume_prefix`, your search profile).
3. Create the private inputs the prompts expect, next to this README:
   - `profile/qualify_rules.md`: your SKIP / ESCALATE / APPLY rules and location rule.
   - `profile/apply_rules.md`: standing answers, consents, what to skip while applying.
   - `blitz_answers.md`: the only facts an agent may type into a form.
   - `resume_GTM.txt`, `resume_FDE.txt`, `resume_AI.txt`, `resume_TOOLS.txt`, `resume_AI-HEALTH.txt`: resume
     variants, and `keyword_facts.json` for the tailoring engine. Optional `routing.json` maps JD keys to variants.
4. Register the scheduled tasks with `pipeline\register-tasks.bat`.

Nothing here syncs to Google Drive automatically.
