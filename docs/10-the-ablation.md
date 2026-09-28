# 10. Four changes, measured

Executed 2026-09-29. Every arm is the full 500 BIRD mini-dev questions,
`google.gemma-4-31b`, no glossary, scored against `arcwise_plat_sql.json`
(pinned in `benchmarks/CORRECTED_GOLD.md`), paired per question with an exact
McNemar test. One change per run.

## The noise floor, first

Two runs of the **identical** configuration, nothing changed:

| | |
|---|---|
| run 1 | 300/415 = **72.3%** |
| run 2 | 300/415 = **72.3%** |
| rescued / broke | **11 / 11** |
| net | **0**, p = 1.000 |

The same score, reached by flipping 22 questions. That is what doing nothing
looks like on this harness, and it is the bar every result below has to clear.

## The results

| arm | corrected gold | rescued | broke | net | p | tokens |
|---|---|---|---|---|---|---|
| baseline | 72.3% | — | — | — | — | 1.45M |
| **noise floor** (no change) | 72.3% | 11 | 11 | 0 | 1.000 | 1.45M |
| column documentation | 73.3% | 19 | 15 | **+4** | 0.608 | 1.98M |
| literal rebinding | 72.8% | 10 | 8 | **+2** | 0.815 | 1.45M |
| **few-shot exemplars** | **66.5%** | 18 | 42 | **−24** | **0.0027** | 1.63M |
| docs + rebinding together | **73.7%** | 19 | 13 | **+6** | 0.377 | 2.00M |

**Nothing cleared the noise floor. One change was significantly harmful.**

The best configuration found is 73.7% — the two positive changes together,
+6 questions over baseline at p = 0.377 and 38% more tokens. It is the
highest number this pipeline has produced and it is still inside ±11.

## Arm by arm

### Column documentation — +4, inside the noise, 37% more tokens

BIRD ships a written description for 77% of its columns and this pipeline used
none of them. Both systems above us on the corrected leaderboard feed exactly
this, and OpenSearch-SQL's ablation on these same 500 questions puts schema
extraction at −4.2.

Wiring it in grew the rendered schema for `california_schools` from 5,676 to
13,873 characters and the run from 1.45M to 1.98M tokens. It bought +4
questions, which is inside a floor of ±11.

Kept in the codebase, default off. The mechanism is right and general —
`COMMENT ON COLUMN` is where a production warehouse already keeps this, and a
schema whose columns are named `EdOpsCode` benefits more than BIRD's do. It
did not earn a default here.

### Literal rebinding — +2, and it fired once

`WHERE category = 'Cravings Deals'` against a column storing
`'Cravings Deals ⭐'`. Deterministic: the database says the literal is absent,
the database proposes the replacement, the rewrite is kept only if it turns an
empty result into a non-empty one. No model call.

**It fired on 1 question in 500** and got that one right:

```
posts.Title: 'Computer Game Datasets' -> 'Computer game datasets'
```

The surface was already known to be small — 41.6% of gold queries contain no
string literal at all and 54.8% have literals the model can copy verbatim out
of the question — but one firing is smaller than the 18-question addressable
set predicted, because only 16 of 500 queries returned empty in the first
place and only one of those had a rebindable literal.

Kept, default off, and worth more in production than here: BIRD questions
quote their literals, real users do not, and production has no `evidence`
hint doing a tenth of the value-linking for free.

### Few-shot exemplars — −24, p = 0.0027, and the only significant result

The largest published lever: OpenSearch-SQL measures −6.2 at generation for
removing dynamic few-shot, more than any other component. We had none.

Built carefully. Examples came from **BIRD-Verified** (ReViSQL's
expert-corrected release, 2,064 pairs) rather than BIRD's own train split,
precisely because that split is 52.8% mis-annotated and ReViSQL measured
training on it scoring 7 points *below* not training at all. Checked for
contamination first: zero overlapping questions, zero overlapping gold SQL,
69 training databases disjoint from the 11 evaluation ones. BM25 retrieval,
top 3, a block that says the tables are from other databases and must not be
used.

It lost 42 questions and rescued 18.

**It is not schema leakage.** Of the 51 questions it broke on original gold,
**zero** produced SQL naming a table absent from that database. The model did
not copy the exemplars' tables; it copied their *shape* onto questions that
needed a different one.

CHASE-SQL predicted this and we did not weigh it heavily enough. Their Table 9
isolates example quality: retrieved real training examples get **worse** as
you add more (58.80 at n=5 → 56.91 at n=125), while instance-aware
*synthesised* examples improve to ~75 and beat retrieval by +5.6. Every system
reporting a large few-shot gain either synthesises examples per question
(CHASE-SQL's OS generator) or self-generates chain-of-thought for them
(OpenSearch-SQL's Query-CoT-SQL). Nobody reports a large gain from retrieved
question–SQL pairs alone, which is what we built.

**Deleted from the default path, kept behind a flag with this result written
next to it**, so the next person who reads "few-shot is the biggest lever"
finds the measurement before they spend a day on it.

## What this says

Three changes drawn from the best-evidenced parts of the literature, built
faithfully, measured honestly: **+4, +2, and −24.** The two positives are
inside a ±11 noise floor and the negative is four times outside it.

The honest reading is not that the literature is wrong. It is that the
published deltas were measured against weaker baselines — OpenSearch-SQL's
few-shot gain is over a 59.6% zero-shot generator, CHASE-SQL's query fixer is
worth +3.83 on a 57.75 baseline and +0.93 on a 67.09 one. Our 72.3% single-call
pipeline is above every baseline these components were measured against, and
the components shrink toward zero exactly as those papers' own numbers predict
they should.

ReViSQL reached the same conclusion from the other end and deleted their
inference pipeline between paper versions: 86% of their headline is training
on verified data, and only 1.60 points is anything available at inference
time.

**What would actually move this number is a stronger base model or fine-tuning
on verified data. Not more pipeline.**

## What was spent, and what is left on

Six runs of 500 questions, **$1.55** total, about 50 minutes of wall clock.

Everything stays **default off**. Nothing earned a default, which is the rule
this repository has followed since the critic: a feature ships behind a
toggle, gets A/B'd on the same questions, and keeps its measurement next to
it whether the measurement flatters it or not.

`--column-docs` and `--rebind` are kept because both are mechanically right
and both are worth more off this benchmark than on it — BIRD questions quote
their literals verbatim and BIRD's `evidence` hint already does a tenth of
the value-linking that a real deployment has to do for itself.
`--exemplars` is kept only so the next person to read "few-shot is the
largest lever" finds this number before spending a day on it.
