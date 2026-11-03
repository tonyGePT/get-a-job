# Daily job pipeline

Discover new postings, decide fit, apply through the employer's own ATS, then
start TraceHire runs so outreach drafts land in Gmail. One Discord summary a day
tells you what happened and what needs a human.

**Location (moved 2026-11-02):** everything lives in `C:/tmp/jobharness/pipeline/`, inside the job-harness
directory that mirrors the Google Drive folder `job-harness`. It used to live in `C:/tmp/jobhunt/`, which now
holds only a pointer README. Nothing syncs to Drive automatically any more (the Drive folder is a manual
`gws` mirror): the code/prompts/config were uploaded once to `job-harness/pipeline/`.
Secrets (`discord_webhook.txt`) and `state/` stay local.

Generated per-application resumes (`<resume_prefix>_<key>.pdf`, `resume_<key>.txt`) are written to
`C:/tmp/jobharness/resumes/` (changed 2026-11-02; `resume_tailor.OUT_DIR`). The five variant templates stay in
the harness root. See the repo `README.md` for setup and what is kept private.

Everything runs natively on this Windows machine (since 2026-10-29; it ran under WSL
before): `C:\Python313\python.exe` for the deterministic stages and the orchestrator,
Windows `omp.exe` with managed Chromium for qualifying and form filling, the Windows
`gws` install for Gmail/Drive, and two cloud services we already pay for —
treg/BlitzAPI for discovery, TraceHire for contacts and drafts.

## Why it is split the way it is

| Stage | What runs it | Why |
|---|---|---|
| discover | Python → treg HTTP proxy (`/call/blitzapi.jobs.search`) | deterministic, ~$0.15/day with the 47 title profiles (engineering + ops/automation families added 2026-11-02; "Manager" titles pass only for those families, see `MANAGER_ALLOW` in discover.py) |
| qualify | `omp -p` + `workpool("scout")` | needs judgement: JD reading, ATS discovery, skip rules |
| prepare | Python (`prepare.py` → `resume_tailor.py`) | deterministic routing + 100%-coverage resume, no model in the loop |
| apply | `omp -p` + `workpool("task")` + Chromium + `gws` (emailed security codes) | DOMs differ per ATS; a script per ATS is 10 scripts to maintain |
| verify | Python + Gmail API | receipts are ground truth; never trust the agent's claim |
| trace | Python + TraceHire API | deterministic API, deduped on posting URL in state.db |
| digest | Python + Discord | your morning read, posted to the job channel |

A model failure can therefore corrupt nothing outside `apply` — and `verify`
downgrades anything unproven to `unverified` before it is logged or traced.

A day with **zero candidates** skips intake only. `verify`, `trace`, `record` and
`digest` still run: a receipt that arrives overnight, a run that finished, or a
log row that was never written are all work for today.

## Layout

```
C:/tmp/jobharness/pipeline/
  config.json          query profile, caps, notification mode
  daily.py             the orchestrator (msvcrt lock, per-stage logging, digest on failure)
  omp-pipeline.yml     per-run omp settings overlay (daily.py passes `--config`): default model
                       muskapi2/gpt-6.1-terra, task (workpool subagents) muskapi2/gpt-6-luna, retries max 3
                       with exponential backoff capped at 30 s, fallback openrouter/deepseek/deepseek-v4.1-flash.
                       Global ~/.omp config is not touched. Both prompts start with `orchestrate`.
  discover.py          BlitzAPI via treg → candidates (dedupe vs state.db decisions +
                       the history ledger); --results FILE filters a saved response offline
  prepare.py           verdicts → routed, keyword-verified resume PDFs → apply-queue.json
                       (also the first stage that stores verdicts in state.db)
  verify.py            Gmail receipts → submitted/unverified (append-only, one query/run)
  trace.py             start | poll | retry [--dry-run] | status (db-deduped, daily cap)
  ledger.py            check | add | list | outreach | resume-change: duplicate review and hand-run entries
                       against state.db (replaces application_log.md)
  digest.py            state → state/digest-<date>.md, posted to the Discord channel via lib/notify.py (no
                       email). Decisions grouped by final outcome, SKIPPED last; one line each: company, role,
                       location, salary. Header carries today's omp tokens/cost, summed from the session files
                       under ~/.omp/agent/sessions/--C--tmp-jobharness-pipeline--/ (incl. qualify/apply subagents).
  watchdog.py          ALERT when the heartbeat goes quiet
  lib/notify.py        Discord: `captcha --key K` (called live by the apply agent for CAPTCHAs it could NOT
                       solve), `scan` (after apply, deduped via state/discord-sent.json), `test`. Bot token in
                       discord_bot_token.txt + notify.discord_channel_id, else the webhook file.
  lib/captcha.py       `solve --method M --sitekey K --url U` → token on stdout (RapidAPI Fast MultiSolver:
                       reCAPTCHA v2/v3, hCaptcha, Turnstile, CaptchaFox). Key read from the `captcha-solver`
                       entry in ~/.omp/agent/mcp.json; cap `limits.max_captcha_solves_per_day` (default 10),
                       ledger state/captcha-solves.json. `status` shows today's usage. Creating the request
                       retries transient provider errors (no capacity, network, 429/5xx) with exponential
                       backoff 5 s…180 s, 8 attempts; exit 4 = still unavailable, escalate.
  prompts/qualify.md   agent contract: per-candidate verdicts + ATS URL + JD text
  prompts/apply.md     agent contract: submit queue, prove it, escalate the rest
  state/
    state.db           postings, runs, applications, receipts  ← the only state that matters
    candidates-latest.json, dropped-<date>.json   what was kept and why the rest was not
    verdicts/<date>.json, apply-queue.json
    evidence/<key>.{json,png}   per-application proof (the apply agent writes these)
    verification-<date>.json, trace-results-<date>.json   per-run artefacts of the agents
    digest-<date>.md, heartbeat.json, logs/<date>.log
    ledger.json, ledger.json.bak-<date>   DEAD, imported 2026-10-28 into state.db
```

