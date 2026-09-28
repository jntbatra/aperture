# 9. What the systems above us actually do, and what of it we can use

Seven parallel reads of primary sources — ReViSQL, OpenSearch-SQL, CHASE-SQL,
XiYan-SQL, CSC-SQL, GenaSQL/N-rep, SHARE — plus the cross-cutting literature on
value retrieval and execution-based selection. Every number below is quoted
from a paper, a repository, or measured here. Where a source does not publish a
number, this document says so rather than estimating one.

Our position: **72.3%** on corrected BIRD mini-dev (`arcwise_plat_sql.json`,
n=415), `google.gemma-4-31b`, 2 model calls, 3.2s per question.

---

## 1. The finding that constrains everything else

Before importing any technique, we measured whether candidate ensembling has
room to work here at all. We already had two full 500-question runs of the same
pipeline. A perfect chooser between just those two is the ceiling of any
selection mechanism over that pair:

| | corrected gold, n=415 |
|---|---|
| fast | 72.3% |
| fast + intent | 75.4% |
| **oracle over the two** | **77.3%** |
| headroom over the better one | **+1.9pp** |
| **executions that already agree** | **374/415 = 90.1%** |

**On nine questions in ten the two candidates return identical rows.** A vote
has nothing to choose between them. That is the mechanism CHASE-SQL reports
+5.84 from and CSC-SQL reports +1.9 from — and on our candidates it has almost
no surface to act on.

**What this does and does not settle.** Both runs used the same prompt and the
same schema rendering at temperature 0, so the only diversity between them is
incidental sampling noise. It settles that *temperature-style* diversity is
nearly exhausted here. It does **not** test GenaSQL's claim, which is that
diversity should come from the **schema rendering format** instead. That
remains open and is the reason §3 is ordered the way it is.

---

## 2. What every source agrees on, including two of our own results

**An untuned model asked to judge SQL makes things worse.** Four independent
measurements:

| source | finding |
|---|---|
| CHASE-SQL Table 7 | untuned ranker **65.51** vs plain execution voting **68.84** |
| XiYan-SQL Table VIII | selector on randomly-ordered candidates **68.19** vs majority voting **70.21** |
| CSC-SQL Table 2 | merge-reviser without GRPO is below baseline in **all 16 cells**, worst 63.65 → 53.91 |
| SHARE Table 2 | GPT-4o self-correction **55.87 → 55.28** |
| **ours** | **critic net 0, then −6** |

SHARE's authors state the mechanism directly: *"the model lacks reliable
mechanisms to assess the correctness of its prior reasoning steps, sometimes
converting originally correct solutions into incorrect ones."*

Our critic result was not a bug in our critic. It is the documented behaviour
of the intervention. **Every system that gets value from selection either
fine-tunes the selector (CHASE, XiYan, CSC-SQL, SHARE) or does not use a model
at all.**

XiYan adds the one repair that needs no fine-tuning: **ordering**. Same model,
same candidates, random order 68.19 → clustered-and-ranked **73.34**. If a
selector is ever revisited here, the input layout is the variable, not the
prompt.

---

## 3. Candidates, ranked by evidence and cost

### 3.1 Schema representation — the only free item

We render a plain text listing with 2 sample rows per table. Three of the
systems above use a structured format with **per-column** distinct values.

XiYan's M-Schema against bare DDL, models not fine-tuned on the format:
GPT-4o **+3.65**, OmniSQL-32B **+4.50**, Codestral-22B +3.52, Gemini-1.5-pro
+3.45, Claude 3.5 +2.86, DeepSeek-V3 +2.87. Two counterexamples: TableGPT2
**−0.72**, DeepSeek-v2.5 +0.13.

Discount honestly: their DDL baseline had no descriptions and no values, and we
already ship sample rows. **Nobody has measured M-Schema against a descriptive
listing.** Plausible band here: **+1 to +3**, and it could be negative.

What the format carries that ours does not:
* per-column `SELECT DISTINCT … LIMIT 3` — exposes each column's *domain*,
  including low-cardinality enums. Two whole rows do not.
* explicit uppercase types, `Primary Key` markers, an explicit FK edge list.

### 3.2 BIRD ships column documentation and we discard all of it

`database_description/*.csv`, present for all 11 dev databases, unreferenced
anywhere in our code. Measured across them:

| | |
|---|---|
| columns with a description row | 799 |
| meaningful `column_description` | 565 (71%) |
| non-empty `value_description` | 278 (35%) |
| **at least one** | **617 (77%)** |

The `value_description` field is our failure class, pre-solved:

```
card_games.availability
  desc: A list of the card's available printing types.
  vals: "arena", "dreamcast", "mtgo", "paper", "shandalar"
```

ReViSQL uses these descriptions first and falls back to sampled values only
where one is missing. OpenSearch-SQL builds its entire column rendering from
them. This is shipped benchmark input, not leakage, and it generalises:
`COMMENT ON COLUMN` is the production equivalent.

**Zero model calls. Same renderer change as 3.1.**

### 3.3 Dynamic few-shot exemplars — the largest published lever

The only fine-grained ablation on mini-dev is OpenSearch-SQL's, and few-shot
dominates it:

