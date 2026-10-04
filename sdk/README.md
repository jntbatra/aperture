# sql-agent SDK — integration guide

Ask a PostgreSQL database a question in English and get back an answer, the
SQL that produced it, and the rows. Two parts:

1. **The service**: the sql-agent Python API (`sqlagent.api.app`), run as its own
   process next to the backend. Setup is in [`deploy/EC2.md`](../deploy/EC2.md).
2. **The client**: `sdk/typescript/`, a zero-dependency typed client for Node 18+
   (CommonJS, ES2020; it suits a CommonJS / ES2020 TypeScript backend).

This file is written to be handed to whoever wires it into the OMS,
including another Claude session.

---

## Tiers

Two tiers, set per question. Measured on all 500 BIRD mini-dev questions
against corrected gold (`docs/10-the-ablation.md`), same model
(`google.gemma-4-31b`) in both:

| | `cheap` | `expensive` |
|---|---|---|
| Accuracy | 72.4% (8 runs) | 76.2% (3 runs) |
| Model calls / question | ~2.0 | ~3.1 |
| Tokens / question | ~3,000 | ~8,100 |
| Cost / question (standard) | ~$0.00045 | ~$0.00117 |
| Time / question | ~3–4s | ~5–6s |

`expensive` adds the result-intent check: after the query runs, a model reads
the rows and asks whether they answer the question, and the query is rewritten
once if not. Column documentation is used in both tiers if the database has
any (`COMMENT ON COLUMN`).

**These numbers are from BIRD, not your database.** A larger schema sends
larger prompts, so token counts will be higher there. Nothing has been
measured outside BIRD.

Suggested default: `cheap` for dashboards and anything high-volume, `expensive`
when someone is going to act on the number.

---

## Installing the client into your backend

The client is one file with no dependencies. Either:

- **Copy** `sdk/typescript/src/index.ts` into `<backend>/src/lib/sqlAgent.ts`
  and import from there. Simplest; nothing to publish.
- **Or** build and install the package:
  ```bash
  cd sdk/typescript && npm install && npm run build
  cd /path/to/backend && npm install /path/to/sql-agent/sdk/typescript
  ```

## Using it

```ts
import { SqlAgentClient, SqlAgentError } from "./lib/sqlAgent";

const agent = new SqlAgentClient({
  baseUrl: process.env.SQL_AGENT_URL ?? "http://127.0.0.1:8000",
  defaultTier: "cheap",
  timeoutMs: 60_000,
});

const a = await agent.ask("How many orders were delivered last week?", {
  tier: "expensive",            // or "cheap"
  conversationId,               // optional: from the previous answer, for follow-ups
  ambiguity: "ask",             // "ask" (default) may return clarifications; "guess" always answers
});

if (a.clarifications.length) {
  // The agent asked instead of guessing. Show the options; send the user's
  // choice as the next question with the same conversationId.
} else if (a.ok) {
  a.answer;      // "312 orders were delivered last week."
  a.sql;         // the query — show it to people who need to check the figure
  a.columns; a.rows; a.rowCount; a.truncated;
  a.warnings;    // ran fine but may be wrong — show these, do not drop them
} else {
  a.error;       // no query succeeded
}
a.usage;         // { tier, modelCalls, inputTokens, outputTokens, seconds, repairs }
a.conversationId // keep for the next question in the thread
```

`ask` resolves even when `ok` is false. It rejects with `SqlAgentError` only on
transport or HTTP errors (`status` 0 means timeout / unreachable).

## Wiring rules for the OMS

- **Do not expose the service to the internet.** It binds to `127.0.0.1`. Add an
  authenticated backend route (admin/ops roles only) that calls the client. The
  service itself has no login — whoever can reach it can query — so the OMS's
  own auth on that route is the only gate; the
  dashboard calls that route.
- **Do not let the browser pick arbitrary options.** Accept only
  `{ question, tier, conversationId }` from the dashboard; the client sends
  the rest.
- **Show the SQL and the warnings** next to the answer. Most wrong answers on
  this system run without error.
- **Timeouts**: the expensive tier on a large schema can take 10s+. Use a
  generous HTTP timeout on the backend route, or the streaming endpoint
  (`GET /api/ask/stream`, server-sent events) if the UI should show progress.
  The client does not wrap streaming yet.
- **Rate-limit per user** on the backend route. Each question costs model
  calls.
- **Per-question cost** is in `a.usage`. Log it if you want a spend view.

## Server endpoints the client uses

| | |
|---|---|
| `POST /api/ask` | `{ question, conversation_id?, options }` → answer. The client sends `options = { quality_tier: "fast", check_result_intent: <tier>, ambiguity_handling }`. |
| `GET /api/health` | liveness, table count, models |

## Tests

```bash
cd sdk/typescript && npm install && npm test   # fake fetch, no server, no model calls
```

Server side, the per-question tier is covered by
`tests/test_toggles.py::test_the_intent_check_can_be_turned_on_for_one_question`.
