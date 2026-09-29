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

## The results — single run, and why they had to be repeated

| arm | corrected gold | rescued | broke | net | p | tokens |
|---|---|---|---|---|---|---|
| baseline | 72.3% | — | — | — | — | 1.45M |
| noise floor (no change) | 72.3% | 11 | 11 | 0 | 1.000 | 1.45M |
| column documentation | 73.3% | 19 | 15 | +4 | 0.608 | 1.98M |
| literal rebinding | 72.8% | 10 | 8 | +2 | 0.815 | 1.45M |
| few-shot exemplars | 66.5% | 18 | 42 | −24 | 0.0027 | 1.63M |
| docs + rebinding | 73.7% | 19 | 13 | +6 | 0.377 | 2.00M |

On this evidence I wrote that nothing cleared the noise floor. **That was
wrong about column documentation, and a multi-seed sweep is what found it.**

## The multi-seed sweep

Eleven more runs of the full 500 on the Flex tier, $1.31. Five baselines,
three column-docs, three exemplars. `benchmarks/analysis/sweepstats.py`,
output in `sweep-results.txt`.

| arm | usable runs | accuracy per run | mean | sd |
|---|---|---|---|---|
| baseline | 5 | 71.6 71.8 73.3 72.8 72.8 | **72.4%** | 0.71 |
| column-docs | 3 | 73.3 75.4 74.7 | **74.5%** | 1.10 |
| exemplars | 2 | 67.2 66.0 | **66.6%** | 0.85 |

The null, from all 10 baseline-vs-baseline pairs:

* discordant pairs: mean **16.4**, range 10–19
* net: `[-2, -2, 0, 1, 4, 4, 5, 5, 6, 7]` — **largest |net| = 7**

And each arm against all five baselines:

| arm | mean net | sd | range | median p | p < 0.05 |
|---|---|---|---|---|---|
| **column-docs** | **+8.4** | 4.7 | 0 .. +16 | 0.185 | 4/15 |
| **exemplars** | **−24.1** | 3.8 | −30 .. −18 | 0.0027 | **10/10** |

### Column documentation is probably real

+2.1pp, mean net +8.4, and **never negative in 15 comparisons** against a null
whose largest excursion is 7. The single run that produced +4 was a low draw
from this distribution, and calling it noise was a mistake of exactly the kind
this sweep existed to catch — it caught it in my own conclusion.

It is still not *settled*: median p is 0.185 and only 4 of 15 comparisons
clear 0.05 individually. The arm means differ by 2.1pp with n=5 and n=3.
Suggestive, not proven, and the 37% token cost is real.

### Exemplars are confirmed harmful

−5.8pp, mean net −24.1, **significant in 10 of 10 comparisons.** The
single-run −24 reproduced almost exactly. This one is settled.

### One run was an outage, not a measurement

`sweep-exem-2` produced no SQL for **339 of 500** questions: 338 `Mantle call
failed after 4 attempts` errors — connection resets, SSL EOF, DNS failure —
over 3,498 seconds against roughly 400 for its siblings.

Averaged in, it dragged that arm from 66.6% to 50.9% and turned a real
−5.8pp effect into a meaningless −89 net. `sweepstats.py` now refuses any run
where more than 5% of questions produce no SQL and prints the reason. A failed
network is not a result, and a sweep that silently averages one is worse than
no sweep.

### A wrinkle worth recording

The baseline-vs-baseline nets have mean **+2.8**, not 0, and the five baseline
runs rise through the hour (71.6, 71.8, 73.3, 72.8, 72.8). With n=5 that may
be chance, but it may be drift in the serving stack — and every non-baseline
arm ran *after* all five baselines, which would flatter them. Future sweeps
should interleave arms rather than block them.

## The complete matrix — 24 runs, 7 arms

`benchmarks/analysis/sweepstats.py`, output in `sweep-results.txt`. Corrected
gold, 415 questions scorable in every run.

| arm | runs | mean | sd | mean net vs baseline | median p | p<0.05 |
|---|---|---|---|---|---|---|
| baseline | 8 | **72.4%** | 0.56 | — | — | — |
| exemplars | 2 | 66.6% | 0.85 | **−23.9** | 0.0027 | **16/16** |
| rebind | 2 | 74.0% | 0.34 | +6.6 | 0.173 | 4/16 |
| column-docs | 3 | 74.5% | 1.10 | +8.6 | 0.143 | 7/24 |
| docs+rebind | 2 | 74.5% | 0.00 | +8.6 | 0.144 | 2/16 |
| intent | 3 | 74.5% | 0.77 | +9.0 | 0.108 | 6/24 |
| **intent + docs** | 3 | **76.2%** | 0.85 | **+16.0** | **0.0151** | **19/24** |

The null, from all 28 baseline-vs-baseline pairs: discordant mean 14.8, net
mean **+0.6**, sd 3.3, **largest |net| 7**. Interleaving fixed the +2.8 offset
sweep 1 showed — the pooled null is centred on zero, which is what a null
should look like.

### Intent and column documentation compose

**+16.0 mean net, all 24 comparisons positive, range +9 to +23, 19 of 24
individually significant.** More than double the null's largest excursion.

They add almost exactly: intent alone +9.0, docs alone +8.6, together +16.0.
That is what independent mechanisms look like, and it is the one prediction
made in advance here that the data then confirmed — intent catches wrong rows
after execution, documentation prevents wrong columns before generation.

**72.4% → 76.2%, +3.8pp**, for one extra model call.

### What I got wrong, twice, on partial data

Mid-sweep I wrote that intent+docs "is not stacking" and was "below intent
alone". That was read off **original gold** — the noisy target the whole
`rescore.py` exercise exists to avoid. On corrected gold it is the best arm by
1.7pp.

Earlier I called column documentation "inside the noise" on a single +4 run.
It is +8.6 across 24 comparisons.

Both errors have the same shape: a conclusion drawn from one run, or from the
wrong gold, stated without the hedge the evidence deserved.

### The latency claim was a bug, not a measurement

| run | wall clock |
|---|---|
| `full500-intent`, before the fix | **3,760s** |
| sweep2-intent-1/2/3, after | 430s, 428s, 414s |

Every `--intent` latency figure in this repository before commit `9b4ad99`
measured a cartesian-product probe in `guards/evidence.py`, not the feature.
The check costs roughly **20% more wall clock** than baseline, not 7.6x.
Accuracy is unaffected — the probe only ever fed a prompt.

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
