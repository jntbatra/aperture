# 6. Running it

Setup, configuration, deployment, benchmarking and what to do when something
breaks.

---

## Requirements

| | Version | Why |
|---|---|---|
| Python | 3.12+ | Uses `X \| None` syntax and `zip(strict=)` |
| Docker | any recent | Only for the throwaway test database |
| Node | 20+ | Only for the web interface |
| AWS credentials | — | Bedrock access, region `us-east-1` by default |

---

## Setup

```bash
make install        # venv + dependencies
make db-up          # throwaway Postgres on port 5434
make db-seed        # example schema and sample data
make test           # 222 tests, should all pass
```

Then either:

```bash
.venv/bin/python -m sqlagent.cli "How many customers are there?"
```

or:

```bash
make api            # terminal 1
make web            # terminal 2 → http://localhost:5173
```

---

## Configuration

Every setting is an environment variable prefixed `SQLAGENT_`, or a line in
`.env`. Defined and documented in [`config.py`](../src/sqlagent/config.py).

### The ones you will actually change

```bash
# The database being analysed. MUST be a read-only role in production.
SQLAGENT_DATABASE_URL="postgresql+psycopg://readonly_user:secret@host:5432/mydb"

# Models. Verify availability first: python -m sqlagent.cli --models
SQLAGENT_LIGHT_MODEL="qwen.qwen3-coder-480b-a35b-instruct"
SQLAGENT_STRONG_MODEL="qwen.qwen3-coder-480b-a35b-instruct"
SQLAGENT_MANTLE_REGION="us-east-1"
```