`state.db` (stdlib sqlite3, WAL, 30s busy timeout) is the source of truth:

| Table | One row per | Written by |
|---|---|---|
| `postings` | discovered posting (keyed by normalised URL) | discover, prepare (verdicts incl. `location`, `remote`, `salary` as the qualify agent read them; added 2026-11-02), verify |
| `runs` | TraceHire run, one per posting URL ever | trace start/poll/retry |
| `applications` | evidence key | verify (sync of `evidence/*.json`) |
| `receipts` | Gmail message id | verify, append-only |
| `history` | one decision/outcome from before the pipeline or entered by hand (`source` = log-import or manual) | one-time import 2026-11-02, then `ledger.py add` |
| `outreach`, `resume_changes`, `log_notes` | outreach draft/send, resume edit, free-text note from the old log | one-time import, then `ledger.py outreach add` / `resume-change add` |

Every write is one row in one transaction. Nothing holds a whole-state copy, so a
manual command running while `trace.py poll` waits 30 minutes can no longer be
overwritten. Set `JOBHUNT_STATE=<dir>` to run any stage against a copy of `state/`.

## Daily commands

```sh
python C:/tmp/jobharness/pipeline/daily.py                  # full run (or: schtasks /Run /TN JobHuntDaily)
python C:/tmp/jobharness/pipeline/daily.py --discover-only  # cheap sanity check, no LLM, no browser
python C:/tmp/jobharness/pipeline/daily.py --no-apply       # qualify + resumes only
python C:/tmp/jobharness/pipeline/daily.py --days 7         # override the 3-day window after a gap
python trace.py status                          # outreach table
python trace.py start --dry-run                 # which applications would be traced, nothing posted
python trace.py retry --dry-run                 # which failures would be retried, and why not
python trace.py retry                           # retry transient failures (max 1 per URL)
python ledger.py check --company C --title T [--url U]   # duplicate review (exit 1 = prior record found)
python ledger.py add --company C --title T --url U --action applied|unapplied|skipped|escalated --notes N
python ledger.py list --days 7                  # recent history rows; also: outreach, resume-change
python digest.py --no-discord                   # rebuild the digest files, post nothing
python discover.py --results C:/tmp/saved.json  # run the dedupe offline, call nothing
python watchdog.py                              # is the pipeline alive?
```

`python` = `C:\Python313\python.exe` (has fpdf2 for resume PDFs). Any stage can be
pointed at a copy of the state with `JOBHUNT_STATE=<dir>`, e.g.

```sh
cp -r C:/tmp/jobharness/pipeline/state C:/tmp/jh-copy
JOBHUNT_STATE=C:/tmp/jh-copy python verify.py --days 2
```

A failed stage stops `daily.py` but it still posts a digest: a silent failure is worse than a noisy one.

## Scheduling (registered 2026-10-27)

Two Windows tasks, both hidden (wscript wrapper, no console flash):

| Task | Trigger | Command |
|---|---|---|
| `JobHuntDaily` | daily 08:00 (moved from 03:00 on 2026-11-03) | `wscript.exe C:\tmp\jobharness\pipeline\daily-hidden.vbs` |
| `JobHuntWatchdog` | every 120 min | `wscript.exe C:\tmp\jobharness\pipeline\watchdog-hidden.vbs` |

