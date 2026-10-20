# REST API

The server exposes everything the web app uses. All routes are under `/api`.
Interactive docs are at `/docs` on your server.

## Authentication

Send an API key as a bearer token:

```bash
curl -H "Authorization: Bearer $APERTURE_KEY" http://127.0.0.1:8000/api/schema
```

Keys come from the server's `SQLAGENT_API_KEYS`. A server started with
`SQLAGENT_AUTH_REQUIRED=false` needs no header. Browsers use a session cookie
from signing in instead. Only `/api/healthz` is open.

## Routes

| Method and path | Purpose |
|---|---|
| `POST /api/ask` | Ask a question. Body: `question`, optional `conversation_id`, `dataset_id`, `options`. |
| `GET /api/ask/stream` | The same, streamed as server-sent events with progress. |
| `POST /api/query` | Re-run a statement read-only, with no model call. Body: `sql`, optional `dataset_id`. |
| `GET /api/schema` | Tables and columns. Optional `?dataset_id=`. |
| `GET /api/schema/graph` | The foreign-key graph with layout. |
| `GET /api/health` | Which database the server is on and how many tables it sees. |
| `GET /api/healthz` | Liveness only. Open, for platform health checks. |
| `POST /api/datasets` | Upload a CSV, Excel file or plain-SQL `pg_dump` (multipart `file`). |
| `GET /api/datasets`, `DELETE /api/datasets/{id}` | Your uploads. |
| `GET/POST /api/conversations`, `GET/DELETE /api/conversations/{id}` | Chats and their turns. |
| `GET /api/search?q=` | Search past questions. |

Chats and uploads belong to the user or API key that made them. Another user's
id returns 404.

## Ask

```bash
curl -s -X POST http://127.0.0.1:8000/api/ask \
  -H "Authorization: Bearer $APERTURE_KEY" -H "Content-Type: application/json" \
  -d '{"question": "How many customers are there?"}'
```

```json
{
  "question": "How many customers are there?",
  "answer": "There are 3 customers.",
  "conversation_id": "9f2c1a7e0b4d6a35",
  "sql": "SELECT COUNT(*) AS customers FROM customers LIMIT 1000",
  "columns": ["customers"],
  "rows": [[3]],
  "row_count": 1,
  "truncated": false,
  "ok": true,
  "error": null,
  "warnings": []
}
```

Send `conversation_id` back to ask a follow-up ("and for April?"). `ok: false`
comes back with a 200 and an `error`; a clarifying question arrives in
`clarification_asks`. `warnings` mean the query ran but the number may not mean
what it looks like: show them.

## Re-run a query

```bash
curl -s -X POST http://127.0.0.1:8000/api/query \
  -H "Authorization: Bearer $APERTURE_KEY" -H "Content-Type: application/json" \
  -d '{"sql": "SELECT id, name FROM customers ORDER BY id"}'
```

The statement must be a single `SELECT` over known tables. It is rejected with
400 and a reason otherwise. It has a row cap and a time limit, and it is not saved
to your chat history.

## Clients

[Python](../sdk/python/README.md) (`pip install aperture-sql`) and
[TypeScript](../sdk/typescript/README.md) (`npm install aperture-sql`) wrap these
routes.
