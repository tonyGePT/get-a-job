# Daily job pipeline

Discover new postings, decide fit, apply through the employer's own ATS, and verify each application against its
confirmation email. One Discord summary a day tells you what happened and what needs a human. An optional private
extension (`trace.py`, not published) can add outreach after verify; it is skipped when the file is absent.

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
`gws` install for Gmail/Drive, and treg/BlitzAPI for discovery.

## Why it is split the way it is

| Stage | What runs it | Why |
|---|---|---|
| discover | Python → treg HTTP proxy (`/call/blitzapi.jobs.search`) | deterministic, ~$0.15/day with the 47 title profiles (engineering + ops/automation families added 2026-11-02; "Manager" titles pass only for those families, see `MANAGER_ALLOW` in discover.py) |
| qualify | `omp -p` + `workpool("scout")` | needs judgement: JD reading, ATS discovery, skip rules |
| prepare | Python (`prepare.py` → `resume_tailor.py`) | deterministic routing + 100%-coverage resume, no model in the loop |
| apply | `omp -p` + `workpool("task")` + Chromium + `gws` (emailed security codes) | DOMs differ per ATS; a script per ATS is 10 scripts to maintain |
| verify | Python + Gmail API | receipts are ground truth; never trust the agent's claim |
| trace (optional, private) | `trace.py`, not published | skipped unless `pipeline/trace.py` and a `tracehire` config block exist |
| digest | Python + Discord | your morning read, posted to the job channel |
| retro (post-run) | `retro.py` → deterministic telemetry + one `omp -p` review pass | turns yesterday's wasted tokens and broken stages into gated, revertible changes; runs after digest and again at 12:00 |

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
                       muskapi2/gpt-6.1-terra (fallback anthropic/claude-sonnet-5-5), task (workpool subagents)
                       openrouter/deepseek/deepseek-v4.1-flash with no fallback; retries max 5 with exponential backoff capped at 30 s.
                       Global ~/.omp config is not touched. Both prompts start with `orchestrate`.
  discover.py          BlitzAPI via treg → candidates (dedupe vs state.db decisions +
                       the history ledger); --results FILE filters a saved response offline
  prepare.py           verdicts → routed, keyword-verified resume PDFs → apply-queue.json
                       (also the first stage that stores verdicts in state.db)
  verify.py            Gmail receipts → submitted/unverified (append-only, one query/run)
  trace.py             OPTIONAL private extension, not published; daily.py runs it only if the file exists
  ledger.py            check | add | list | outreach | lead | resume-change: duplicate review and hand-run entries
                       against state.db (replaces application_log.md and outreach_leads.md)
  digest.py            state → state/digest-<date>.md, posted to the Discord channel via lib/notify.py (no
                       email). Decisions grouped by final outcome, SKIPPED last; one line each: company, role,
                       location, salary. Header carries today's omp tokens/cost, summed from the session files
                       under ~/.omp/agent/sessions/--C--tmp-jobharness-pipeline--/ (incl. qualify/apply subagents).
  watchdog.py          ALERT when the heartbeat goes quiet
  retro.py             post-run troubleshooter: telemetry → one omp review → gated changes →
                       state/retro-<date>.md + its own Discord digest. Also: --status, --backfill,
                       --dry-run, --no-agent, --digest. Holds the same .lock as daily.py, so it can
                       never run beside a live pipeline.
  lib/retro_telemetry.py  deterministic evidence packet: funnel, per-stage tokens/cost, tool calls,
                       skip reasons, apply outcomes, stage durations, hygiene, findings. Python
                       computes every number; the agent never parses raw logs or transcripts.
  prompts/retro.md     agent contract: read the packet, propose JSON changes, edit nothing.
  pow.py               proof-of-work artifacts: generate (one bounded omp pass) -> deterministic
                       validation -> store; signals (the only paid call: postings for a gtm_signal
                       artifact, budget-capped, stored under state/pow/signals/); run (built-in
                       ai_eval evaluator, or --execute for the artifact's own code);
                       publish/attach/revoke/status/reconcile; `smoke` is the offline end-to-end
                       scenario (temp DB + local stub host + a fixture treg response, no paid call).
  lib/pow_store.py     the additive pow_artifacts / pow_events DDL (single owner), atomic files
                       under state/pow/ (artifacts, candidates, refused, exec scratch, fetched
                       signals), canonical JSON SHA-256 = the dedupe key.
  pow_metrics.py       attribution CLI (record|report|status|pending) over pow_events; reads
  lib/pow_telemetry.py state.db only, never sends mail, never enters the retro KPI dict.
  prompts/pow.md       agent contract for one artifact candidate (envelope is Python's, evidence
  prompts/pow-*.md     is the agent's); one recipe section per recipe (gtm_signal, fde_integration,
                       ai_eval), each with its own minimums and abstention rules.
  lib/notify.py        Discord: `captcha --key K` (called live by the apply agent for CAPTCHAs it could NOT
                       solve), `scan` (after apply, deduped via state/discord-sent.json), `test`,
                       `retro` (stdin → the retro webhook). Bot token in
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
    state.db           postings, runs, applications, receipts, history, outreach, leads, resume_changes, log_notes  ← the only state that matters
    candidates-latest.json, dropped-<date>.json   what was kept and why the rest was not
    verdicts/<date>.json, apply-queue.json
    evidence/<key>.{json,png}   per-application proof (the apply agent writes these)
    verification-<date>.json, trace-results-<date>.json   per-run artefacts of the agents
    digest-<date>.md, heartbeat.json, logs/<date>.log
    retro-<date>.md, retro-<date>-evidence.json   the troubleshooter's report and evidence packet
    retro-<date>-proposals.json   the agent's machine-readable proposals (rejected or applied)
    pow/artifacts/<id>.json       accepted PoW envelopes: exactly what publish sends
    pow/candidates/, pow/rejected/, pow/tmp/   raw agent candidates, refused candidates, exec scratch
    pow/signals/<token>.jsonl     every posting `pow.py signals` fetched for one artifact (the only
    pow/signals/<token>.meta.json rows a gtm_signal artifact may cite) and its per-call spend ledger
    pow/task.json                 the current agent pass: recipe, target, signals_token, signals_path,
                                  fetch caps and the one file the agent may write
    discover-overrides.json, ats-hints.json   optional rules grown by retro.py; absent = no-op
    ledger.json, ledger.json.bak-<date>   DEAD, imported 2026-10-28 into state.db
```

`state.db` (stdlib sqlite3, WAL, 30s busy timeout) is the source of truth:

| Table | One row per | Written by |
|---|---|---|
| `postings` | discovered posting (keyed by normalised URL) | discover, prepare (verdicts incl. `location`, `remote`, `salary` as the qualify agent read them; added 2026-11-02), verify |
| `runs` | run of the optional private trace extension, one per posting URL ever | trace.py (when installed) |
| `applications` | evidence key | verify (sync of `evidence/*.json`) |
| `receipts` | Gmail message id | verify, append-only |
| `history` | one decision/outcome from before the pipeline or entered by hand (`source` = log-import or manual) | one-time import 2026-11-02, then `ledger.py add` |
| `outreach`, `resume_changes`, `log_notes` | outreach draft/send, resume edit, free-text note from the old log | one-time import, then `ledger.py outreach add` / `resume-change add` |
| `retro_runs` | one post-run review: KPIs, finding count, agent tokens/cost | retro.py |
| `retro_actions` | one self-healing change: target, patch, metric, baseline, grade, outcome | retro.py |
| `pow_artifacts` | one PoW artifact (abstained/rejected rows included, with the reason) | pow.py |
| `pow_events` | one attribution event: generated/published/attached/revoked/blocked context, or a recorded outcome | pow.py, pow_metrics.py |

Every write is one row in one transaction. Nothing holds a whole-state copy, so a
manual command running during a long poll can no longer be
overwritten. Set `JOBHUNT_STATE=<dir>` to run any stage against a copy of `state/`.

## Daily commands

```sh
python C:/tmp/jobharness/pipeline/daily.py                  # full run (or: schtasks /Run /TN JobHuntDaily)
python C:/tmp/jobharness/pipeline/daily.py --discover-only  # cheap sanity check, no LLM, no browser
python C:/tmp/jobharness/pipeline/daily.py --no-apply       # qualify + resumes only
python C:/tmp/jobharness/pipeline/daily.py --days 7         # override the 3-day window after a gap
python ledger.py check --company C --title T [--url U]   # duplicate review (exit 1 = prior record found)
python ledger.py add --company C --title T --url U --action applied|unapplied|skipped|escalated --notes N
python ledger.py list --days 7                  # recent history rows; also: outreach, resume-change
python C:/tmp/jobharness/pipeline/pow.py generate --url U    # one PoW artifact (metered omp pass)
python C:/tmp/jobharness/pipeline/pow.py generate --url U --candidate FILE   # validate/store only
python C:/tmp/jobharness/pipeline/pow.py signals --titles "driver recruiter,fleet safety manager" --days 21 --countries US
python C:/tmp/jobharness/pipeline/pow.py signals --titles "driver recruiter" --icp-keywords "trucking,freight" --sizes "51-200,201-500"
python C:/tmp/jobharness/pipeline/pow.py signals --emit-sources   # stored postings as sources entries (free; acct sources are written by Python anyway)
python C:/tmp/jobharness/pipeline/pow.py signals --dry-run --titles T --days N   # free: the request only
python C:/tmp/jobharness/pipeline/pow.py validate FILE --url U --company C --role R --recipe R
python C:/tmp/jobharness/pipeline/pow.py generate --url U --candidate FILE --signals FILE  # hand-written candidate
                                       # (--signals / JOBHUNT_POW_SIGNALS = the store it must be grounded in)
python C:/tmp/jobharness/pipeline/pow.py run <artifact_id>   # ai_eval: built-in evaluator
python C:/tmp/jobharness/pipeline/pow.py run <artifact_id> --execute         # FDE code, opt-in
python C:/tmp/jobharness/pipeline/pow.py publish <artifact_id>               # needs pow.publish_enabled
python C:/tmp/jobharness/pipeline/pow.py attach <artifact_id> --run <run id> # needs pow.attach_enabled
python C:/tmp/jobharness/pipeline/pow.py status | reconcile --readonly | revoke <artifact_id>
python C:/tmp/jobharness/pipeline/pow.py smoke               # offline, no agent, no paid call
python C:/tmp/jobharness/pipeline/pow_metrics.py report --days 30            # attribution counts
python C:/tmp/jobharness/pipeline/retro.py                  # review + self-heal (also runs inside daily.py)
python C:/tmp/jobharness/pipeline/retro.py --status         # KPI table + pending changes
python C:/tmp/jobharness/pipeline/retro.py --dry-run        # collect + report only, no agent, no changes
python C:/tmp/jobharness/pipeline/retro.py --no-agent       # collect + grade + report, skip the model pass
python C:/tmp/jobharness/pipeline/retro.py --backfill 30    # seed history from past logs
python C:/tmp/jobharness/pipeline/retro.py --digest         # re-post today's self-healing digest
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
Three Windows tasks, all hidden (wscript wrapper, no console flash):

| Task | Trigger | Command |
|---|---|---|
| `JobHuntDaily` | daily 08:00 (moved from 03:00 on 2026-11-03) | `wscript.exe C:\tmp\jobharness\pipeline\daily-hidden.vbs` |
| `JobHuntWatchdog` | every 120 min | `wscript.exe C:\tmp\jobharness\pipeline\watchdog-hidden.vbs` |
| `JobHuntRetro` | daily 12:00 (4 h after the start) | `wscript.exe C:\tmp\jobharness\pipeline\retro-hidden.vbs` |

**Retro timing.** `daily.py` calls `retro.py --in-process` after `digest`, so the normal path needs no timer.
`JobHuntRetro` at 12:00 is the fallback for a run that died before reaching digest, and it is safe to leave
enabled: retro takes the same `.lock` non-blocking and exits immediately while a run is still going (no stage
timeouts, so a late run can still be in `apply` at noon). It also exits unless `state/digest-<today>.md` exists,
so a day with no run does nothing.

**No stage timeouts (2026-11-03).** The qualify and apply agents run until they finish. The model providers congest
during Asian business hours (roughly midnight to 05:00 PT), which is why the run starts at 08:00 PT. The cost of no
limit: a genuinely hung agent blocks the run, and the next day's run then exits at the `.lock` check ("another run
holds the lock") instead of stacking. `watchdog.py` still raises an ALERT after 26 h without a heartbeat.

The wrappers run `daily.bat` / `watchdog.bat` / `retro.bat`, which call `C:\Python313\python.exe`
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

- `limits` in config.json: `max_candidates` 500, `max_applications_per_day` 50. The application cap is enforced by `prepare.py`, which trims the apply queue to that size (it caps attempts queued, not confirmed submissions). Parallel apply workers are capped separately at 8 by omp (`task.maxConcurrency`).
- Discovery (2026-10-29): two BlitzAPI searches per run, both paginated by cursor
  (`query.max_pages_per_search` × 50 rows): one for remote roles (Remote Solely/OK) and one for
  hybrid/on-site roles in the configured home-area cities (BlitzAPI tags most hybrid jobs On-site). Rolling
  3-day window, because postings land in the index a day or more late and a 1-day window caught ~1/10.
  `is_agency: false` — `true` means *only* agencies (the 10-27..10-29 runs had it inverted).
- `query.exclude_hq_cities` remains available for the candidate's private cultural-fit exclusions; those values stay in local `config.json` and are not copied into tracked files. Headquarters do not disqualify US-remote roles except for explicitly configured private exclusions; LA hybrid/on-site search uses configured home-area cities.
  Same-role multi-city reposts are collapsed before the 3+-roles board check.
- `msvcrt` lock on `.lock` (held by `daily.py`): a hung run can never stack with the next one.
- Every excluded posting is written to `dropped-<date>.json` with a reason — no silent filtering.
- A posting is dropped by `discover.py` once it has a verdict **or** an application
  outcome in `state.db` (by normalised URL, or by company+title pair), and also when
  it already appears in the `history` ledger. A posting that was surfaced but never
  decided (qualify crashed) stays eligible.
- Receipts are append-only per Gmail message id and a confirmed submission is never
  downgraded; one Gmail list query per run covers every company, and only
  unconfirmed submissions are re-checked.
- Receipts must come from an ATS or employer domain; mail you send yourself is excluded.
- **Retro self-healing is gated, not free-form.** The agent writes two files under `state/` and nothing else;
  every change is applied by `retro.py`, which (a) only accepts targets in `RETRO_WRITABLE`, (b) never accepts
  `profile/`, `config.json`, `state/` or `trace.py`, (c) re-compiles every affected `.py` and re-checks required
  prompt sections before writing, (d) keeps a `.retro-bak` copy, (e) records the change in `retro_actions` with the
  KPI it claims to move, and (f) auto-reverts it if that KPI regresses for two later runs. Data-only changes
  (`blocklist`, `discover-overrides.json`, `ats-hints.json`) are merged by Python; patterns are regex-validated.
- **Retro never runs beside the pipeline.** `daily.py` calls it in-process after `digest`; the 12:00 task takes the
  same `.lock` non-blocking and exits if the daily run is still alive.
- **Retro cannot invent candidate facts.** Its prompt forbids `blitz_answers.md`, `resume_*`, `profile/`; escalations
  that need a human are reported as "For a human", never auto-answered.
- **PoW generation is doubly gated, and its limit is a dollar budget.** The daily stage runs only when `pow.enabled`
  *and* `pow.daily` are true (both default false), so a fresh config spends nothing; `pow.max_per_day` (default 1)
  caps omp passes per local day; `pow.agent_budget_usd` (default `$2.00`) is passed to `omp` as `--max-cost`, so the
  pass stops sending model requests when that spend is reached and print mode exits 3 — `pow.py` then validates
  whatever candidate the agent had written, and only a failed pass (any other nonzero code) is recorded as rejected;
  `state/pow/.lock` stops two passes at once. There is no wall-clock limit on the agent. Only applications with
  `status='submitted' AND confirmed=1` are eligible — nothing is inferred from a draft or a skipped stage.
- **PoW's `gtm_signal` rows come from a paid fetch, and Python owns the money.** `pow.py signals` is the only place
  `pow.py` calls a provider: `blitzapi.jobs.search` (BlitzAPI `POST /v2/jobs/search` via treg's proxy) at
  **$0.000495 per returned job row** (`max_results` 1-50, cursor paginated, an empty page is free). The token is the
  one `discover.py` uses (`~/.treg/config.json`) and is never printed. Per artifact, `pow.treg_budget_usd` (0.75) is
  enforced from the per-call ledger in `state/pow/signals/<token>.meta.json`; there is no call cap, so a fetch keeps
  paginating until the budget or the cursor runs out. Every call carries `Idempotency-Key` and
  `X-Treg-Route-Max-Cost` (what is left), and an exhausted budget stops the command with a message instead of another
  charge. One call of 50 rows ≈ $0.025. The spend lands on the artifact's `generated`
  event (`details.cost_usd`, `details.treg_calls`) — or on the `blocked` event when the result is an abstention.
- **PoW grounding is re-derived from the stored fetch, never from the model's memory — and Python authors the table.**
  A `gtm_signal` candidate only chooses: its `accounts.csv` is parsed leniently for the posting URLs it names, then
  every cell is rebuilt from this token's store — account and trigger from the fetched posting, `evidence` the URL,
  `detail` the computed open-role count, `posted` the fetched date inside the window, `ats` what the URL reveals —
  and written by the `csv` module, so a stray comma or a retyped date cannot reach the page. Row order is the
  candidate's (strongest signal first), one row per company, and the `acct-<n>` sources are regenerated from the store
  (an acct source no kept row refers to is pruned, and a finding citing one fails as an unknown source). An URL the
  fetch never returned is refused. Headline figures are
  placeholders (`{n_accounts}`, `{n_multi}`, `{window_days}`, `{freshest_days}`, `{n_recent_3d}`,
  `{n_ats_match}`, and for the method finding `{n_fetched}`, `{n_companies}`, `{titles}`) substituted by Python from
  the validated rows and the fetch ledger; a typed two-digit number in `title`/`thesis`/`email_line`/a claim is
  refused, as is a pack with no METHOD finding (the one claim that states the trigger, the funnel and the keywords
  that ran). Three more rules make the page worth opening rather than merely true. **Depth:** the prompt demands at
  least three differently-aimed searches and 12-20 kept accounts (8 is the floor, else abstain), and `signals` prints
  the pool and the spend after every call so the agent can see it grow. **The hook:** `title`/`thesis`/`email_line` may
  name kept accounts, and Python refuses a company-shaped name (carrying a designator such as `Logistics`, `Freight`,
  `Inc.`) that is not the employer, an integration partner or a kept row, plus any fetched company the pack dropped; a
  placeholder that resolves to 0 or 1 in the headline is refused ("1 accounts" is not a hook); `{n_ats_match}` only
  exists from two matching rows up, and the ATS is never the hook because the postings are mostly LinkedIn URLs.
  **Findings are positive:** a claim that states an absence ("none of the URLs reveals…", "no partner was found") is
  refused, and the prompt sends that material to `limitations`.
  Below 8 grounded accounts the candidate becomes an **abstention** with the count, and a `gtm_signal` candidate with
  no signal store is refused outright. A rejected candidate whose URLs and agent prose are sound can be re-run without
  a new agent pass: `pow.py generate --url U --company C --role R --recipe gtm_signal --candidate <file> --signals
  state/pow/signals/<token>.jsonl` still fails on unfetched URLs and invented numbers, but not on CSV formatting.
- **PoW execution never runs model-written code automatically.** `ai_eval` is scored by the built-in deterministic
  evaluator (`pow.py __eval`, our code, isolated child process, `pow.exec_timeout_seconds`) over a declarative JSON
  spec (three baselines: keyword_classify, regex_extract, exact_match). An artifact's own code runs only with an
  operator `--execute`, in `state/pow/tmp` with a minimal env and a timeout. There is no network isolation: read the
  artifact before you execute it.
- **PoW posting identity mirrors the host.** `lib/pow_store.url_key()` drops only the fragment, trailing slashes and
  `utm_*` params; other query params (`gh_jid`, `id`, `s`, `ref`) are kept in order and the path keeps its case — the
  pipeline's `S.norm_url` is *not* used for PoW artifacts, because it would fold two postings into one and make the
  attach guard disagree with the host. `dedupe_key` is the canonical UTF-8 JSON hash of the whole artifact
  (`artifact_id` included) and the host echoes it back; any difference is treated as a canonicalization bug.
- **PoW publishing and attaching are separate operator commands.** `pow.publish_enabled` / `pow.attach_enabled` are
  false by default; `daily.py` calls only `pow.py daily` (generate) and `pow.py reconcile --readonly`. Publish sends
  `{artifact, dedupe_key}` where the key is the host's content identity (canonical sorted-key JSON of the whole
  artifact, `artifact_id` included), so an identical re-post dedupes onto the same page; the returned `id` is the page owner and is what
  attach/GET/DELETE use. The host echoes that same hash; a missing or
  different value fails the publish (`rc 1`), leaves the row in its previous status so it cannot be attached, and
  reports the remote page as an uncertain external effect instead of deleting it. Attach refuses when no local `runs` row matches the artifact's posting.

## When it escalates to you

The digest lists anything the pipeline refused to submit: unsolved CAPTCHAs (image challenges, or a
token the form rejected twice), emailed verification codes, exact-salary demands, legal/export-control
wording, AI-attestation, mandatory cover letters, anonymous staffing reposts. Each has a
`state/evidence/<key>.json` with the fields already filled, so re-doing one by
hand is a copy-paste job.

PoW additions to the same list: `pow.py attach` failures that the host refuses (no draft, draft already sent, draft
carries attachments, posting URL mismatch) print the reason and leave the artifact `published`; append the line by
hand only if the draft is sound. `pow.py status` also shows any abstained artifact with the agent's reason — an
abstention is a decision, not a bug.

## Known gaps

- Token CAPTCHAs are solved by `lib/captcha.py` (also mounted as the `captcha-solver` MCP server).
  Image/slider challenges and reCAPTCHA Enterprise variants the solver rejects still land in the
  escalation list. As of 2026-11-02 only the solver call itself is smoke-tested (Google's public demo
  sitekey); token injection into a live ATS form has not been exercised yet.
- Workday candidate accounts are created per employer; passwords are recorded in
  the evidence JSON.
- BlitzAPI's `is_agency` flag is weak — the blocklist regex and the
  "3+ roles in one window" board heuristic do most of the filtering. For PoW signals neither runs: `signals` passes
  `company.is_agency: false` and narrows with `--icp-keywords`/`--sizes`, and the agent is told to drop staffing
  agencies, boards and the employer's own competitors by hand before writing `accounts.csv`.
- PoW signal postings are mostly LinkedIn URLs, so `ats` is often empty and `{n_ats_match}` is usually too small to
  sell. The placeholder therefore only exists from two matching rows up (one match is refused as unavailable), the
  prompt tells the agent not to mention ATS in the hook at all otherwise, and the `ats` column stays only as a table
  column filled from the posting URL's host. A zero is honest, not a bug — it is just not the hook.
- PoW publish/attach/revoke/reconcile need the private TraceHire worker and its key file; generation, validation and
  the built-in evaluator are offline, and `pow.py smoke` covers the whole flow (including a fixture `treg` response)
  against a local stub.
- **Cross-language parity fixtures live in `archive/pow-fixtures-2026-11-04/`** (untracked): three accepted artifacts
  plus `manifest.json` with bytes, canonical sha256, local and host `url_key`. They were validated against the host
  validator offline (3/3) and are the post-deploy baseline: publish `artifact.json` and compare the returned
  sha256/bytes to the manifest. Regenerate into a scratch state with
  `JOBHUNT_STATE=<dir> python pow.py generate --url U --company C --role R --recipe R --candidate <file>`
  (the candidate inputs are the `_*_candidate()` fixtures inside `pow.py`; the `gtm_signal` one needs the fixture
  signal store, which `pow.py smoke` writes — regenerate it there or pass `--signals`). The archived `gtm_signal`
  artifacts were baselined before the v2 account-list change, so their regeneration now differs by design:
  re-baseline instead of investigating.
  **Two checks, two questions — keep them separate.** (1) *Post-deploy, host:* publish the archived `artifact.json`
  (input bytes pinned) and compare the returned sha256/bytes/`url_key` to the manifest — any mismatch is the deployed
  build, because the input hash cannot have changed. (2) *Generator:* regenerate from the `_*_candidate()` fixtures and
  compare to the manifest — any mismatch means `pow.py`/prompts/fixtures changed, so re-baseline instead of
  investigating the host. Regeneration recovers the *reference values*; it is not a substitute for the pinned *input
  file*, and merging the two checks makes a drift unattributable.
  Caveat: this repo lives under `C:/tmp`, so the archive is a convenience copy, not durable storage — a broad `C:/tmp`
  wipe or an image reset takes it. Copy it outside `C:/tmp` (e.g. the job-harness Drive folder) if it must survive the
  machine.
- The ai_eval baselines are deterministic smoke-level tools (keyword/regex/normalization), not model evaluations; the
  artifact says so in `limitations`, and no provider is ever called by `pow.py`.

## State history

- 2026-11-05 (v2.2): the `gtm_signal` recipe was made to produce something a GTM lead would click, after the first
  live pack (8 accounts from one treg call) was valid but weak. The prompt now demands at least three
  differently-aimed searches (title variants, a 7-day and a 21-day window, ICP filters), targets 12-20 kept accounts
  with 8 as the floor, and gives a deterministic order (freshest first, then recognizable/large operators, then
  multi-role companies — Python keeps the candidate's order). `pow.py signals` prints the pool (postings and distinct
  companies) and the spend after every call. New computed placeholders `{freshest_days}` and `{n_recent_3d}` let a
  headline state recency truthfully; a placeholder that resolves to 0 or 1 in `title`/`thesis`/`email_line` is
  refused; `{n_ats_match}` now exists only from two matching rows up; a finding that states an absence is refused;
  and a company-shaped name in `title`/`thesis` must be a kept row (Python also refuses a fetched company the pack
  dropped). `prompts/pow-gtm_signal.md` gained a third "Python will not take on trust" rule in `prompts/pow.md`. No
  table or column changed.
- 2026-11-05 (v2.1): every job-level PoW limit is a dollar budget. `pow.agent_timeout_seconds` is gone: one omp pass
  is capped by `omp --max-cost <pow.agent_budget_usd>` (default $2.00), whose print-mode exit 3 means "budget reached",
  and `pow.py` then validates whatever candidate the agent had written. `pow.treg_max_calls` is gone as well:
  `pow.treg_budget_usd` is the only bound on the signal fetch (the per-call row clamp, `X-Treg-Route-Max-Cost`, the
  idempotency key and the spend ledger all stay). `pow.exec_timeout_seconds` and the HTTP socket timeout stay on
  purpose, because a local child process costs no money and a dead connection must not hang the run.
  `gtm_signal`'s `accounts.csv` and its `acct-<n>` sources are now authored by `pow.py` from the fetched store — the
  candidate supplies the posting URLs and nothing else — so a quoting bug, a hand-typed date or a retyped excerpt can
  no longer reject an otherwise sound pack. `state/pow/task.json` publishes `agent_budget_usd` in place of
  `treg_max_calls`. No table or column changed.
- 2026-11-04 (v2): `gtm_signal` became an account-list artifact. New `state/pow/signals/` (fetched postings + spend
  ledger) and `state/pow/task.json` (adds `signals_token`, `signals_path`, the fetch caps). New `pow.py signals`
  command and `pow.treg_budget_usd` / `pow.treg_max_calls` / `pow.signal_window_days` config keys (all defaulted in
  code, so an existing `config.json` keeps working). No table or column changed: the fetch cost rides on the existing
  `pow_events.details_json`. Artifacts generated before this date keep their stored bytes; a new `gtm_signal` candidate
  needs a signal store to validate.
- 2026-11-04: two additive tables, `pow_artifacts` and `pow_events` (`lib/pow_store.py` is the only DDL owner;
  `pow_telemetry` imports `ensure_schema`), plus `state/pow/` for artifacts, candidates, refused candidates and
  exec scratch. No existing table or column changed. `retro.py` now refuses to edit any `pow*` file, and no PoW
  metric enters the retro KPI dict: attribution over a handful of sends must never become a self-optimisation target.
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
- 2026-10-29: moved from WSL to native Windows. Under WSL the apply agent's `gws` resolved
  to an unauthenticated Linux shim, so Greenhouse security codes could not be read and two
  applications stalled at `needs_code`. `daily.sh` became
  `daily.py`; the treg CLI call became a direct HTTP call; every stored `/mnt/c/` path in
  `state.db` (59 `postings.jd_file` rows) and the state JSON sidecars was rewritten to
  `C:/` by a one-shot script, since deleted.