Both tiers default to the same model because it measured fastest *and* most
accurate — see [07-decisions.md](07-decisions.md#model-selection). The split
exists for the case where a cheaper model is worth its accuracy loss.

### Safety limits

```bash
SQLAGENT_ROW_LIMIT=1000                 # max rows a query may return
SQLAGENT_STATEMENT_TIMEOUT_MS=30000     # database-enforced wall clock
SQLAGENT_MAX_REPAIR_ATTEMPTS=3          # retries before giving up
```

### Schema handling

```bash
SQLAGENT_FULL_SCHEMA_THRESHOLD=15   # at or below this many tables, send them all
SQLAGENT_INITIAL_HOPS=1             # graph walk distance on the first attempt
SQLAGENT_MAX_HOPS=3                 # ceiling when widening
```

`FULL_SCHEMA_THRESHOLD` is the one worth tuning. Below it, retrieval is skipped
entirely — one fewer model call and no chance of picking the wrong starting
table. Above it, retrieval is necessary because the schema will not fit.

Set it to `0` to always use retrieval, which is what the retrieval tests do.

### Uploads and history

```bash
SQLAGENT_DATA_DIR="./data"            # uploaded datasets + the history store
SQLAGENT_POSTGRES_ADMIN_URL=""        # needed only to restore uploaded pg dumps
SQLAGENT_HISTORY_LIMIT=100
```

CSV and Excel uploads become their own SQLite file under `data/datasets/`, so
each is isolated and deleting one is a single file removal. A PostgreSQL dump
needs a server to restore into: set `SQLAGENT_POSTGRES_ADMIN_URL` to a
connection with permission to `CREATE DATABASE`, and each dump gets its own.

Question history lives in `data/sqlagent.db`. **Result rows are deliberately
not stored** — a question like "list every customer" would otherwise turn the
log into a shadow copy of the database. The SQL is kept, since it is
reproducible and carries no data.

Uploaded CSVs and workbooks carry no foreign keys, so their tables cannot be
joined until a relationship is declared (`sqlagent.ingest.add_relationship`).
Nothing is inferred from column names: a wrong inferred join returns plausible
wrong rows rather than an error.

### Privacy

```bash
SQLAGENT_SAMPLE_ROWS=2         # example rows per table; 0 disables entirely
SQLAGENT_MAX_CELL_CHARS=100    # truncate long values
```

**Read this if the database holds personal data.** Sample rows are real rows.
They travel to the model provider and appear in anything that logs prompts.

Two rows per table materially improves accuracy — it is how the model learns
that `status` holds `'active'` rather than `1` — but if that trade is
unacceptable for your data, set `SAMPLE_ROWS=0`. The pipeline works without
samples; it is simply less informed.

For finer control, `sample_tables()` accepts `exclude_columns`, which omits
named columns from every sample while leaving the rest intact. Wiring that to a
configured list is a small change and the right one for a schema with a few
sensitive fields among many benign ones.

---

## Setting up a read-only role

The single most important piece of production configuration.

```sql
CREATE ROLE aperture_ro LOGIN PASSWORD 'choose-a-real-password';

GRANT CONNECT ON DATABASE mydb TO aperture_ro;
GRANT USAGE ON SCHEMA public TO aperture_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO aperture_ro;

-- Tables created later are not covered by the grant above.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO aperture_ro;

-- Explicitly remove the ability to create anything.
REVOKE CREATE ON SCHEMA public FROM aperture_ro;
```

To exclude sensitive tables, grant per-table instead of `ALL TABLES`.

The agent refuses writes in its own validator as well, and runs every query in
a read-only transaction. Those are defence in depth. This role is the boundary
that cannot be talked around by a cleverly-worded question.

---

## Which models can you actually use

Mantle's model list reports what it *hosts*, not what your account may
*invoke*. The only reliable test is to call each one:

```bash
python -m sqlagent.cli --models
```

```
  ✓ qwen.qwen3-coder-480b-a35b-instruct
  ✓ deepseek.v3.2
  ✓ zai.glm-5
  ✗ anthropic.claude-sonnet-5      not entitled
  ✗ openai.gpt-5.6-sol             not entitled
```

On the account this was built against, 10 of 54 listed models were callable.
Anthropic and GPT-5 models are listed but return `not available for this
account`.

---

## Benchmarking

```bash
make bench ARGS="--limit 40"                               # quick check
make bench ARGS="--limit 150 --workers 6"                  # meaningful sample
make bench ARGS="--full --out benchmarks/results/full.json" # all 500
make bench ARGS="--model deepseek.v3.2 --limit 150"        # compare a model
```

You need the BIRD mini-dev dataset, which is about 4 GB. Point `--data` at the
unpacked `MINIDEV` directory.

Output:

```
Accuracy           57.8%  (289/500)
Correct first try  56.6%
Needed a repair     3.8%
Wall clock         361s
Mean per question   2.6s
Tokens in/out      1,246,595 / 54,976

By difficulty:
  simple        70.3%  (104/148)
  moderate      55.6%  (139/250)
  challenging   45.1%  (46/102)
```

That is the full 500-question set. A `--limit 150` run lands within about a
point of it and takes a fifth of the time, which is usually the right trade
while iterating.

`--seed` fixes the sample, so two runs compare like with like. Change the code,
not the seed, when you want to know whether something improved.

Cost: roughly 355k tokens for 150 questions, 1.25M for the full 500.

---

## Deployment

### The API

```bash
uvicorn sqlagent.api.app:app --host 0.0.0.0 --port 8000 --workers 4
```

Each worker is a separate process with its own schema snapshot, reflected at
startup. That is fine — the snapshot is small — but it does mean four
reflections at boot.

Behind nginx, disable buffering for the streaming endpoint or SSE will not work:

```nginx
location /api/ask/stream {
    proxy_pass http://localhost:8000;
    proxy_buffering off;
    proxy_read_timeout 300s;
}
```

The app already sends `X-Accel-Buffering: no`, which nginx honours, but the
explicit configuration is clearer.

### The frontend

```bash
VITE_API_URL=https://api.example.com make web-build
```

Produces static files in `frontend/dist/`. Serve them from anywhere.

### Environment

At minimum: `SQLAGENT_DATABASE_URL` and AWS credentials. On EC2, ECS or EKS,
prefer an instance role or task role over static keys — the client fetches
credentials per request, so rotation works without a restart.

---

## Troubleshooting

### `pytest` fails with `ModuleNotFoundError: No module named 'osrf_pycommon'`

A ROS installation on the host puts its own site-packages on `PYTHONPATH`, and
pytest auto-loads plugins it finds there. One of them fails to import under
this project's Python.

Every `make` target clears `PYTHONPATH` for this reason. If running pytest
directly:

```bash
env PYTHONPATH= .venv/bin/python -m pytest
```

### `ModuleNotFoundError` right after a successful install

`uv pip install` installed into a different environment. Always pass the
interpreter explicitly:

```bash
uv pip install --python .venv/bin/python -e ".[dev]"
```

### Integration tests skip

No database on port 5434. `make db-up`, then `make db-seed`.

### `No AWS credentials found`

Configure `~/.aws/credentials`, or set `AWS_ACCESS_KEY_ID` and
`AWS_SECRET_ACCESS_KEY`, or attach an instance role. Verify with:

```bash
aws sts get-caller-identity
```

### `not available for this AWS account`

That model is listed but not entitled. Run `python -m sqlagent.cli --models`
and pick one that passes, or request access in the Bedrock console.

### Answers are wrong but the SQL runs

Usually a join inflating an aggregate. Open the SQL panel and look for a join
to a detail table (`order_items`, `line_items`) inside an aggregate over a
parent table. This class of error is reduced by good schema context and is not
eliminated — which is exactly why the SQL is shown.

### Everything is slow

Check `model_calls` in the response. Two is normal on a small schema, three
when retrieval runs. More means repairs are happening; the trace records what
failed. Rising repair rate over time is the signal that something has drifted —
a schema change, or a model version change upstream.
