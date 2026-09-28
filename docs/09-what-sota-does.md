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

## 3.6 The head-to-head exists, and it diagnoses our own zero

Borchmann & Wydmuch (Snowflake), *Query and Conquer*, arXiv 2503.24364,
Table 1. Same candidate pool, same N, one column for text voting and one for
execution voting. Their `Maj@10` is defined as *"the majority vote with SQL
normalization"* using sqlglot — **that is exactly what we built and measured
at zero.** BIRD, temperature 0.7, single prompt, no schema linking:

| model | greedy | **Maj@10 (text)** | Exec@10 | exec − text |
|---|---|---|---|---|
| Llama 3.2 3B | 18.6 | 20.2 | 25.6 | +5.4 |
| Qwen 2.5 Coder 7B | 44.1 | 45.4 | 51.7 | +6.3 |
| **Gemma 3 27B** | **53.1** | **55.5** | **55.6** | **+0.1** |
| **Qwen 2.5 Coder 32B** | **55.0** | **55.2** | **57.1** | **+1.9** |
| GPT-4o | 51.6 | 51.6 | 52.4 | +0.8 |

Verbatim: *"weaker models benefit more from the proposed method."*

Two things fall out, and the second is the important one.

**Execution voting over text voting is worth +0.1 to +1.9 in our size class.**
Not the +5.84 CHASE reports on Gemini with 21 structurally diverse candidates.

**Gemma 3 27B gained +2.4 from text voting alone. We gained exactly zero.**
That is the diagnosis. If plain text voting pays two points for a comparable
model and pays nothing for us, our samples are not diverse enough to vote
over — a *generation* problem, not a *selection* problem. Execution voting
cannot fix that; it only merges candidates that already differ in text but
agree in rows. Our oracle confirms it from the other side: 90.1% of pairs
already return identical rows.

SQL-PaLM names the same failure: *"after fine-tuning, LLM's sampling output
converges to one single answer even using very high sampling temperature …
self-consistency decoding is observed not to help much."*

## 3.7 The best free signal in the whole literature

SQLens (AWS/MIT/UChicago, arXiv 2506.04494) defines an **Abnormal Result**:
the output is empty, or a column is entirely zeros, or a column is entirely
NULL. Precision at identifying a **wrong** query, on BIRD, across three
different generator systems:

| base system | precision | recall |
|---|---|---|
| DIN-SQL | **99.68%** | 37.01% |
| MAC-SQL | **100%** | 6.66% |
| CHESS | **98.48%** | 13.27% |

**A query returning nothing, or all zeros, or all NULLs is wrong 98.5–100% of
the time.** That is a deterministic, zero-model-call correctness signal with
near-perfect precision, and our `guards/evidence.py` already computes two
thirds of it.

Consequences, both free:
* such a candidate must never win a vote (SIRIUS scores it 0 by convention);
* it is the highest-precision trigger available for a repair pass.

CodeT names the failure this prevents: *"solutions that always output 'None',
'0', or an empty string … leading to a large cluster of incorrect solutions
that significantly affects performance."* LEVER measured it hurting Spider.

## 3.8 The intent check is already at the published ceiling

SQLens publishes fix/break accounting, which is the honest frame for any
repair pass. BIRD, out of 1,534:

| base system | method | net | fixed | broke |
|---|---|---|---|---|
| CHESS (67.91) | generic self-reflection | **+12** | 23 | 11 |
| CHESS | SQLens (signal-gated) | +28 | 40 | 12 |
| MAC-SQL (59.32) | generic self-reflection | **+12** | 39 | 27 |
| MAC-SQL | SQLens | +60 | 78 | 18 |
| vanilla (59.07) | generic self-reflection | **+1** | 22 | 21 |

Generic self-reflection on a strong base nets almost nothing because it breaks
nearly as much as it fixes. Signal-gated correction nets 3–4× more.

**Ours: +8 net on 415, fixed 12, broke 4 — a 3:1 ratio, in the SQLens band,
not the self-reflection band.** As a percentage, +1.93pp sits between
CHESS+SQLens (+1.83) and MAC-SQL+SQLens (+3.99).

CHASE-SQL's fixer shrinks monotonically with generator strength: +3.83 on a
57.75 generator, +0.93 on a 67.09 one. Self-Debugging: *"typically one
debugging turn is sufficient, and the accuracy improvement after one turn is
within 0.1%"* — which is `intent_repair_attempts = 1`, arrived at
independently.

**There is very little headroom left in this component.** It is built, it
works, and it is at the ceiling the literature reports.

## 3.8b What actually makes a repair pass work — and it is not the rows

