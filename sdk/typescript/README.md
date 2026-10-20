# aperture-sql (TypeScript / JavaScript)

A typed client for an [Aperture](https://github.com/jntbatra/aperture) server.
Ask a database a question in English and get back the answer, the SQL that
produced it, and the rows. No runtime dependencies; Node 18+ (or any runtime
with `fetch`).

You need an Aperture server to talk to. Aperture is self-hosted: see
[Run your own server](https://github.com/jntbatra/aperture/blob/master/docs/self-hosting.md).

```bash
npm install aperture-sql
```

## Use it

```ts
import { ApertureClient } from "aperture-sql";

const client = new ApertureClient({
  baseUrl: "http://127.0.0.1:8000",
  apiKey: process.env.APERTURE_API_KEY,
});

const answer = await client.ask("How many orders did we get last month?");
console.log(answer.answer); // "There were 1,204 orders last month."
console.log(answer.sql);    // the query, so a person can check it
console.log(answer.rows);   // [[1204]]

// Follow up in the same thread.
const more = await client.ask("and the month before?", {
  conversationId: answer.conversationId ?? undefined,
});
```

`apiKey` is one of the keys in the server's `SQLAGENT_API_KEYS`. It isn't needed
when the server runs with `SQLAGENT_AUTH_REQUIRED=false` (a laptop or a network
you already trust). `SqlAgentClient` is the same class under its older name.

## Methods

| Method | What it does |
|---|---|
| `ask(question, { tier, conversationId, datasetId, ambiguity, signal })` | Answer a question. Resolves even when `ok` is `false`; rejects on HTTP or network errors. |
| `query(sql, { datasetId })` | Run a statement again, read-only, with no model call. One `SELECT`, known tables, row cap and timeout. |
| `schema({ datasetId })` | The tables and columns the agent can see. |
| `health()` | Which database the server is pointed at. |

## Tiers

`tier: "cheap"` (default) is about two model calls per question. `"expensive"`
adds a check that a model reads the rows and asks whether they answer the
question, rewriting the query once if not. Use `expensive` when someone will act
on the number. Measured on the BIRD benchmark, not on your database.

## Clarifying questions

By default the agent can ask instead of guessing when a question has two
reasonable readings. Then `answer.clarifications` is non-empty: show the options
and send the user's choice as the next question with the same `conversationId`.
Pass `ambiguity: "guess"` for batch jobs where nobody can reply.

## Errors

`SqlAgentError` has `.status` (the HTTP status, or `0` if no response arrived)
and `.body`. A refused statement in `query()` rejects with status `400` and the
reason in the message.

MIT licensed.
