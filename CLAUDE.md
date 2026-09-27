# Working rules for this repo

## Ask before spending tokens

**Never launch a benchmark, sweep or any multi-question model run without
asking first.** State the cost estimate and wait for a yes.

This is not a style preference. On 2026-09-27 eight runs went out in one
session without a single one being agreed to first:

| run | tokens |
|---|---|
| tier-thorough | 1,989,741 |
| tier-medium | 1,189,327 |
| tier-fast | 454,448 |
| bfs-hops-2 | 455,984 |
| model-qwen3-coder-480b | 434,502 |
| bfs-hops-1 | 420,712 |
| model-deepseek.v3.2 | 406,473 |
| model-zai.glm-5 | 406,091 |
| **total** | **5,757,278** |

Some of that was wasted on my own errors — the first 150-question run used the
wrong glossary and had to be redone, and the tier sweep silently skipped
`medium` because `--tier` did not list it.

### What needs asking

- any `benchmarks/bird.py` run, at any `--limit`
- any `benchmarks/ambiguity.py` run
- any sweep or loop that calls a model more than a handful of times
- an A/B, which is two runs and should be priced as two

### What does not

- `benchmarks/rescore.py` — re-executes stored SQL, **zero model calls**
- unit tests, lint, typecheck
- one or two questions through the running app to check a change works
- reading, analysing or re-scoring results that already exist

### How to ask

Give the number before the question:

> "This is ~450k tokens and about 2 minutes. Run it?"

For an A/B, price both halves. If a cheaper measurement answers the same
question, say so and offer that instead — `rescore.py` exists precisely
because re-scoring stored predictions answers "did the correction change our
score?" for nothing.

## Measure before building

The pattern that has actually worked here: a feature ships behind a toggle,
default off, gets A/B'd on the same questions with per-question McNemar, and is
**deleted if it does not earn its model call**. The critic was removed that
way. `docs/08-intent-design.md` holds the current evidence and the open
questions.

Corollary: score against **corrected** gold (`benchmarks/rescore.py`). BIRD's
own annotations are 52.8% wrong, so the original gold is a noisy target.

## Verify, do not assert

Claims about behaviour need a command and its output, not a reading of the
source. Several things in this repo were "obviously fine" and were not:
`tsc --noEmit` was checking nothing, the tuning panel displayed the opposite of
what the engine ran, and `data/control.db` — password hashes and live session
tokens — was committed to git.
