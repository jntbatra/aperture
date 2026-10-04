# Tickets

Every design decision taken in this project, as a ticket, with its real status.
Kept in the repo rather than in a tracker so the status survives the session
that produced it.

Status is one of **done**, **open**, **won't build**. "done" means the code
exists *and* a test covers it — a ticket is not closed by a plan.

---

## Closed

| # | Ticket | Evidence |
|---|---|---|
| 1 | Bounded BFS retrieval over a foreign-key graph | `schema/retrieve.py`, `tests/test_retrieval.py` |
| 2 | Deterministic fallback when seed selection returns nothing | `pipeline._match_tables_by_name`, `tests/test_retrieval.py` |
| 3 | sqlglot AST validation, not pattern matching | `guards/validator.py`, `tests/test_validator.py` |
| 4 | Read-only database role as the real boundary | `aperture_ro`, documented in `docs/06-operations.md` |
| 5 | Row cap fetching one row more than the limit | `db/execute.py`, `tests/test_execute.py` |
| 6 | Statement timeout per query | `db/execute.py`, `tests/test_execute.py` |
| 7 | Repair loop A — invalid or failing SQL | `agent_graph.py`, `tests/test_graph.py` |
| 8 | Repair loop B — context too narrow, widen schema | `agent_graph.py`, `tests/test_graph.py` |
| 9 | Repair loop C — critic rejection | `agent_graph.py`, `tests/test_critic.py` |
| 10 | Join fan-out detection | `guards/arithmetic.py`, `tests/test_arithmetic.py` |
| 11 | Self-join fan-out detection | same file — added after a real ₹271 → ₹1,076 miss |
| 12 | CTE re-aggregation detection | same file — added after a real 269 → 393 miss |
| 13 | Faithfulness guard: answer figures must appear in the rows | `guards/faithfulness.py`, `tests/test_faithfulness.py` |
| 14 | Self-consistency voting | `voting.py`, `tests/test_toggles.py` |
| 15 | SQL cache keyed on question + schema version | `cache.py`, `tests/test_cache.py` |
| 16 | Conversational memory — bounded turn window | `conversation.py`, `tests/test_conversation.py` |
| 17 | Carry small results forward, never large ones | `conversation.carryable_result` |
| 18 | Clarification turns distinguishable from failures | `conversation.Turn.clarification` |
| 19 | Ask rather than assume — one `Ask` per ambiguity | `clarify.py`, `tests/test_clarify.py` |
| 20 | Business glossary as a version-controlled file | `glossary.py`, `glossaries/example.json` |
| 21 | Question decomposition, hard-capped | `report.py`, `tests/test_report.py` |
| 22 | Per-request override allow-list | `config.OVERRIDABLE`, `tests/test_toggles.py` |
| 23 | MCP server exposure | `mcp_server.py`, `tests/test_mcp_server.py` |
| 24 | Chats with a chat ID; flat history view removed | `store.py`, `api/app.py`, `tests/test_store.py` |
| 31 | Result tables persisted across reopening a chat | `store.py`, `api/app.py` |
| 32 | Toggles exposed on the page, including hop counts | `frontend/src/Toggles.tsx`, `tests/test_toggles.py` |
| 33 | Bedrock Mantle bearer-token auth for gemma-4-31b | `llm/mantle.py`, `tests/test_mantle.py` |
| 36 | Screening runs before decomposition | `pipeline._screen_before_answering` |
| 37 | Decomposed parts see earlier parts' results | `pipeline._answer_in_parts` |
| 38 | `tsc -b`, not `tsc --noEmit` — project references made it vacuous | `Makefile` target `web-typecheck` |

---

## Closed in this pass

