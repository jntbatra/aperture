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

## Open

| # | Ticket | State |
|---|---|---|
| 25 | Multi-agent Manager + Analyst/Research/Compute | **won't build** — see below |
| 26 | Sandboxed code interpreter | **won't build** — see below |
| 27 | Research agent / web search | **won't build** — see below |
| 28 | Conversation summarisation beyond the 4-turn window | open |
| 29 | Drift detection | open |
| 30 | LLM-as-judge for answer quality | open |
| 34 | Ambiguity check is nondeterministic and unmeasured | open |
| 35 | Nothing committed to git | open |
| 39 | A query can answer a different question than the one asked | open |
