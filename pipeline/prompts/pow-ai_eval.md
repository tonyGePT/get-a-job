# Recipe: ai_eval

Supported scope: applied AI, AI product/engineering, evals, LLM and ML roles where the useful
artifact is a **small, runnable eval**: a labelled case set, a stated baseline and a measured
score. The artifact measures a baseline you can actually run here, not their unreleased product.

Not supported: any claim about the company's model quality, any number produced by a provider you
did not call, and any evaluation that needs their data.

## What "useful" means here (minimums the validator enforces)

- at least **1 file** in `deliverable.files`, including one **eval spec**: a `.json` file with at
  least `baseline` and `cases`. Exactly one spec per artifact.
- at least one `eval:` result with status `passed`. `pow.py` produces it: it runs the built-in
  deterministic evaluator on your spec in an isolated child process with a bounded timeout.
- `deliverable.results` left as `[]`. Everything the model writes there is discarded, because the
  model cannot certify that anything ran.

If the declared claim is not met, the result is recorded as `failed` and the artifact is refused:
revise the claim or the cases, or abstain.

## The eval spec (exact format)

```json
{
  "baseline": "keyword_classify",
  "config": {"labels": {"integration": ["api", "webhook", "connector"], "pricing": ["pricing", "tier", "seat"]}},
  "cases": [
    {"id": "c1", "input": "does the api expose a webhook per event?", "expected": "integration"},
    {"id": "c2", "input": "what does the team tier cost per seat?", "expected": "pricing"},
    {"id": "c3", "input": "can a connector retry a failed sync?", "expected": "integration"}
  ],
  "claim": {"metric": "accuracy", "min": 0.8}
}
```

Rules:

- `baseline` is one of `keyword_classify`, `regex_extract`, `exact_match`. All three are built into
  `pow.py`; none of them calls a model, a network or an installed package.
  - `keyword_classify`: `config.labels` maps 2..12 labels to 1..20 keyword strings. The baseline
    picks the label with the most keyword hits (no hits → `unknown`); the prediction must equal
    `expected` case-insensitively.
  - `regex_extract`: `config.pattern` (≤200 chars, must compile, no nested quantifiers) and
    `config.group` (0..9). The extracted group must equal `expected`.
  - `exact_match`: `config.normalize` is `lower_trim` or `lower_alnum`. The normalized input must
    equal the raw `expected`.
- `cases` is 3..50 labelled cases with `input` (≤4000 chars) and `expected` (≤200 chars), at least
  120 characters of input in total, ids unique if you use them.
- `claim` is optional. With `claim`, the result is `passed` only when accuracy ≥ `claim.min`;
  otherwise the run is recorded as `failed` and the artifact is refused. Without a claim the score
  is reported and the run passes if it executed.
- Numbers in `observed` are produced by the evaluator. You never write a score.

## What to cite

- the page that defines the task or the labels (help-centre structure, taxonomy, API error codes,
  product categories) — one or two sources is normal;
- if the labels are your own construction, say so in `findings[].limitations` and in the artifact's
  `limitations`. A hand-labelled set of four cases is a smoke-level eval; label it as such.

## Shape of a good artifact

1. `thesis`: what decision the eval informs, and why a deterministic baseline is the right first
   measurement.
2. `findings`: two or three, each tied to a source ("the help centre already groups questions
   into these two topics, so the label scheme is theirs, not invented").
3. `deliverable.files`: `eval_spec.json` plus `HARNESS.md` (what the baseline is, how the score is
   computed, what would need a model or their data).
4. `limitations`: case count, label provenance, and that no model/provider was called.

## Honest abstention

Abstain when the role is not really about AI/evals, when there is no public definition of the task
to label against, or when an honest eval would need their data or a provider call. Say which.

## Do not

- Do not claim a provider score, a benchmark ranking, or a comparison with their unreleased system.
- Do not write a score into the artifact; the evaluator's output is the only score.
- Do not smuggle credentials into the spec or ask the harness to call anything.
- Do not use the job description as the case set: the cases are about the task, not the posting.