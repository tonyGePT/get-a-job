# Recipe: fde_integration

Supported scope: forward-deployed, solutions, implementation, integration, deployment and partner
engineering roles where the useful artifact is a **runnable reproduction against the company's own
public documentation**: the pagination loop, the webhook verifier, the auth flow, the data-model
mapping — written so an engineer can read it and run it.

Not supported: anything that needs a credential, a sandbox account, a live endpoint or data you do
not have. Never invent a working integration with a private API.

## What "useful" means here (minimums the validator enforces)

- at least **2 public source URLs** (the documentation the code is built against);
- at least **1 file** in `deliverable.files`, one of which must be a runnable harness:
  `run.py`, `main.py`, `harness.py` or `*_run.py` (Python; the host has CPython);
- `deliverable.results` left as `[]`: the recipe needs an explicit execution status, and
  `pow.py` adds `exec:pending` with `not_run` at generation time. Only an operator running
  `python pow.py run <artifact_id> --execute` turns that into real `exec:` evidence.

That means: your artifact is accepted **without** any claim that the code works. Do not claim it
works. Write code that would work, and say exactly what you did not verify.

## The harness contract

- The harness runs from a fresh scratch copy of your file tree, with a minimal environment (no
  credentials, no repository, no network guarantees) and a bounded timeout (60 s by default).
- It must not require arguments, environment variables, network access, or installed packages
  beyond the Python standard library. If the reproduction needs a live call, the harness must
  exercise an **in-memory fixture that mirrors the documented shape**, and `limitations` must say
  that no live call was made.
- Print a short line or JSON summary; exit 0 on success, non-zero on failure. Output is truncated
  into the result row, so keep it under a screen.
- No `pip install`, no `requests`, no subprocesses of its own, never any credential, never a
  `while True` loop.

## What to cite

- the reference page that defines the endpoint, parameter, pagination cursor, auth header, rate
  limit or event payload you implement;
- the page that defines the data model you map between, if the integration maps fields;
- one source per non-obvious behaviour. Two strong pages beat six weak ones.

Excerpts must be the sentences that justify the code. If the docs are ambiguous, that ambiguity
belongs in `findings[].limitations` — it is a real finding, and it is worth more than a guess.

## Shape of a good artifact

1. `thesis`: the integration risk you are removing, in one paragraph.
2. `findings`: two to four, each tied to a doc line ("records are cursor paginated; the client
   must follow next_cursor"), with what is not covered.
3. `deliverable.files`: the harness plus a short `NOTES.md` mapping each behaviour to the doc
   sentence it implements, plus any fixture JSON it reads.
4. `deliverable.summary`: what runs, against what, and what is deliberately unverified.
5. `limitations`: "runs against an in-memory fixture, not their live API", "no credential
   available, so authenticated behaviour is unverified", "rate-limit backoff documented but not
   implemented".

## Honest abstention

Abstain when the company publishes no API or integration documentation you can cite, when the role
is really a sales or support role, or when the documented flow needs credentials that do not exist
here. Say which one.

## Do not

- Do not paste a token, cookie, hostname with credentials, or an internal URL into any file.
- Do not describe the code as "working", "tested" or "deployed". Status is `not_run` until an
  operator runs it.
- Do not include the job description's requirements list as a checklist for the artifact.
- Do not fetch their live API from inside a file you declare: the harness runs without network
  guarantees and must not depend on it.