| # | Ticket | Evidence |
|---|---|---|
| 28 | Conversation summarisation beyond the window | `summarise.py`, `tests/test_summarise.py` (26), wiring in `tests/test_toggles.py` (8) |
| 29 | Drift detection | `drift.py`, `tests/test_drift.py` (33), `GET /api/drift`, `tests/test_api.py` (5) |
| 30 | LLM-as-judge for answer quality | `judge.py`, `tests/test_judge.py` (21), `--judge` on the BIRD harness, `tests/test_bench_report.py` (6) |
| 35 | Nothing committed to git | one commit, `.env` / history DB / uploaded datasets excluded, `.env.example` added |
| 40 | Three hand-maintained copies of the toggle list | `tests/test_api.py` — `OVERRIDABLE`, `AskOptions` and `TOGGLE_DESCRIPTIONS` are now checked against each other |
| 41 | Client guessed which toggles were numeric | `ToggleInfo.numeric`, declared by the server |
| 43 | Clarifications counted as failures in drift and in the sidebar | `clarify.CLARIFICATION_ERROR`, `drift.clarify_rate`, `store.stats`, tests in `test_drift.py` and `test_store.py` |
| 44 | `"needs_clarification"` written out as a literal in four places | one constant in `clarify.py`, imported by the graph, the pipeline, the API and drift |
| 45 | `/api/drift` had no interface — endpoint built, nothing showed it | `frontend/src/components/Drift.tsx`, verified rendering against live data with no console errors |
| 46 | New work documented only in `07-decisions.md` | `04-code-walkthrough.md` (summarise/judge/drift), `05-frontend.md` (Toggles, Drift, sidebar counters), `06-operations.md` (judge, stability, drift, conversation settings), `README.md` |
| 47 | Clarifier capped at 3, and the cap was hardcoded in the prompt | `max_clarifying_questions`, default 7, overridable; prompt interpolates the same number it is held to |

---

## Closed by measurement

| # | Ticket | Result |
|---|---|---|
| 34 | Ambiguity check nondeterministic and unmeasured | **100% stable**, 42 questions x 5 repeats, 210 calls, verdict spread `{1: 42}` |
| 39 | A query can answer a different question than asked | Detected by the judge (7 cases in 150, one verified by hand). Critic measured and **stays off**: net +0 accuracy, p = 1.000, 1.9x tokens |
| 48 | `provide_token()` read the region from the environment, not the setting | Failed every question in a non-interactive shell; `mantle_region` now passed explicitly |
| 49 | `llm_auth="sigv4"` unreachable whenever `llm_base_url` was set | Client dispatches on `resolved_llm_auth`; "local" decided from the hostname |
| 50 | 16 API routes documented nowhere | Reference table in `06-operations.md`, checked against the code |
| 51 | Stability set could only tell obvious from obvious | 12 unlabelled borderline questions added; still 100% stable |

---

## Open

| # | Ticket | State |
|---|---|---|
| 42 | `ruff format --check` fails repo-wide | open — needs a decision, not code |
| 52 | The answer can invert the question the SQL answered | open — new, and the interesting one |

### 52 — checking the sentence against the question

Found by running the judge. Seven of 150 answers were **correct and badly
written**: the SQL returned exactly the gold rows and the prose did not answer
what was asked.

```
Q:      ratio of OUTPATIENT to INPATIENT among SLE patients
SQL:    matches gold exactly — scored CORRECT
Answer: "The ratio of INPATIENT to OUTPATIENT ... is 1.3095"
```

Nothing in the pipeline can see this. Execution accuracy compares rows. The
faithfulness guard compares figures, and `1.3095…` is genuinely in the rows.
The critic reviews SQL, and this SQL deserved approving — measured above, it is
the wrong lever.

The fix belongs at the answer step: compare the sentence against the
**question**, not against the rows. There are seven verified cases in
`benchmarks/results/gemma4-31b-judged.json` to build against, which is the
right way round — the test cases exist before the code.

Costs a model call per question, so it would ship as a toggle, default off,
and be measured the same way the critic just was.

### 42 — repo-wide formatting

`make lint` runs `ruff check` (clean) and `ruff format --check`, which fails on
40 files, pre-existing and mostly untouched. The code is hand-formatted and
`ruff format` would reflow a lot of deliberately laid-out prose. Either accept
a large whitespace diff or drop the format check. Not decided.

## SaaS

**Removed 2026-10-04.** Accounts, sign-in, sessions, API keys, tenants, plans,
quotas, encrypted tenant credentials, and the marketing / pricing / checkout
pages were all deleted. The app has no login: whoever can reach the server can
use it, so it belongs on a private address. The code is in git history before
this change. `SAAS.md` keeps the original plan.

| # | Ticket | State |
|---|---|---|
| 53, 54, 58, 59, 64, 65, 66 | Tenancy, auth, credentials, plans, accounts, pages | **removed** |
| 67 | `data/control.db` was committed to git | **done** — removed from history; the file is now unused |
| 55, 56, 57, 62, 68, 69 | Per-tenant registry, caches, history; browser tokens; control-plane migrations; Razorpay | **dropped** — no tenants |
| 60 | Python SDK | open |
| 61 | TypeScript SDK | **server-side client done** — `sdk/typescript`, cheap / expensive tiers |
| 63 | AWS deployment | open — `deploy/EC2.md` |