This repository has argued since `docs/08` that the intent check works
*because it sees the returned rows*, where the critic did not. The evidence
does not support that as the main mechanism.

MapleRepair (arXiv 2501.09310), MAC-SQL + GPT-3.5 on BIRD, repaired / broken:

| variant | repaired | broken | net |
|---|---|---|---|
| LLM-Plain — sees the SQL only | 140 | 47 | +93 |
| **LLM-Exe — sees the execution result too** | **148** | **49** | **+99** |
| **Rule-Exe — gated on a deterministic signal** | **75** | **4** | **+71** |

Showing the rows is worth about **+6 questions out of 1,534** over showing
the SQL alone. What changes the picture is the **gate**: 4 broken instead of
49, at a 19:1 ratio instead of 3:1.

The ungated version of this is genuinely dangerous. MAGIC (arXiv 2406.12692),
BIRD dev, applying a repair guideline to *every* query: **56.52 → 46.14,
−10.38**. Oracle-gated to only the incorrect queries, the same method gives
+0.7 to +2.6. MapleRepair, verbatim: *"an improper repairing attempt would
exacerbate the errors!"* and *"LLM-Value introduces errors into 8.1% of the
correct SQL queries."*

**Correction to `docs/08`:** the claim that the intent check's advantage over
the critic is having rows in its prompt is not supported by the literature.
Its advantage is that it fires on 12.5% of questions rather than all of them,
and that `guards/evidence.py` gates it on facts checked against the database.
The rows are a secondary contributor. The measured 12 fixed / 4 broken is a
gating result, not a visibility result.

**The operational consequence is to hold the gate where it is, or tighten it.**
The firing rate is 52/415 = 12.5%. Widening it moves this component toward
MAGIC's −10.38, not toward more of the +8. The one direction worth exploring
is *narrowing* onto DB-computed signals — SQLens's Abnormal Result is
98.5–100% precise where an LLM judgement is not.

CSC-SQL Table 2 is the warning for a small model specifically: an untrained
reviser shown both SQLs *and* both execution results scores **−5.41 / −9.74**
(3B) and **−2.21 / −4.79** (7B) against its own baseline. Rows in the prompt
did not save it. Only GRPO training did.

## 3.8c Nobody publishes the experiment we ran

Across every system surveyed here — DAIL-SQL, CodeS, CHESS, XiYan-SQL,
OpenSearch-SQL, CHASE-SQL, Alpha-SQL, Arctic, CSC-SQL, SLM-SQL,
Agentar-Scale-SQL, DeepEye-SQL, DPC, Distillery — **not one votes on SQL
strings.** "Self-consistency" in this field already means execution-result
clustering. The only text-side arms that exist anywhere are LLM-judge
variants, and both lose to execution clustering.

So the A/B we ran — normalised-SQL-text voting against execution-result
voting on the same pool — is not in the literature. Our zero is not anomalous;
it is the measurement nobody bothered to publish, and *Query and Conquer*'s
`Maj@10` column is the closest published equivalent.

## 3.8d Implementation details, confirmed in shipped code

If step 5 is ever built, these are settled rather than invented:

* **Tie-break: shortest SQL in the largest cluster.** CHESS
  `aggregate_sqls` clusters on `frozenset(tuple(row) for row in result)`,
  drops non-OK, then `min(largest_cluster, key=len)`. Their ablation: 1
  sample 61.22 → 3-sample execution consistency **64.62 (+3.40)**.
* **Soft similarity is the shipped default, not a paper idea.**
  Arctic-Text2SQL-R1's `major_voting` defaults to per-column value-count
  distributions with a pairwise similarity matrix; the exact-`frozenset`
  vote is the non-default mode.
* **Empty results excluded explicitly.** OpenSearch-SQL §3.6: *"Exclude SQLs
  that cannot be fixed and those that result in empty answers … among SQL
  queries with the same answers, we select the one with the shortest
  execution time."*
* **Errored candidates never agree with each other** (MBR-Exec), so two
  crashing queries cannot form a cluster of two.

Three more same-system deltas, all inside the +0.5 to +2.5 band: Databricks
RLVR 32B **+2.12**, OpenSearch-SQL on mini-dev **+2.4**, DeepEye-SQL with
Gemma3-27B **+1.0**.

## 3.8e Spend on candidates, not on repairs

Olausson et al., *Is Self-Repair a Silver Bullet?* (arXiv 2306.09896),
verbatim: *"increasing the number of initial programs consistently leads to
relative performance gains … fixing n_p and increasing n_fr does not appear
to be worth the additional cost … the most important factor is the diversity
of the base samples generated up-front, rather than the diversity of the
repairs sampled."*