**No stage timeouts (2026-11-03).** The qualify and apply agents run until they finish. The model providers congest
during Asian business hours (roughly midnight to 05:00 PT), which is why the run starts at 08:00 PT. The cost of no
limit: a genuinely hung agent blocks the run, and the next day's run then exits at the `.lock` check ("another run
holds the lock") instead of stacking. `watchdog.py` still raises an ALERT after 26 h without a heartbeat.

The wrappers run `daily.bat` / `watchdog.bat`, which call `C:\Python313\python.exe`
directly. Re-register with `C:\tmp\jobharness\pipeline\register-tasks.bat` (idempotent, `/F` overwrites).
treg credentials are read from `%USERPROFILE%\.treg\config.json` (copied from WSL
`~/.treg` at the cutover; `treg login` on any machine rewrites the same shape).

```bat
schtasks /Query /TN JobHuntDaily /FO LIST /V      :: Status, Last Result, Next Run
schtasks /Run   /TN JobHuntDaily                  :: force a run now
schtasks /Change /TN JobHuntDaily /DISABLE        :: pause the pipeline
```

A paused readout: `Last Result 0` is success; `JobHuntWatchdog` writes
`state/logs/watchdog.log`, and silence beyond 26h leaves `state/ALERT`.

## Caps and guards already enforced

- `limits` in config.json: `max_candidates` 500, `max_applications_per_day` 50, `max_trace_runs_per_day` 10. The application cap is enforced by `prepare.py`, which trims the apply queue to that size (it caps attempts queued, not confirmed submissions). Parallel apply workers are capped separately at 8 by omp (`task.maxConcurrency`).
- Discovery (2026-10-29): two BlitzAPI searches per run, both paginated by cursor
  (`query.max_pages_per_search` × 50 rows): one for remote roles (Remote Solely/OK) and one for
  hybrid/on-site roles in the configured home-area cities (BlitzAPI tags most hybrid jobs On-site). Rolling
  3-day window, because postings land in the index a day or more late and a 1-day window caught ~1/10.
  `is_agency: false` — `true` means *only* agencies (the 10-27..10-29 runs had it inverted).
  Companies headquartered in `query.exclude_hq_cities` are excluded server-side
  (`company.hq.city.exclude`) and again by the qualify prompt (companies with no HQ on file).
  Same-role multi-city reposts are collapsed before the 3+-roles board check.
- `msvcrt` lock on `.lock` (held by `daily.py`): a hung run can never stack with the next one.
- Every excluded posting is written to `dropped-<date>.json` with a reason — no silent filtering.
- A posting is dropped by `discover.py` once it has a verdict **or** an application
  outcome in `state.db` (by normalised URL, or by company+title pair), and also when
  it already appears in the `history` ledger. A posting that was surfaced but never
  decided (qualify crashed) stays eligible.
- `runs` is keyed on the normalised job URL, so a posting is traced once, ever.
- Starts *and* retries both stamp `started`, and both spend from
  `max_trace_runs_per_day`: a retry cannot sneak past the daily cap.
- Retries are classified, not blanket: `start_failed`, `stale` (no terminal state in
  6h) and `failed` with a transient signature (`treg.web.extract … no page markdown`,
  transport/timeout, 429/5xx) are retried once; `no_people`, `no_confirmed_people`,
  `no_contact`, `killed`, `budget` are final — each retry costs ~$0.07.
- Runs stuck in `queued|running` past 6h are marked `stale` (clock = latest
  start/restart), not left to block polling.
- Receipts are append-only per Gmail message id and a confirmed submission is never
  downgraded; one Gmail list query per run covers every company, and only
  unconfirmed submissions are re-checked.
- Receipts must come from an ATS or employer domain; mail you send yourself is excluded.

## When it escalates to you

The digest lists anything the pipeline refused to submit: unsolved CAPTCHAs (image challenges, or a
token the form rejected twice), emailed verification codes, exact-salary demands, legal/export-control
wording, AI-attestation, mandatory cover letters, anonymous staffing reposts. Each has a
`state/evidence/<key>.json` with the fields already filled, so re-doing one by
hand is a copy-paste job.

## Known gaps

- Token CAPTCHAs are solved by `lib/captcha.py` (also mounted as the `captcha-solver` MCP server).
  Image/slider challenges and reCAPTCHA Enterprise variants the solver rejects still land in the
  escalation list. As of 2026-11-02 only the solver call itself is smoke-tested (Google's public demo
  sitekey); token injection into a live ATS form has not been exercised yet.
- Workday candidate accounts are created per employer; passwords are recorded in
  the evidence JSON.
