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

| # | Ticket | State |
|---|---|---|
| 53 | Tenant identity: `Tenant`, `Principal`, API keys | **done** — `saas/tenancy.py`, 38 tests |
| 54 | Fail-closed auth on every front | **done** — `saas/auth.py`, `api/auth_routes.py`; the route-table test proved to bite |
| 58 | Encrypted tenant database credentials | **done** — `saas/secrets.py`, 25 tests |
| 59 | Plan tiers and quotas | **done** — `saas/plans.py`, 51 tests; enforced on both ask handlers |
| 64 | Connect a tenant database, proving the role is read-only | **done** — verified against a real read-only *and* a real writable role |
| 65 | Accounts, sessions, passwords | **done** — `saas/control.py`, `saas/passwords.py`; scrypt at 63ms |
| 66 | Marketing, pricing, auth and checkout pages | **done** — verified in a browser |
| 67 | `data/control.db` was committed to git | **done** — password hashes and live session tokens; never pushed, removed from history, `.gitignore` now matches `data/*.db` and `*.sqlite*` |
| 55 | Per-tenant agent registry | **open** |
| 56 | Tenant-scoped SQL and summary caches | **open** |
| 57 | Tenant-scoped history store | **open** |
| 60 | Python SDK | open |
| 61 | TypeScript SDK, server and browser shapes | open |
| 62 | Short-lived browser tokens | open — required before the browser SDK ships |
| 63 | AWS deployment | open |
| 68 | Control plane runs on SQLite with no migrations | open |
| 69 | Razorpay | open — deliberately deferred; the page says so on its own surface |

### 55, 56, 57 — why this cannot be hosted yet

With two real tenants today, `get_agent()` returns **one** agent for the whole
process, holding one `SqlCache` and one `SummaryCache` keyed without a tenant.
Tenant B can be served SQL generated for tenant A's question, and the history
table has no tenant column at all.

Authentication is done and correct. The isolation behind it is not. These three
come before the SDK and before AWS.

### 68 — the control plane's own database

`ControlStore` defaults to SQLite and creates its schema with
`CREATE TABLE IF NOT EXISTS`, which cannot add a column to an existing table.
Fine while the schema is new; fatal the first time it changes in production. A
hosted deployment needs PostgreSQL — `SQLAGENT_CONTROL_DATABASE_URL` already
takes one — and a real migration tool.

---

## Accuracy

Everything here is measured. See [docs/08-intent-design.md](docs/08-intent-design.md).

| # | Ticket | State |
|---|---|---|
| 70 | Benchmark the whole architecture, not just the base pipeline | **done** — fast 61.3%, medium 61.3%, thorough 57.3%; the ladder is worth nothing or less |
| 71 | The production glossary was contaminating every BIRD prompt | **done** — removing it: 58.7% -> 61.3%, faster and cheaper |
| 72 | `--tier` never offered `medium`, so a sweep silently skipped it | **done** — harness reads the tiers from the engine, test binds them |
| 73 | Gold annotations are 52.8% wrong; every number was against noise | **done** — `benchmarks/rescore.py`, no model calls; fast 72.3% -> 77.3% |
| 74 | Is the model the bottleneck? | **done** — no. gemma-31b 61.3% beats qwen-480b 56.0% on the same pipeline |
| 75 | `check_intent` — one call after execution, seeing question + SQL + rows | **in progress** — built, wired as Loop D behind `check_result_intent` (default off), 31 tests; the A/B has not been run |
| 76 | An unmatched literal must produce a question, never a dropped filter | open — the Cravings Deals failure, and checkable rather than guessable |
| 77 | Is BFS better than dumping the whole schema on a small database? | **run** — 1 hop 62.7%, whole schema 61.3%, 2 hops 60.0%; p = 0.688, needs the full 500 before the default moves |
| 78 | Score the full 500 on corrected gold, repeated | open — 119 matched by text with a ±7pp interval ranks nothing |
| 79 | Medium and thorough are sold and are worse than fast | open — the pricing charges for a negative |

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

**Not done, in order:**
1. Delete the critic once this replaces it — measured net 0, then net −6.
2. A/B against `fast` on corrected gold: **2 runs, ~900k tokens, ~5 min,
   ~$0.14. ASK FIRST** (see CLAUDE.md). The harness flag exists:
   `benchmarks/bird.py --intent`, and each outcome records
   `intent_rejections` and `intent_asked` so the asks can be counted rather
   than silently absorbed into the failures.

**Reminder for whoever picks this up:** `ask` scores as a failure in the
benchmark because a harness cannot answer it, so the measured number
*understates* the real behaviour. Report it that way rather than disabling
asking to flatter the score.

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
