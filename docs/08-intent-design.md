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

**Caveat, and it is a real one.** That is BFS versus *perfect tables* on 75
tables. It is **not** BFS versus *dumping the whole schema* on a small one,
which is what `fast` does on BIRD. That A/B has never been run, and it is one
run: `full_schema_threshold=0` against `15`, same questions. Until it is run,
"more context is always fine" is an assumption, not a finding.

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

1. Ships as a toggle, **default off**.
2. A/B against `fast`, same 150 questions, same seed.
3. Scored on **corrected** gold via `benchmarks/rescore.py`, because the
   original gold has a 52.8% annotation error rate and measuring against it is
   measuring against noise.
4. Reported per-question — rescued, broke, net, McNemar — not as two totals.
5. If it does not earn its model call, it is deleted, and this section is what
   says so.

Two other runs stand ahead of or beside it, both one run each:

* **`full_schema_threshold=0` vs `15`** — does forcing seed selection and a
  one-hop BFS beat dumping the whole schema on a small database? Untested.
* **Corrected gold on the full 500**, not the 119 matched by question text, and
  repeated, so a number can be ranked rather than quoted.

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
