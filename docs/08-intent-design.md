# 8. Where the accuracy actually goes, and the one change worth making

This document exists because the architecture was measured properly for the
first time and most of it turned out to be inert. It records what the
measurements say, what they rule out, and the single design change the evidence
points at.

Everything here is a number from this repository, not a plan.

---

## 1. What `fast` actually is

`fast` is the tier that scores best. Here is everything it does:

```
question
   ├─ screen              SKIPPED   prescreen_input=False, ambiguity=best_effort
   ├─ check_cache         SKIPPED   cache_sql=False
   ├─ select_tables       SKIPPED   schema (3-13 tables) <= full_schema_threshold(15)
   ├─ build_context       deterministic: every table, columns, FKs, 2 sample rows
   ├─ generate_sql        MODEL CALL 1 — one shot, temperature 0, no sampling
   ├─ criticise           SKIPPED   use_critic=False
   ├─ validate_and_execute    deterministic: AST validation, LIMIT injection,
   │                          EXPLAIN cost gate, read-only transaction,
   │                          fan-out check (advisory only)
   └─ write_answer        MODEL CALL 2 — rows to prose, faithfulness check
```

**2.0 model calls per question.** No retrieval, no voting, no critic, no
decomposition, no glossary, and — measured across 150 questions — **zero
repairs**.

So the thing this project calls an architecture is, in the configuration that
scores highest, a **single-shot text-to-SQL baseline with a strong
deterministic safety layer**. The safety layer is real and does its job. The
accuracy machinery was never on.

---

## 2. What the measurements rule out

### The quality ladder is worth nothing or less

Same 150 questions, same seed, no glossary:

| tier | accuracy | s/q | calls/q | tokens |
|---|---|---|---|---|
| fast | **61.3%** (92/150) | 3.5 | 2.0 | 454k |
| medium — voting | **61.3%** (92/150) | 7.5 | 4.0 | 1,189k |
| thorough — voting + critic + decomposition | **57.3%** (86/150) | 14.0 | 7.2 | 1,990k |

```
medium   vs fast: rescued 2, broke 2, net  +0    McNemar p = 1.000
thorough vs fast: rescued 2, broke 8, net  -6    McNemar p = 0.109
```

Not "the gains are small". Zero, then negative, at 2.6x and 4.4x the tokens.

### The model is not the bottleneck

Identical pipeline, only the model changed:

| model | accuracy |
|---|---|
| **google.gemma-4-31b** | **61.3%** |
| zai.glm-5 | 56.0% |
| qwen3-coder-480b-a35b | 56.0% |
| deepseek.v3.2 | 52.0% |

A **480B** model scores 5.3pp *worse* than the 31B one on the same prompts.
Buying a bigger model does not close the gap, and this is the cheapest
hypothesis to have eliminated.

### Retrieval has a measured ceiling of under one point

On the merged 75-table PostgreSQL build, where BFS genuinely engages:

| | Accuracy |
|---|---|
| Handed exactly the right 3–13 tables | 57.0% |
| Finding them among 75 by BFS | 56.4% |
| **Cost of retrieval** | **−0.7pp**, p = 1.000 |

Of 66 failures there, **one** was caused by retrieval missing a column. Perfect
table selection — which multiple seed sets approximates — is worth less than a
point.

### …but dumping the whole schema is not free either — now measured

The paragraph above compares BFS against *perfect tables*. It says nothing
about BFS against *dumping everything*, which is what `fast` actually does on
BIRD, where every database is under the 15-table threshold and retrieval is
skipped on all 150 questions.

That A/B has now been run. `full_schema_threshold=0` forces retrieval on;
everything else identical.

| | accuracy | vs whole schema | s/q | calls | tokens |
|---|---|---|---|---|---|
| whole schema (current `fast`) | 61.3% | — | 3.5 | 2.0 | 454k |
| **BFS, 1 hop** | **62.7%** | rescued 4, broke 2, **net +2** | 5.0 | 3.0 | **421k** |
| BFS, 2 hops | 60.0% | rescued 2, broke 4, net −2 | 4.8 | 3.0 | 456k |

