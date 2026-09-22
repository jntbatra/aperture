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

## Open

| # | Ticket | State |
|---|---|---|
| 34 | Ambiguity check is nondeterministic | **instrument built, not run** |
| 39 | A query can answer a different question than the one asked | **instrument built, not run** |
| 42 | `ruff format --check` fails repo-wide | open |

### 34 — ambiguity stability

`benchmarks/ambiguity.py` asks the same question *k* times and reports how
often the clarifier agrees with itself, alongside a labelled 30-question set
(15 vague, 15 clear) in `benchmarks/ambiguity_questions.json`. Stability is the
number that matters: it needs no ground truth and cannot be argued with, unlike
the accuracy figure beside it.

30 questions x k=5 is 150 model calls and no database work.

**Not run** — benchmark runs are paused by request. The aggregation is tested
(`tests/test_ambiguity_harness.py`, 17 tests); the model calls are not.

### 39 — answering a different question

`judge.py` detects this offline, and `--judge` reports "correct but badly
answered" — right rows, wrong write-up — which nothing else in the harness can
surface. The open decision is whether the critic should default to on, and that
needs a measured run with and without it.

**Not run**, same reason.

### What running the drift detector found

Pointed at this project's own history on its first run, it reported the failure
rate rising from **1.6% to 35%**. Thirty-one of those thirty-five "failures"
were the agent asking a clarifying question — doing exactly what switching
`ambiguity_handling` to `ask_human` configured it to do. The metric was
measuring a settings change and calling it a regression.

Two things were wrong, both now fixed (#43, #44): a clarification is stored
with `ok = 0` because no answer was produced, and three separate places
compared against the string `"needs_clarification"` by hand. The sidebar had
the same bug and was reporting **78% answered** on a system where almost
nothing had failed.

Corrected, the same window reads:

| | baseline (62) | recent (100) |
|---|---|---|
| failure_rate | 1.6% | 4.0% — not significant |
| clarify_rate | 0.0% | **31.0%** (p < 0.0001) |
| slow_rate | 9.7% | **37.0%** (p = 0.0001) |
| costly_rate | 9.7% | **33.0%** (p = 0.0007) |

All three shifts are explained: `ask_human` became the default, and the model
changed to `gemma-4-31b` with a glossary in every prompt. That is the intended
use — the detector says *something changed*, and a human says what.

### 42 — repo-wide formatting

`make lint` runs `ruff check` (clean) and `ruff format --check`, which fails on
40 files. This is pre-existing and includes files untouched in this pass; the
code is hand-formatted and `ruff format` would reflow a lot of deliberately
laid-out prose and comments. Fixing it means either accepting a large
whitespace diff or dropping the format check. Not decided.

---

## Won't build, and why

| # | Ticket | Why not |
|---|---|---|
| 25 | Multi-agent Manager + Analyst/Research/Compute | The pipeline is engineer-planned: the steps a SQL question needs are known in advance and encoded as a graph. A manager would re-derive that plan on every question, at a model call each, and get it wrong sometimes. Multi-agent buys flexibility this problem does not need, and pays for it in the property that matters most here — that the SQL is readable next to the answer. |
| 26 | Sandboxed code interpreter | A separate security project, not a feature. The whole safety argument of this system is that the only thing reaching the database is a statement proven to be a SELECT by a parser. Arbitrary Python alongside it makes every one of those guarantees conditional on the sandbox instead. |
| 27 | Research agent / web search | Every answer here traces to a row in a named table, which is why the SQL is shown. A retrieved web page does not, and an answer mixing the two is exactly as checkable as its weakest source while looking uniformly authoritative. |

These are decisions, not a backlog. Reopening one means arguing with the reason
above, which is the point of writing it down.