## Accuracy

Everything here is measured. See [docs/08-intent-design.md](docs/08-intent-design.md).

| # | Ticket | State |
|---|---|---|
| 70 | Benchmark the whole architecture, not just the base pipeline | **done** — fast 61.3%, medium 61.3%, thorough 57.3%; the ladder is worth nothing or less |
| 71 | The production glossary was contaminating every BIRD prompt | **done** — removing it: 58.7% -> 61.3%, faster and cheaper |
| 72 | `--tier` never offered `medium`, so a sweep silently skipped it | **done** — harness reads the tiers from the engine, test binds them |
| 73 | Gold annotations are 52.8% wrong; every number was against noise | **done** — `benchmarks/rescore.py`, no model calls; fast 72.3% -> 77.3% |
| 74 | Is the model the bottleneck? | **done** — no. gemma-31b 61.3% beats qwen-480b 56.0% on the same pipeline |
| 75 | `check_intent` — one call after execution, seeing question + SQL + rows | **A/B'd on the full 500** — fires on 15.6% of questions; on those, 28.8% -> 48.1% (rescued 13, broke 3, p = 0.021). Headline +19 not trustworthy: a null bucket in the same run scored +9 |
| 76 | An unmatched literal must produce a question, never a dropped filter | open — the Cravings Deals failure, and checkable rather than guessable |
| 77 | Is BFS better than dumping the whole schema on a small database? | **run** — 1 hop 62.7%, whole schema 61.3%, 2 hops 60.0%; p = 0.688, needs the full 500 before the default moves |
| 78 | Score the full 500 on corrected gold, repeated | **half done** — 416 paired on corrected gold, both arms. The *repeated* half is now the important half: a no-change `fast` vs `fast` run is needed to establish the noise floor, because a bucket where nothing happened scored +9 at p = 0.023 |
| 79 | Medium and thorough are sold and are worse than fast | open — the pricing charges for a negative |
| 80 | Noise floor of the harness | **done** — identical configs: 11 rescued, 11 broke, net 0, p=1.000. Any effect under ±11 questions (2.6pp) is a re-roll |
| 81 | BIRD column documentation into the schema | **built, measured, default off** — +4, p=0.608, +37% tokens |
| 82 | Deterministic literal rebinding on an empty result | **built, measured, default off** — fired once in 500, +2, p=0.815 |
| 83 | Few-shot exemplars from BIRD-Verified | **built, measured, DO NOT ENABLE** — −24, p=0.0027. Retrieved question/SQL pairs cost four times the noise floor |

### 75 — exactly where it stands

**Done:**
* `src/sqlagent/guards/evidence.py` — deterministic facts: unmatched literals
  (including the PostgreSQL enum case, where the driver raises rather than
  returning no rows), filters that excluded nothing, distinctive phrases from
  the question absent from the SQL. Verified against the three real failures
  *and* against correct queries, which is how two false-positive bugs were
  caught before they shipped.
* `src/sqlagent/intent.py` — `check_intent()` returning
  `answers` / `mismatch(reason)` / `ask(question, options)`, with
  `allow_ask=False` for the harness since a benchmark has nobody to ask.

**Wired, 2026-09-28:** `check_intent` is a graph node between
`validate_and_execute` and `write_answer`, behind `check_result_intent`
(default off, deliberately *not* in `OVERRIDABLE` — nothing to put on a page
until the A/B says it earns its call). `mismatch` routes back through the
ordinary repair loop with the named defect attached; `ask` ends the run as a
clarification and is refused outright when `ambiguity_handling` is
`best_effort`, so a deployment that says never interrupt the user holds.
`intent_repair_attempts` is 1, not the general budget: a database error either
stops recurring or does not, whereas "these rows do not answer the question"
can be said about every rewrite in turn.

31 tests — `tests/test_intent.py` for the module, a new section of
`tests/test_toggles.py` for the wiring through the real graph against a real
database. 869 passing, ruff clean.

**A/B'd, 2026-09-28** — `benchmarks/results/intent-on.json`, 150 questions,
seed 7, gemma-4-31b, no glossary. Full numbers in `docs/08-intent-design.md`
§6c.

| | fast | fast + intent |
|---|---|---|
| corrected gold | 77.3% (92/119) | **81.5% (97/119)** |
| paired, 118 | — | rescued 5, broke 0, **p = 0.062** |
| where the check fired (17 questions) | — | rescued 4, broke 0 |
| tokens | 454k | 894k (1.97x) |
| cost | $0.068 | $0.131 |