**p = 0.688** on four discordant questions. A direction, not a finding.

Two things are worth keeping even at that significance. One hop scored higher
on **fewer tokens** than sending everything, despite an extra model call —
retrieval pays for itself on prompt size alone. And two hops scored *worse*
than one, which is the first direct evidence in this repo that **more context
can hurt**, rather than merely cost more.

The honest status: promising, underpowered, and it needs the full 500 to
become a finding. It is not a reason to change the default yet.

---

## 3. The failure that is actually left

```
62 failures out of 150

  no SQL produced                    0
  SQL would not execute              1
  SQL ran and returned wrong rows   61        <- 98%

  repairs triggered on failures:  61 at zero repairs, 1 at three
  questions rescued by a repair:   0
```

Three repair loops, a validator, a cost gate — and on 61 of 62 failures
**nothing fired, because nothing errored**. The query parsed, passed every
guard, executed, returned rows. It was the wrong query.

Two hand-verified cases:

```
Q:      the ratio of OUTPATIENT to INPATIENT among SLE patients
SQL:    matches the gold exactly — scored CORRECT
Answer: "The ratio of INPATIENT to OUTPATIENT ... is 1.3095"
```

```
Q:      customers whose first order was from the 'craving deals' category,
        how many went on to 2+, 3+, 4+, 5+ orders
SQL:    SELECT ... FROM orders GROUP BY "userId" HAVING count > 1
        -- no category join at all; the filter was silently dropped
Answer: 519 / 341 / 242 / 178  (the entire customer base)
```

In the second case `categories` was in the prompt. Retrieval did its job. The
literal `'craving deals'` matched nothing — the real name is `Cravings Deals ⭐`
— and instead of asking, the system dropped the constraint.

---

## 4. Why every existing check misses it

| check | sees question | sees SQL | sees schema | **sees rows** |
|---|---|---|---|---|
| `clarify` — asks | yes | no | table names | **no** |
| `critic` — judges | yes | yes | yes | **no** |
| `faithfulness` | **no** | no | no | yes (vs prose) |

Every check is *question ↔ SQL* or *rows ↔ prose*. **Nothing anywhere compares
the question against the rows.** That is exactly where 61 of 62 failures live.

The critic is not too harsh and is not aimed at style — its first rule is
literally *"the query answers a different question than the one asked"*. It
fails because it is reading code and guessing. Its signature is the whole
problem:

```python
review(client, *, question, schema_text, sql, model)   # no rows
```

On the Cravings Deals query it would have seen plausible SQL and approved it.
What gives that query away is the *result*: a cohort the size of the entire
customer base. And the clarifier cannot help either — it runs before execution,
when there is no evidence to be suspicious of.

---

## 5. The change

**Status: built, wired, tested, A/B'd once — positive, and not significant.**
`src/sqlagent/guards/evidence.py`, `src/sqlagent/intent.py`, and Loop D in
`src/sqlagent/agent_graph.py`, behind `check_result_intent` (default off). 32
tests; run it with `benchmarks/bird.py --intent`. The numbers are in §6c.

One model call, **after** execution, that sees everything:

```python
def check_intent(
    client, *,
    question,        # what was asked
    conversation,    # the thread, plus answers to earlier clarifications
    sql,             # what was run
    schema_text,     # what it could have used
    columns, rows, row_count, truncated,   # what came back
    unmatched_literals,                    # values in the question found in no column
    unfiltered_count,                      # what the count would be with no WHERE
    model,
) -> Verdict
```

Three outcomes, not two:

* **`answers`** — write the answer.
* **`mismatch(reason)`** — regenerate with the reason in the repair prompt.
* **`ask(question, options)`** — put it back to the user.

### How it sits in the graph

```
validate_and_execute ──success──► check_intent ──answers───► write_answer
                                              ──mismatch──► build_context
                                              ──ask───────► give_up (as a question)
```

