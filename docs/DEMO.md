# Demo script

Questions rehearsed against the tiffinwala database. Run them in this order; each one shows
something the previous did not.

## Before you start

```bash
cd backend && aperture profile          # warms the schema cache, proves the connection
aperture usage                          # shows spend so far
```

The first question after a cold start pays for schema introspection (~0.15s) and a model round trip.
Ask one throwaway question before the audience is watching.

## 1. It works, and it shows its work

> **How many orders were delivered each month?**

Watch the timeline: linking, writing SQL, validating, cost, execute, narrate, chart. Point out that
the SQL is always shown, and that `status = 'DELIVERED'` came from the database's observed values,
not from the model guessing `'completed'`.

## 2. It knows what the words mean

> **What was our revenue last month?**

The semantic layer supplies the definition: delivered orders only, amounts stored in paise. Show
`backend/aperture/semantic/semantic.yaml` — the point is that two people asking the same question get
the same number, because the definition is reviewed rather than inferred.

## 3. It repairs identifiers deterministically

> **Average time from order creation to delivery for each kitchen**

The timeline shows `repaired N identifiers`. Explain: PostgreSQL folds bare `createdAt` to
`createdat`; rather than spend a repair attempt rediscovering the spelling, the AST is corrected from
the schema. Before this existed, this exact question exhausted both attempts and failed.

## 4. It refuses writes, twice over

> **Delete all cancelled orders**

Refused by the validator. Then show the layer underneath, which does not depend on any prompt:

```bash
PGPASSWORD=... psql -h localhost -p 5433 -U aperture_ro tiffinwala \
  -c "delete from orders where 1=0"
# ERROR: cannot execute DELETE in a read-only transaction
```

## 5. It explains empty results instead of saying "0"

> **Total refunds issued last month**

Answer: 0 — *because the refunds table contains no data at all. This is a property of the database,
not of the query.* No model call was needed to work that out.

> **How many orders were placed in January 2019?**

Answer: 0 — *the date filter falls outside the data; `orders."createdAt"` covers 2026-05-09 to
2026-09-17.*

## 6. It warns about joins that silently lie

```bash
aperture link "orders and their status history"
```

Shows: `order_status_history holds ~5.6x the rows of orders; joining them multiplies orders rows`.
A legal foreign-key join that inflates every `SUM` and raises no error.

## 7. Anything can use it

```bash
aperture mcp          # same guardrails, over MCP, for any agent or IDE
curl localhost:8000/ask -d '{"question":"..."}'   # SSE stream of the same loop
```

## If something goes wrong

- Model unreachable: `aperture usage` confirms credentials and spend; the answer path needs Bedrock,
  but `aperture profile` and `aperture link` work offline and still demo the retrieval story.
- Wrong or empty answer: that is the point of showing the SQL — read it aloud and say what you would
  fix. The assumptions line usually reveals the disagreement.
