# aperture-sql (Python)

A client for an [Aperture](https://github.com/jntbatra/aperture) server. Ask a
database a question in English and get back the answer, the SQL that produced
it, and the rows.

You need an Aperture server to talk to. Aperture is self-hosted: see
[Run your own server](https://github.com/jntbatra/aperture/blob/master/docs/self-hosting.md).

## Install

**Not on PyPI yet**, so `pip install aperture-sql` does not work today. Install it from
GitHub:

```bash
pip install "git+https://github.com/jntbatra/aperture#subdirectory=sdk/python"
```

Once it is published, `pip install aperture-sql` will work.

## Use it

```python
from aperture_sql import Aperture

with Aperture("http://127.0.0.1:8000", api_key="YOUR_KEY") as client:
    answer = client.ask("How many orders did we get last month?")
    print(answer.answer)        # "There were 1,204 orders last month."
    print(answer.sql)           # the query, so a person can check it
    print(answer.rows)          # [[1204]]

    # Follow up in the same thread.
    more = client.ask("and the month before?", conversation_id=answer.conversation_id)
```

`api_key` is one of the keys in the server's `SQLAGENT_API_KEYS`. It isn't needed
when the server runs with `SQLAGENT_AUTH_REQUIRED=false` (a laptop or a network
you already trust).

## Methods

| Method | What it does |
|---|---|
| `ask(question, tier=, conversation_id=, dataset_id=, ambiguity=)` | Answer a question. Returns an `Answer` even when `ok` is `False`. |
| `query(sql, dataset_id=)` | Run a statement again, read-only, with no model call. One `SELECT`, known tables, row cap and timeout. |
| `schema(dataset_id=)` | The tables and columns the agent can see. |
| `health(dataset_id=)` | Which database the server is pointed at. |
| `upload_dataset(path)` | Upload a CSV, Excel file or plain-SQL `pg_dump`. Returns the dataset; use its `id` as `dataset_id`. |
| `list_datasets()` / `delete_dataset(id)` | Manage your uploads. |

## Tiers

`tier="cheap"` (default) is about two model calls per question. `tier="expensive"`
adds a check that a model reads the rows and asks whether they answer the
question, rewriting the query once if not. Use `expensive` when someone will act
on the number. Measured on the BIRD benchmark, not on your database; see the
repository docs for the figures.

## Clarifying questions

By default the agent can ask instead of guessing when a question has two
reasonable readings. Then `answer.clarifications` is non-empty: show the options
and send the user's choice as the next question in the same `conversation_id`.
Pass `ambiguity="guess"` for batch jobs where nobody can reply.

## Errors

`ApertureError` has `.status` (the HTTP status, or `0` if no response arrived)
and `.body`. A refused statement in `query()` raises with status `400` and the
reason in the message.

## Safety

Every query, from the agent or from `query()`, goes through the same read-only
checks. Use a database role that only has `SELECT` anyway.

MIT licensed.