Three bounds, each for a reason that was paid for once already:

* **`intent_repair_attempts = 1`**, not the shared repair budget. A database
  error either stops recurring or does not; "these rows do not answer the
  question" is an opinion that can be held about every rewrite in turn. The
  second attempt has a named defect the first did not — that is the part with
  a mechanism behind it, and there is no third.
* **A cached statement is never re-judged**, for the same reason the critic
  skips one: it would undo the point of the cache.
* **`ask` is refused when `ambiguity_handling` is `best_effort`.** A
  deployment configured never to interrupt the user must hold here too, and
  the harness sets exactly that.

One cost worth naming rather than discovering later: a `mismatch` sends a
*working* result back to be rewritten, and if the rewrite then fails to
execute, the run gives up instead of falling back to the result it already
had. That is the critic's existing shape. It is bounded to one rewrite, and if
the A/B shows it losing questions that way, that is the number that decides
whether the fallback gets built or the check gets deleted.

### What the deterministic half already catches

`guards/evidence.py` settles the checkable parts before any model sees them.
Verified against the real failures and against correct queries:

```
category filter dropped entirely  →  "the question mentions 'Veg Spring Rolls',
                                      'Dal Makhni' — appears nowhere in the SQL"
status = 'DELIVRED'               →  "that value does not occur anywhere in
                                      orders.status; the filter matches nothing"
kitchenName = 'EatCrave'          →  same, on plain text
correct COUNT query               →  silent
correct listing query             →  silent
```

Two bugs were found by testing it against **correct** queries alongside broken
ones, and both are the reason that matters:

* *"the same rows with and without the WHERE"* is true of **every** un-grouped
  aggregate by construction, so the check fired on a perfectly good
  `SELECT count(*) … WHERE status = 'DELIVERED'`. That is precisely the false
  alarm this module exists to avoid — it teaches the model to distrust correct
  SQL — and it was only visible because a correct query was in the test set.
* PostgreSQL **enums raise rather than returning no rows**:
  `invalid input value for enum "OrderStatus": "DELIVRED"`. That error is
  *stronger* evidence than an empty result, which could also mean the table is
  empty, so it is now treated as a confirmed finding rather than a failed probe.

### Why after execution

Because the rows are the cheap signal, and only they distinguish the failures
we have:

* a filter that matched nothing → **0 rows**
* a filter that was dropped → **row count identical to unfiltered**
* a label inverted → only the question reveals it

Two of those three are arithmetic, not judgement, which is why
`unmatched_literals` and `unfiltered_count` are inputs. A literal in the
question that matches no value in any candidate column is a **fact we can
check**, and it must produce a question rather than a dropped filter. Had that
one rule existed, the Cravings Deals answer would have been *"I can't find a
category called 'craving deals' — did you mean 'Cravings Deals ⭐'?"*

### Why it replaces the critic rather than joining it

Same cost — one model call — strictly more information, and pointed at the
failure that exists rather than the one we imagined. The critic has now been
measured twice at net zero and once at net −6. Keeping both would be paying
twice to answer the same question, once blind.

### Why "ask" belongs here and not only up front

The clarifier asks before there is evidence, so it asks about the *wording*. In
the session that produced the Cravings Deals failure it asked *"order by which
column?"* — a real ambiguity, correctly spotted — and in the same breath
silently dropped a category filter it could not resolve. Asking works. It was
aimed at the wrong thing because it had nothing to look at.

---

## 6. How it gets judged

The same discipline that killed the critic:

1. Ships as a toggle, **default off**. Done.
2. A/B against `fast`, same 150 questions, same seed.
3. Scored on **corrected** gold via `benchmarks/rescore.py`, because the
   original gold has a 52.8% annotation error rate and measuring against it is
   measuring against noise.
4. Reported per-question — rescued, broke, net, McNemar — not as two totals.
5. If it does not earn its model call, it is deleted, and this section is what
   says so.