| removed | ΔEX_G (single SQL, our architecture) | ΔEX final |
|---|---|---|
| **few-shot** | **−6.2** | **−4.6** |
| Extraction | −4.2 | −3.2 |
| Info Alignment | −3.0 | −2.0 |
| CoT | −2.8 | −1.4 |
| column filtering | −2.6 | −2.0 |
| value retrieval | −1.4 | −1.4 |
| self-consistency & vote | — | −2.4 |

Their baseline is 70.6, close to our 72.3, which makes the comparison unusually
relevant. Query-CoT-SQL exemplars beat plain Query-SQL exemplars by a further
−2.8 EX_G.

Mechanism: embed the 9,428 BIRD train questions once, cosine top-3 per
question, inject as demonstrations. **Zero extra generation calls** — one
embedding lookup.

**The caveat that may cap it:** BIRD's annotations are 52.8% wrong and the
corrections cover mini-dev, **not train**. Every retrieved exemplar carries
gold SQL that is more likely wrong than right. No source measures few-shot
retrieval against corrected exemplars.

### 3.4 Candidate diversity from rendering format — the one genuinely new idea

GenaSQL (N-rep) generates every candidate at **temperature 0 with no CoT**, and
gets diversity purely by rendering the same schema three different ways.
Nobody else does this: CHASE varies CoT templates, CHESS varies temperature,
XiYan varies fine-tuned generators.

Their BIRD dev numbers, one call versus the full system:

| model | single call | N-rep | Δ |
|---|---|---|---|
| Qwen3-8B | 44.98 | 57.04 | **+12.06** |
| Qwen3-14B | 53.46 | 61.67 | +8.21 |
| Qwen3-32B | 56.71 | 64.02 | **+7.31** |

The gain is inversely proportional to model scale — which favours a 31B. That
is the opposite of the pattern in every other paper here, and it is the reason
this is worth testing despite §1.

Their selector is also the most disciplined in the literature: vote on
execution results first, and escalate to a pairwise LLM comparison **only** on
three named vote-distribution patterns. Table 2: regular voting 67.2 →
confidence-aware 68.8, at **60% fewer calls** than always-on judging. The judge
sees the first 10 rows of each candidate's output, not just two SQL strings.

### 3.5 Measured dead on arrival here

**Retry on execution error.** Our full-500 run had **3 hard errors in 500
questions.** The trigger has a ceiling of 3 questions. XiYan reports −0.55 for
removing it; we cannot reach even that.

**Value retrieval as a separate index.** OpenSearch measures it at −1.4, the
smallest component in their table, and 3.2 delivers much of the same
information for free. Revisit only if 3.1 and 3.2 land and the
literal-mismatch failures survive.

**Anything requiring fine-tuning.** ReViSQL's decomposition is the clearest
statement of the ceiling: base 81.37 → +7.18 verified data → +2.82 reward
shaping → **+1.60 self-consistency**. **86% of their headline is training.
1.60 points is the entire inference-time contribution**, and their v3 paper
deleted its own pipeline stage and scored the same. Its title is now
*"…Without Pipeline Engineering."*

---

## 4. The ablation order

One change per run, McNemar per question against corrected gold, against the
`full500-fast.json` baseline at 72.3%.

| # | change | calls | cost | stop rule |
|---|---|---|---|---|
| 1 | column descriptions + per-column distinct values | 2 | $0.22 | — |
| 2 | full M-Schema syntax on top | 2 | $0.22 | drop if 1 already got it |
| 3 | few-shot top-3 from BIRD train | 2 | $0.22 | — |
| 4 | 3 renderings, temp 0, vote on execution results | 4 | $0.45 | **stop here if net ≤ 0** |
| 5 | confidence-gated pairwise judge | 4–6 | $0.50 | only if 4 wins |

Steps 1–3 leave the call count at 2. Step 4 is the first that changes the cost
curve, and §1 says it starts at a disadvantage: 90.1% of our candidate pairs
already agree.

**The noise floor is the precondition for all of it.** 27% of questions
produced different SQL between two identical-config runs. A `fast` vs `fast`
run with no change measures that, and without it a +2 result cannot be
distinguished from a re-roll. It costs one run, ~$0.22, and it should go first.

---

## 5. What this says about where the project actually stands

The techniques above are worth, on published numbers and discounted for our
baseline, somewhere between **+2 and +8 points**. That would put us at
roughly 74–80% — level with Contextual-SQL (75.10) and beneath
GenaSQL (82.13) and OpenSearch-SQL (83.33), both of which run on GPT-5.2.

**No combination of them reaches ReViSQL's 93.17%.** Their own ablation says
why, and it is not a pipeline you can build: verified training data was worth
+7.18 points, and training on the *original* BIRD train set scored **7 points
below not training at all**. Data quality was the binding constraint. The
authors removed their inference pipeline between versions and lost nothing.

The honest target for this architecture is **high 70s at 2–4 model calls**,
which is a defensible efficiency result rather than an accuracy record. The
one thing here nobody else is doing — asking the user when the question is
genuinely ambiguous — is also the thing ReViSQL's own failure taxonomy says
dominates at the ceiling: **69% of their residual errors are question
ambiguity, not model defects.** No benchmark can reward it.