That is the same conclusion as §3.6 reached from our own zero: the pool is the
constraint here, not the chooser. CHASE-SQL's error budget puts ~10pp of BIRD
dev in "a correct candidate existed and was not chosen", and
Agentar-Scale-SQL's *RL-trained* tournament recovers only 16% of its own
11.21pp oracle gap. Selection is a fractional recovery of a gap that
generation has to open first.

## 3.9 We cannot validate a small gain at n=415

At 415 items, a +1.5pp effect is ~6 questions. With realistic discordance that
is p ≈ 0.3. Our +8 net only reached p = 0.077. **Clearing p < 0.05 needs
roughly +12 net, ≈ +2.9pp** — the top of the plausible range for anything
above, not the middle.

So the eval set has to grow before the next A/B means anything. Full BIRD dev
is 1,534 questions; there a +1.5pp effect is ~23 questions and becomes
resolvable. Mini-dev cannot settle the changes we are contemplating.

## 3.10 The empty-result guard, measured end to end on our own data

Two measurements, neither costing a model call, settle this one.

**BIRD's gold never returns nothing, and that is a policy, not luck.** 0 of
498 successfully-executing mini-dev golds return zero rows — and BIRD's
annotation protocol forbids it by construction. §3.4 of arXiv 2305.03111,
verbatim:

> *"the SQL validness will be confirmed that each SQL is executable and can
> return a valid result from the database. The 'valid result' refers to the
> set of results that is not 'NULL'. If the executed result set is 'NULL',
> experts will make slight changes to the conditions of the questions until
> the associated SQLs can provide a valid result set."*

So on BIRD dev "zero rows means wrong" is sound **by design**, not merely
measured. Three boundaries where it stops being sound:

* **BIRD train.** Arctic-Text2SQL-R1 drops 9,428 → 8,017 (**15.0%**) of train
  under an empty-or-slow filter while leaving dev at the full 1,534. If
  few-shot exemplars are ever drawn from train (§3.3), the guarantee does not
  cover them.
* **Spider.** 3–5% of golds are legitimately empty across three sources, which
  is why MAC-SQL's code disables this exact check on Spider only.
* **Production.** No annotation policy protects a real warehouse. Luo
  (VLDB 2006) measured **5.75%–38%** of real user queries returning empty
  across three production workloads.

### The trap this sets for us, stated plainly

Some of the gain this guard would show on BIRD exists **because BIRD
guarantees no empty golds**. Shipping the same rule against the production database would fire
on legitimately-empty answers — of which there are real ones, "no orders
cancelled last week" being a correct and useful reply — and trigger rewrites
that can only make them worse.

So the benchmark number for this feature is **partly an artifact of the
benchmark**, and it must not be quoted as a product improvement. Two
consequences for how it ships:

1. **Default off outside SQLite/BIRD**, keyed on the dialect, not a global
   flag someone can forget.
2. **The production gate is a different check.** ErrorLLM's Rule7 — walk the
   AST, resolve each literal to its column, confirm the literal exists in that
   column's domain — is sound whether or not the answer is legitimately empty,
   because it tests the *predicate* rather than the *result*.
   `guards/evidence.py` already computes exactly this as
   `_unmatched_literals`. The production feature is therefore mostly built;
   what is missing is the rebinding step, not the detection.

**Our own predictions hit it often, and it is nearly always right.**
Re-executing all 500 stored statements from `full500-fast.json`:

| signal | n | scored correct | precision |
|---|---|---|---|
| **empty result** | **16** | **0** | **100.0%** |
| a column entirely zero | 13 | 1 | 92.3% |
| a column entirely NULL | 9 | 2 | 77.8% |
| **all three** | **38 (7.6%)** | **3** | **92.1%** |

**16 questions where we are wrong and can prove it before answering, with no
model call.** Widening to empty-or-all-zero gives 29 questions at 96.6%.
All-NULL is the weak one — `LEFT JOIN` producing NULL is often correct, which
is exactly the exception SIRIUS names — so it belongs down-weighted, not
excluded.

This is the largest free signal found in the whole survey, and it is the
`Cravings Deals ⭐` failure by another name: a query returning zero rows,
reported to the user as the answer.

### What to do with it, in order of cost

1. **Never let a degenerate result win a vote.** Free, and it is what
   OpenSearch-SQL and SIRIUS already do.
2. **Deterministic literal rebinding, no model call.** On an empty result,
   pull the string literals out with sqlglot — `guards/evidence.py` already
   does this — and probe `SELECT DISTINCT col FROM tbl WHERE col LIKE
   '%literal%' LIMIT 5`. If a near-match comes back, rewrite and re-execute.
   SIRIUS recovers 223 of 1,043 empty candidates this way with zero LLM calls.