One thing to hold on to while reading that score: **an `ask` counts as a
failure**, because a harness has nobody to ask. Each outcome records
`intent_asked`, so the asks can be counted and subtracted in prose rather than
quietly absorbed into the failure total — and the fix is to report it, not to
switch asking off to flatter the number.

Two other runs stand ahead of or beside it, both one run each:

* ~~**`full_schema_threshold=0` vs `15`**~~ — **run**. One hop 62.7% against
  61.3% for the whole schema, on fewer tokens; two hops 60.0%. p = 0.688, so
  it needs the full 500 before the default changes.
* **Corrected gold on the full 500**, not the 119 matched by question text, and
  repeated, so a number can be ranked rather than quoted.

---

## 6b. What a run actually costs

From the AWS Pricing API, `google.gemma-4-31b`, us-east-1, per 1K tokens:
input $0.00014, output $0.00040 (standard); half that on flex/batch.

| tier | per question | per 1,000 questions | a 150-question run |
|---|---|---|---|
| fast | $0.00045 | $0.45 | $0.068 |
| medium | $0.00117 | $1.17 | $0.176 |
| thorough | $0.00195 | $1.95 | $0.292 |

Eight runs in one day came to **$0.86**. Inference is not the constraint here —
wall-clock and attention are — but a run is still the user's call to make, and
`CLAUDE.md` records that it must be asked for.

Output is **3.4%** of tokens: this workload is input-dominated because the whole
schema goes into every prompt. That is also why one-hop BFS came out *cheaper*
than sending everything.

## 6c. The first A/B — run 2026-09-28

150 BIRD questions, seed 7, `google.gemma-4-31b`, no glossary, everything else
identical. `benchmarks/results/intent-on.json` against `tier-fast.json`.
Corrected gold is `arcwise_plat_sql_only_with_diff`, the same 119-question
basis every other corrected number in this document uses.

| | fast | fast + intent |
|---|---|---|
| accuracy, original gold | 61.3% (92/150) | **63.3% (95/150)** |
| accuracy, corrected gold | 77.3% (92/119) | **81.5% (97/119)** |
| tokens | 454k | 894k (1.97x) |
| model calls per question | 2.02 | 3.15 |
| wall clock | 94s | 165s |
| cost | $0.068 | $0.131 |

Paired on corrected gold, 118 questions: **rescued 5, broke 0, net +5,
McNemar exact p = 0.062.**

Zero broken is the part worth pausing on. The check can only lose a question
by rejecting a right answer and getting a worse one back, and across 150
questions it did that **no times** on corrected gold. The critic, on the same
kind of comparison, broke four.

### Why it is still not a finding

**43 of 149 questions produced different SQL with no mismatch ever firing.**
Generation is at temperature 0 and the prompts were identical, so those are
run-to-run nondeterminism, not the change. A paired comparison over the whole
set therefore carries variance of that size alongside the effect.

The attributable subset is the 17 questions where the check actually fired —
11% of them:

| | rescued | broke | net | p |
|---|---|---|---|---|
| corrected gold, 11 of the 17 scorable | 4 | 0 | **+4** | 0.125 |
| original gold, all 17 | 4 | 2 | **+2** | — |

