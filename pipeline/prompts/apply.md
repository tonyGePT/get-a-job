orchestrate

# Daily apply pass

Submit the applications queued in `state/apply-queue.json`. Nothing else.

## Inputs
- `state/apply-queue.json` — `{key, company, title, apply_url, ats, resume_pdf, variant}`. Each key already has a tailored resume at 100% keyword coverage.
- `state/ats-hints.json` — optional, grown by the post-run retro from repeated form friction: per-ATS selector/behaviour facts (`greenhouse`, `ashby`, `lever`, `workday`, …). Read the entry for each ATS in the queue before driving that form; it is guidance about the form, never about the candidate.
- `C:/tmp/jobharness/pipeline/state/jd/<key>.txt` — the job description, for role-specific free text.
- `C:/tmp/jobharness/blitz_answers.md` — the only source of facts you may type (name, email, phone, address, work authorization, salary range, current-company wording, standing background-check consent, EEO answers).

## Method
- This is the prepared queue from today's qualify stage. Do NOT run `ledger.py check`, do NOT use a duplicate-safeguard skill, and do NOT treat today's own `postings` / `verdict=APPLY` row as a prior application. The queue has already been deduped by the pipeline. Only skip for a real prior application (e.g. a confirmed `submitted` row in `state.db` / history) for the same requisition; if uncertain, inspect that prior status and compare ATS requisition URL, not just company/title.
- `workpool("task", name="apply")`, one item per queue entry, all submitted in ONE call (omp caps parallel workers at 8). Each worker must use its own named tab `apply-<key>` via `browser.open(name="apply-<key>", ...)`; never share or reuse another worker's tab. Applications run in parallel, each with independent browser state.
- Open with `browser.open(name="apply-<key>", url=<apply_url>, app={"relay": False, "tern": False})` (managed Chromium; the relay is not connected on this box).
- Filling: `tab.fill` / `tab.type` for inputs, `tab.uploadFile(sel, resume_pdf)` for the hidden resume input, `tab.press("Escape", {"selector": sel})` to dismiss react-select menus, `tab.evaluate("<IIFE string>")` for anything multi-statement. `tab.evaluate` needs an IIFE, not a bare function.
- Retry a rejected field ONCE after reading the red error text; never guess what the validator wanted.
- For dropdown/combobox fields (including Country / Location), select an actual option from the menu or press Enter/Tab to commit the option, then verify the selected value and validation state; merely typing text may not commit a selection.
- Emailed security / verification codes (Greenhouse's 8-character "security code", Ashby/Lever/Workday email codes): you have the candidate's Gmail through the `gws` CLI. When the code field appears, find the newest message from the ATS that arrived AFTER you clicked submit, e.g. `gws gmail users messages list --params '{"userId":"me","q":"newer_than:1h subject:(security code) <Company>","maxResults":3}'` then `gws gmail users messages get --params '{"userId":"me","id":"<id>","format":"full"}'` (the code is in `snippet`). Poll every ~15 s for up to 3 minutes. Type it into the code field in the SAME tab and submit again. Never reload the page or open a new tab first: a fresh submit invalidates the code and mails a new one.

## Truthfulness
- Type only what `blitz_answers.md`, the resume, or the JD supports. No invented metrics, employers, dates, tools, or titles.
- Free text: 2–5 first-person sentences built from real resume facts (see `C:/tmp/jobharness/profile/apply_rules.md`).
- Optional unknown fields stay blank.
- EEO/demographics: "I do not wish to answer" / "Decline to self-identify".

## Escalate instead of submitting
A required single exact salary number · a legal/export-control representation not already answered in the answers file · a mandatory cover letter · a paid assessment or take-home · an attestation that materials were not AI-assisted · a CAPTCHA you cannot solve (see "CAPTCHAs") · an emailed code that did not arrive within 3 minutes (status `needs_code`) · any required question whose truthful answer is not in the answers file.
Standing answers, consents and skip rules (including what to do when the apply link forces a third-party sign-in): read `C:/tmp/jobharness/profile/apply_rules.md` in full before starting and follow it exactly. Do not escalate anything that file already answers.

## CAPTCHAs
A token CAPTCHA (reCAPTCHA v2/v3, hCaptcha, Cloudflare Turnstile, CaptchaFox) is solved, not escalated. Image/slider/"select the squares" challenges the solver does not cover, and anything that still fails after the steps below, are escalated.
1. Find the sitekey in the SAME tab (`tab.evaluate`). The `data-sitekey` attribute is usually on a `.g-recaptcha`, `.h-captcha` or `.cf-turnstile` element; otherwise read it from the challenge iframe `src` (reCAPTCHA `k=`, hCaptcha `sitekey=`, Turnstile the `0x4…` path segment). For reCAPTCHA v3 also find the `action` passed to `grecaptcha.execute`.
2. Solve it (prints only the token; at most once per page load): `C:/Python313/python.exe C:/tmp/jobharness/pipeline/lib/captcha.py solve --method <userrecaptcha|recaptchaV3|hcaptcha|turnstile|captchafox> --sitekey <key> --url <exact page URL> --key <evidence key>`. A normal solve takes 10–60 s, but when the provider has no capacity the script itself retries with exponential backoff (5 s doubling, never more than 3 min between attempts, about 8 minutes in total), so run it with a bash timeout of at least 900 s and do not interrupt it. Exit 3 = daily cap reached, exit 4 = provider unavailable after all retries: both → escalate immediately and do NOT call the script again for that key. Any other non-zero exit → escalate. The same backend is also mounted as the `captcha-solver` MCP server (`Create_Request` / `Get_Response`) if those tools are in your tool list; prefer the script because it retries and enforces the cap.
3. Inject the token immediately (it expires in about 2 minutes) and submit in the same tab: set `textarea[name="g-recaptcha-response"]`, `textarea[name="h-captcha-response"]` or `input[name="cf-turnstile-response"]` to the token and dispatch `input` and `change` events; if the widget has a `data-callback`, also call `window[<name>](token)`. Then click submit.
4. If the form rejects the token or the CAPTCHA reappears, you may re-solve ONCE. After that, write the escalation below. Solving does not change the proof rule: only a confirmation URL/text counts.


## Proof (a click is not proof)
Accept only: a confirmation URL, or explicit confirmation text ("successfully submitted", "received your application", "thank you for applying", "application submitted"). If the form clears with no confirmation, the submission did NOT happen — report `failed` with the validation error you saw.

## Deliverables, per key
- `state/evidence/<key>.png` — full-page screenshot of the confirmation or the blocking step (>5 KB).
- `state/evidence/<key>.json`:
```json
{"key":"","company":"","role":"","status":"submitted|escalated|needs_code|failed",
 "final_url":"","confirmation_text":"","blocking_question":"",
 "fields_filled":{"field":"value"},"blank_optional":[],"note":"<=40 words"}
```
- **CAPTCHA you could not solve:** write the evidence JSON/PNG for that key (status `escalated`, `blocking_question` starting with "CAPTCHA" and naming the type and what failed, e.g. "CAPTCHA: Turnstile token rejected twice"), then run `C:/Python313/python.exe C:/tmp/jobharness/pipeline/lib/notify.py captcha --key <key>` so the user gets a Discord alert. Then move on; the command is safe to repeat. Do NOT alert for CAPTCHAs you solved.
- Never create Gmail drafts, never send email, never write to `state/state.db` or the `history` ledger — later steps do that from your evidence files.
- If the Workday/Teamtailor flow creates a candidate account, record the generated password in the JSON as `account_password`.

## Report
Print one line per key: `<key> <status> <final_url>`.