3. **Only then, one retry with a *typed* prompt** naming the empty result and
   the candidate values. Never a generic "fix this": DIN-SQL measures a
   generic repair prompt at **−3.3** where its gentle variant is +0.9, and
   ErrorLLM measures naive self-correction at −1.52% on BIRD and **−14.15%**
   applied on top of OpenSearch-SQL.

## 3.11 Value retrieval: sized honestly, and smaller than it looks

Every gold query on mini-dev was parsed and every string literal checked
against the question and the `evidence` hint:

| bucket | n | % |
|---|---|---|
| no string literal in gold — retrieval cannot help | 208 | 41.6% |
| every literal appears verbatim; the model can copy it | 274 | 54.8% |
| a literal needs case correction only | 10 | 2.0% |
| a literal is absent entirely | 8 | 1.6% |
| **addressable** | **18** | **3.6%** |

**3.6% is the entire surface.** A working prototype — stdlib only, SQLite
FTS5 trigram index, no embeddings, no network — recovers 14 of the 18 (78%)
in 60ms with zero model calls, and resolves `'Cravings Deals'` →
`'Cravings Deals ⭐'`. Build: 4.5s and 43MB for all 11 databases. Preserved at
`prototypes/valindex.py`.

Ceiling **+2.8pp** if every one of the 14 is currently wrong and every one
flips. Realistic **+1.3 to +1.6pp**. OpenSearch-SQL measured **−1.4 on this
exact 500**, which agrees from the other direction.

**The part that matters more than the benchmark number:** 10.2% of gold
literals appear *only* in BIRD's `evidence` hint. Production has no evidence
hint, so the addressable share there is **larger than 3.6%** — the benchmark
understates this feature's production value, and it should be judged on that
basis rather than on a mini-dev p-value. Also measured: **0 of 79 mini-dev
tables declare `COLLATE NOCASE`**, so the 10 case-only mismatches genuinely
fail and will not self-correct.

## 3.12 Schema reduction is settled — stop working on it

Google, VLDB 2025 (arXiv 2501.12372), Table 3, BIRD dev, **≤13 tables per
request, 6.82 average** — our exact regime:

| tables given | k=1 | k=7 | whole DB | whole dataset |
|---|---|---|---|---|
| EX | 38.01 | 54.69 | **62.32** | **62.58** |
| retrieval precision | 77% | 23% | <35% | **<2%** |
| tokens | 2,003 | 4,628 | 7,381 | 72,620 |

Pruning to the top 7 tables costs **7.63 points**. Dumping 72k tokens of
cross-database schema at under 2% precision costs **nothing**. Verbatim:
*"the model does not get confused despite the presence of a large number of
mostly irrelevant table definitions in the context."*

Corroborated three ways: a McNemar-tested **−3.65pp (p = 1.4×10⁻⁶)** for
schema linking on Llama-3.3-70B over BIRD dev; Distillery's −4.77 to −11.57
across three models; and CHESS's own authors **deleting their Schema
Selector** for their best BIRD configuration, having written that *"where the
schema contains approximately 100 columns, schema linking becomes
unnecessary."*

Our own result — whole schema ≈ 1 hop, 2 hops worse — is mainstream. The
caveat is that model strength, not table count, is the real variable, and
EDBT 2026 shows a *fine-tuned* linker still helping weaker backbones.

## 4. The ablation order

One change per run, McNemar per question against corrected gold, against the
`full500-fast.json` baseline at 72.3%.

| # | change | calls | cost | stop rule |
|---|---|---|---|---|
| 1 | column descriptions + per-column distinct values | 2 | $0.22 | — |
| 2 | full M-Schema syntax on top | 2 | $0.22 | drop if 1 already got it |
| 3 | few-shot top-3 from BIRD train | 2 | $0.22 | — |
| 4 | **literal-domain check + deterministic rebinding** (sound everywhere) | **2** | $0.22 | **16 questions at 100% precision on BIRD** |
| 4b | empty-result trigger, **SQLite/BIRD only** | 2 | — | sound by BIRD policy, *not* in production |
| 5 | value index (`prototypes/valindex.py`) behind a flag | 2 | $0.22 | 18 addressable, judge on production |
| 6 | 3 renderings, temp 0, vote on execution results | 4 | $0.45 | **only if pass@8 − pass@1 > 5pp** |
| ✗ | further schema reduction | — | — | settled: costs 7.63 at our table count |
| ✗ | any LLM judge or pairwise reranker | — | — | contraindicated by 5 sources |

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