- BlitzAPI's `is_agency` flag is weak — the blocklist regex and the
  "3+ roles in one window" board heuristic do most of the filtering.
- TraceHire posting-read failures (`treg.web.extract returned no page markdown`, and from
  2026-10-28 `could not read the job posting: ...`) get exactly one more attempt from
  `trace.py retry`, then stay failed and show in the digest. `trace.py retry --dry-run`
  lists the decision for every recorded run, including the ones it refuses and why.
- `trace.py start` traces an application by its POSTING URL (apply queue, else the
  posting's recorded apply URL), never by the evidence `final_url` (an ATS confirmation
  page). Applications made by hand outside the pipeline have no posting URL in state and
  are not traced here.

## State history

- 2026-10-28: `state/ledger.json` was replaced by `state/state.db` (postings, runs,
  applications, receipts). The import was a one-shot script — it backed the ledger up
  to `ledger.json.bak-2026-10-28`, imported 33 postings, 13 runs, 6 verdicts and 7
  applications (3 mail receipts), proved the counts and a field-by-field spot check,
  and was then deleted so that no code path reads or writes `ledger.json` again.
  `ledger.json` and its `.bak` remain in `state/` for reference only; nothing reads them.
- 2026-11-02: `application_log.md` was retired. Its Applications, Outreach drafts and Resume changes tables
  (533 + 80 + 12 rows) and three free-text sections were imported into `state.db` (`history`, `outreach`,
  `resume_changes`, `log_notes`), counts proved against the file. The `record` stage, `record.py`, the Drive
  log sync and `postings`/`applications` `logged_at`/`log_status` were removed. The frozen file is in
  `C:/tmp/jobharness/archive/application_log-final-2026-11-02.md`; a copy also remains on Drive but nothing
  syncs it. Use `ledger.py` for duplicate checks and hand-run entries.
- 2026-10-28 incident: the first end-to-end run after the migration traced every
  submitted application by its confirmation URL, so the six 2026-10-27 applications got
  second runs (8 paid runs incl. 2 retries, ~$0.56): one identical draft, two new second-contact
  drafts, and three failed reads. Those rows carry a `duplicate: ...` note in `runs`. Fixed the
  same day; `trace.py start --dry-run` now exists so this path is checked before any live POST.
- 2026-10-29: moved from WSL to native Windows. Under WSL the apply agent's `gws` resolved
  to an unauthenticated Linux shim, so Greenhouse security codes could not be read and two
  applications stalled at `needs_code`. `daily.sh` became
  `daily.py`; the treg CLI call became a direct HTTP call; every stored `/mnt/c/` path in
  `state.db` (59 `postings.jd_file` rows) and the state JSON sidecars was rewritten to
  `C:/` by a one-shot script, since deleted.

## TraceHire dependency (2026-10-28)

The trace stage depends on a TraceHire build whose page extractor accepts every shape the
routed `treg.web.extract` returns. Builds before `3920338d` read only `pages[0].markdown`;
when routing served `tinyfish.web.fetch` (its body lives in `text`), the run died with
`treg.web.extract returned no page markdown (200)` — the 6 Ashby failures on 2026-10-27.
The fix (`pageText()` accepts markdown | text | description) went live in
`3920338d-692d-4574-a08d-c952fd2081ca` at 2026-10-28 00:44 UTC; later tinyfish runs read
1,256–5,514 chars. The 2026-10-28 retries all happened to be served by crawl4ai/anyapi, so
they show the runs now succeed, not that the tinyfish path works.

- Since 2026-10-28 TraceHire reads Ashby, Greenhouse and Lever postings from the boards' free
  public APIs (trace step 9, `<ats>.posting-api`, $0) and only scrapes other URLs; a scrape under
  400 characters gets one direct `crawl4ai.web.scrape` attempt (step 15) before the run fails
  with `could not read the job posting: <provider> returned N characters, ...`.
- Live version `7c494f27-b1e0-40fd-9055-deeb16ef8bae` = commit `eb8390e` (2026-10-28 17:47 UTC).
  Rollback target `39c97246-8ad3-4c38-b3dd-2ca27be74da8` = commit `b0d2a02` (the snapshot of what
  ran before). Never roll back past `3920338d`: older builds fail on tinyfish-served pages.
- The repo is now committed (it had been deployed from an uncommitted tree). `npm run deploy` runs
  `scripts/deploy.mjs`: it refuses a dirty tree, runs typecheck + the full test suite, requires
  remote D1 migrations applied and no running/paused workflow instance, then deploys with
  `--tag <commit>`, so `wrangler deployments list` shows the commit behind every version.
  Watch cron is `["0 16 * * *"]` (owner's choice, 2026-10-28).