Honest claim: the check fires on about one question in nine, and where it
fires it has so far only helped. p = 0.062 is a direction, not a result. The
run that settles it is the full 500 (#78).

### What it beats, on the same measurement

The critic — the thing it replaces — measured **rescued 4, broke 4, net 0** at
1.9x tokens, then net −6 inside `thorough`. This is net +5, broke 0, at 1.97x.
Same cost, strictly more information: that was the argument before the run,
and the run did not contradict it.

### What the run could not measure

The harness sets `ambiguity_handling="best_effort"`, so `allow_ask` is False
and **no question was ever put to anyone**. The `ask` path — the outcome that
is meant to be the point of this design — is entirely unmeasured here, and the
zero in the results file is a setting, not a result. `intent_ask_withheld` now
records what it would have asked, so the next run can report how many answers
were given by guessing. This one cannot.

---

## 6d. The full 500 — run 2026-09-28

All 500 mini-dev questions, `google.gemma-4-31b`, no glossary, `fast` against
`fast + intent`. `full500-fast.json` and `full500-intent.json`. Scored against
`arcwise_plat_sql.json`, pinned in `benchmarks/CORRECTED_GOLD.md`.

| | fast | fast + intent |
|---|---|---|
| accuracy, original gold | 63.0% (315/500) | 64.0% (320/500) |
| accuracy, corrected gold | 72.3% | **75.4%** |
| tokens | 1.50M | 2.96M (1.98x) |
| calls per question | 2.00 | 3.17 |
| wall clock | 494s | 3,760s |
| cost | $0.224 | $0.434 |

Paired, corrected gold, split by what actually happened:

| bucket | n | fast | + intent | rescued | broke | net | p |
|---|---|---|---|---|---|---|---|
| all paired | 415 | 72.3% | 75.4% | 21 | 8 | +13 | 0.024 |
| **the check fired** | **52** | **30.8%** | **46.2%** | **12** | **4** | **+8** | **0.077** |
| SQL differed, no fire | 110 | 70.0% | 74.5% | 9 | 4 | +5 | 0.267 |
| SQL identical | 261 | 81.2% | 81.2% | 0 | 0 | 0 | 1.000 |

### What can be claimed

The check fires on **15.6%** of questions and it picks the hard ones: the
baseline scores **30.8%** on them against 81.2% on the questions it stays
quiet about. On those 52 it rescued 12 and broke 4.

**+8, p = 0.077. A direction, not a result.** Of the +13 overall, 5 sits in a
bucket where the check did nothing and the two runs simply generated different
SQL — 110 of 415 questions, 27%, differ with no mismatch fired. Generation at
temperature 0 is not deterministic here, and that is the noise floor any
future A/B on this harness has to clear.

### A correction, recorded rather than quietly fixed

This table was first published against a different corrected file
(`arcwise_plat_sql_only_with_diff.json`) which was lost with a temporary
directory and could not be re-obtained. On that file the no-fire bucket scored
+9 at p = 0.023 and was written up here as evidence that the headline could
not be trusted. On the canonical file it is +5 at p = 0.267.

The conclusion survived — the attributable effect is +8 and does not reach
significance either way — but the alarm was overstated, and one unpinned
dependency moved a p-value from 0.021 to 0.077. See
`benchmarks/CORRECTED_GOLD.md`.

### The ask path, still unmeasured

3 of 415 questions had a question suppressed by `allow_ask=False`. It wants to
ask, rarely. That path stays unmeasured because a harness is the wrong
instrument for it.

---

## 7. What is honestly known about where we stand

On the corrected benchmark, `fast`, gemma-4-31b, 119 matched questions:

**77.3%, 95% CI [69.0%, 83.9%]**

Against published results on the same corrected set:

| System | Arcwise-Plat-SQL |
|---|---|
| ReViSQL-235B | 93.17% |
| OpenSearch-SQL (GPT-5.2) | 83.33% |
| GenaSQL (GPT-5.2) | 82.13% |
| Contextual-SQL (XiYan-32B) | 75.10% |
| CSC-SQL (XiYan-32B) | 71.89% |
| SHARE (GPT-5.2) | 70.88% |

**We have not beaten SOTA.** We are about 16 points behind it. The number sits
in the band of the other 32B-model systems, and a point estimate on 119
questions with a ±7pp interval ranks nothing at all — four of those six systems
fall inside our confidence interval.

The ReViSQL paper's own conclusion is worth keeping in view: closing the gap to
human level *"required improved data quality rather than architectural
complexity."* On this evidence, so does closing ours.

---

## Sources

* Pervasive Annotation Errors Break Text-to-SQL Benchmarks and Leaderboards —
  https://github.com/uiuc-kang-lab/text_to_sql_benchmarks
* ReViSQL — https://arxiv.org/html/2603.20004v1
* XiYan-SQL — https://arxiv.org/pdf/2411.08599
