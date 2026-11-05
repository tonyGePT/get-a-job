# Repair pass: fix one candidate file until the validator passes it

You are repairing **one candidate JSON file** that `pow.py` (Python, deterministic) refused. The
file is named at the bottom of this brief, together with the reasons it was refused. You have a
**dollar budget**, not a step budget: keep going until the validator passes the file or the budget
runs out. There is no retry limit on your side; `pow.py` decides the outcome afterwards with its own
validation, so the only thing that counts is what the file says.

## The loop

1. **Fix** the file in place (see the rules below).
2. **Check it yourself**: run

   ```
   python pow.py check
   ```

   It is free, offline, and reads the candidate path, posting, recipe and signal store from the
   brief, so it needs no arguments. It prints `valid` (exit 0) or a numbered list of every problem
   (exit 1) — all of them, not just the first.
3. **Read every error and fix every one**, then run `python pow.py check` again. Repeat until it
   prints `valid`, or until you are out of budget. Do not stop at one fix: a syntax error masks the
   content errors behind it, and the validator only reports what it can get to.

When you are done, print the last `python pow.py check` output verbatim as your final message, so
the log shows exactly what state you left the file in.

## The rules of this pass

1. **Edit that file in place.** It is the only file you may touch: no new files, no renames, no
   `git`, no edits under `state/pow/`, nothing else in the repository.
2. **Only change what an error requires.** Every other key, source, row and sentence stays as it is.
   A repair that rewrites a finding nobody complained about is worse than the failure it fixes.
3. **No new data.** Do **not** run `pow.py signals`, `pow.py generate`, `pow.py publish`, `treg`,
   `curl`, `wget`, a search, or any other network call. `pow.py signals` refuses while a repair
   pass runs, and the account evidence you need is already in the signal store named in the brief.
4. **Do not add or remove accounts**, and never invent a posting, a company or a date. If a row
   cannot be supported, that is not something this pass can fix by editing the table.
5. **Numbers only via placeholders.** If a claim, the title, the lede or the email line needs a
   figure, use a placeholder (`{n_accounts}`, `{n_multi}`, `{n_roles_total}`, `{max_roles}`,
   `{n_recent_3d}`, `{n_recent_7d}`, `{freshest_days}`, `{window_days}`) — never a digit and never a
   spelled-out number. If no placeholder expresses the claim, rewrite the claim without it.
6. **When an error says a claim is untrue or unexpressible, rewrite the claim so it is true.**
   A per-account count ("Maersk posted four openings") cannot be expressed at all — the table's
   `detail` column carries it. Say something the rows do support instead: the role that fired, the
   recency (`{n_recent_3d}`), the scale (`{n_roles_total}`, `{max_roles}`), the mix of roles, or the
   named accounts themselves. Keep claim #1 as the most valuable takeaway and keep every claim
   within the limits the errors quote.

## If the file is not valid JSON

The brief says so, with the parser's message and the end of the file. Repair the syntax first (a
missing or extra bracket, a stray comma, an unquoted value), keeping every value that is already
there — then run `python pow.py check`, because the content errors were invisible until the file
parsed.

## What you may not do

- Run anything that spends money or fetches: `pow.py signals`, `treg`, `curl`, `wget`, searches.
- Run `pow.py generate`, `publish`, `attach` or `revoke`.
- Edit any file other than the candidate named below.
- Claim success in your final message unless `python pow.py check` printed `valid`.