Three caveats that belong next to that table:

* 43 of 149 questions produced different SQL with **no mismatch firing** —
  temperature-0 nondeterminism. The whole-set comparison carries that as
  noise; the 17-question fired subset is the attributable part.
* p = 0.062 is a direction. #78 is the run that settles it.
* The `ask` path was **never exercised** — the harness sets `best_effort`, so
  `allow_ask` was False throughout. The zero in the results file is a setting.
  `intent_ask_withheld` now records the suppressed question so the next run
  can report it.

It does beat what it replaces on the same measurement: the critic was rescued
4 / broke 4 / net 0 at 1.9x, then net −6 inside `thorough`.

**Full 500, 2026-09-28** — `full500-fast.json` vs `full500-intent.json`,
415 paired, scored against `arcwise_plat_sql.json` (pinned in
`benchmarks/CORRECTED_GOLD.md`). Detail in `docs/08-intent-design.md` §6d.

| bucket | n | fast | + intent | rescued | broke | net | p |
|---|---|---|---|---|---|---|---|
| all paired | 415 | 72.3% | 75.4% | 21 | 8 | +13 | 0.024 |
| **the check fired** | **52** | **30.8%** | **46.2%** | **12** | **4** | **+8** | **0.077** |
| SQL differed, no fire | 110 | 70.0% | 74.5% | 9 | 4 | +5 | 0.267 |
| SQL identical | 261 | 81.2% | 81.2% | 0 | 0 | 0 | 1.000 |

Quote the second row and its p-value. The check fires on 15.6% of questions
and picks the hard ones — baseline 30.8% on those against 81.2% elsewhere.
2.96M tokens against 1.50M; $0.434 against $0.224.

27% of questions produced different SQL with no mismatch firing. That is the
noise floor for every future A/B here.

**Not done:**
0. **The noise floor.** `fast` vs `fast`, no change, same 500 — one run,
   ~1.5M tokens, ~8 min, ~$0.22. If the no-fire bucket comes back at ±9, the
   +10 is inside the noise and nothing above is a finding. This now blocks
   every other decision here.
1. Delete the critic — the evidence now points that way, but it is a product
   change (it is what `thorough` is partly sold on) and #79 is the ticket that
   reprices that tier. Decide them together.
2. Turn `check_result_intent` on by default, and put it in `OVERRIDABLE` with
   a control — only after #78, not on p = 0.062.
3. Measure the `ask` path against something that can answer, which the BIRD
   harness by construction cannot.

**Reminder for whoever picks this up:** `ask` scores as a failure in the
benchmark because a harness cannot answer it, so the measured number
*understates* the real behaviour. Report it that way rather than disabling
asking to flatter the score. In the 2026-09-28 run it was disabled outright,
which is why that run says nothing at all about it.

### 75 — the one change

61 of 62 failures are a valid query that answered the wrong question, with
every table already in the prompt and zero repairs fired. Every existing check
compares question-to-SQL or rows-to-prose; **nothing compares the question to
the rows**. One model call after execution, seeing all three, returning
`answers` / `mismatch(reason)` / `ask(question, options)`. It replaces the
critic rather than joining it — same cost, strictly more information.

### 79 — the pricing is currently wrong

Pro sells "50 detailed answers" and detailed measured **worse** than fast at
4.4x the tokens. That must change before anyone pays.

---

## Won't build, and why

| # | Ticket | Why not |
|---|---|---|
| 25 | Multi-agent Manager + Analyst/Research/Compute | The pipeline is engineer-planned: the steps a SQL question needs are known in advance and encoded as a graph. A manager would re-derive that plan on every question, at a model call each, and get it wrong sometimes. Multi-agent buys flexibility this problem does not need, and pays for it in the property that matters most here — that the SQL is readable next to the answer. |
| 26 | Sandboxed code interpreter | A separate security project, not a feature. The whole safety argument of this system is that the only thing reaching the database is a statement proven to be a SELECT by a parser. Arbitrary Python alongside it makes every one of those guarantees conditional on the sandbox instead. |
| 27 | Research agent / web search | Every answer here traces to a row in a named table, which is why the SQL is shown. A retrieved web page does not, and an answer mixing the two is exactly as checkable as its weakest source while looking uniformly authoritative. |

These are decisions, not a backlog. Reopening one means arguing with the reason
above, which is the point of writing it down.
