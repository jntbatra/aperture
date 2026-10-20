# Aperture — a conversational SQL analyst

Ask a database questions in plain English. Get an answer, plus the SQL that
produced it.

```
You:  What is the total order value per region, highest first?

      Finding the relevant tables    ✓
      Writing SQL                    ✓
      Checking the query is safe     ✓
      Running the query              ✓

SQL:  SELECT r.name, SUM(o.total) AS total
      FROM orders o
      JOIN customers c ON o.customer_id = c.id
      JOIN regions r ON c.region_id = r.id
      GROUP BY r.name
      ORDER BY total DESC

      The United Kingdom leads with £400.00, followed by Germany at £300.00.
```

Self-hosted and MIT licensed: you run the server against your own database and
model account. There is a web app, a REST API, Python and TypeScript SDKs, an MCP
server and a CLI. See [Ways to use it](#ways-to-use-it).

---

## What makes this different from "just ask ChatGPT for SQL"

Four things, and each one is a whole subsystem:

**It knows your schema without being told.** It reads the database's own
catalogue, builds a graph of which tables link to which, and puts only the
relevant corner of that graph into the prompt. Measured on a real **75-table,
87-foreign-key** schema, finding the right two or three tables costs **0.7
percentage points** against being handed them directly — and only 1 of 66
failures came from retrieval missing something.

**It cannot damage your data.** Three independent layers stop writes: a
read-only database role, a SQL parser that rejects anything that is not a
`SELECT`, and a read-only transaction. Any one of them is sufficient; all three
are present because configuration drifts.

**It fixes its own mistakes.** When a query fails, the error is classified and
fed back. A syntax error gets a rewrite; a missing table gets *more schema* and
then a rewrite. Those are different problems, and they are two different
conditional edges in the LangGraph state graph that orchestrates a request.

**It is measured, not assumed.** A benchmark harness runs the agent against
BIRD mini-dev — 500 human-written questions over 11 real databases — and scores
it by comparing result sets against gold queries. Three more instruments sit
beside it: an **answer judge** that grades the prose execution accuracy cannot
see (right rows, wrong write-up), a **stability harness** that asks the same
question five times to find out how often the ambiguity check agrees with
itself, and **drift detection** comparing recent questions against earlier ones
with a p-value attached.

**It says when something changed, and what it cannot know.** The Drift panel
compares the last hundred questions against the ones before them across six
rates, reporting a shift only when it is both significant and large enough to
act on. It is careful about the claim: nothing in the history says whether an
answer was *right*, so a shift is a prompt to go and look, never a verdict.
Pointed at this project's own history it found a real bug in its first run — a
clarifying question was being counted as a failure, which had the interface
reporting 78% answered on a system where almost nothing had failed.

**It holds a conversation, and remembers past the window.** Ask "How many
orders were delivered?", then "break that down by month" — the second is not a
question about a database on its own, and it is answered anyway. Recent turns
are carried as their question plus the SQL they produced; older ones are folded
into a standing note of what still applies. Measured on the live database:
across five turns with a two-turn window, "only DELIVERED orders, exclude test
accounts" set in turn 1 still constrains turn 5. With the note switched off,
both filters are gone and the figure is **37% higher** — an
overstatement, compared against the earlier constrained number as though the
two were the same measurement. Threads live server-side, so a reload does not
lose one.

**It checks its own answer.** The query running is not the same as the answer
being true. A separate guard compares the sentence against the rows it claims to
describe, and regenerates it when it names a value the results do not contain —
a real failure this caught on a real database, where a one-row result naming one
item was reported as a different item entirely.

**It can be told what the schema cannot say.** A glossary declares units, terms
and metrics: *`order_items.price` is in paise*, *"ordered" means `order_items`,
never `cart_items`*. Without it, revenue was reported 100× too large, in the
wrong currency, and no amount of prompting fixes that — the information is
simply absent from the schema.

**Bring your own data.** Upload a CSV, an Excel workbook or a plain-SQL
PostgreSQL dump and ask questions about it immediately. A dump is converted to
SQLite with its tables, rows and keys, so no Postgres server is needed. Every
question is searchable, alongside the SQL it produced and the conversation it
came from, and each answer's SQL has a Run button that re-runs it read-only.
Chats and uploads belong to the person who made them.

**You choose how hard it tries, per question.** A tuning panel on the homepage
exposes the quality toggles — review the query with a second model, write it
three times and keep what recurs, ask when a question is ambiguous, how many
things it may ask about at once, how many turns it remembers in full. Each
states what it costs, because a toggle offered without a price gets switched on
by everyone. The list, the help text and even which choices are numbers come
from the server, so a toggle added there appears here without a client change.
Measured on a real database: 3 model calls by default, 6 on `thorough`.

**Other assistants can use it.** An MCP server offers three tools —
`ask_database`, `list_tables`, `describe_table` — so another agent can ask this
one questions without ever holding database credentials. There is deliberately
no tool that accepts raw SQL. Over HTTP it requires an API key.

---

## Quick start

You need Python 3.12+, Docker (for the test database), Node 20+ (for the web
interface), and access to a model: AWS credentials with Bedrock access, or a
Bedrock API key (`SQLAGENT_LLM_AUTH=bearer`, see
[self-hosting](docs/self-hosting.md)).

```bash
# 1. Install
make install

# 2. Start a database with sample data
make db-up
.venv/bin/python -c "
from sqlalchemy import create_engine
from pathlib import Path
e = create_engine('postgresql+psycopg://testuser:testpass@localhost:5434/testdb')
with e.begin() as c:
    c.exec_driver_sql(Path('tests/fixtures/schema.sql').read_text())
    c.exec_driver_sql(Path('tests/fixtures/seed.sql').read_text())
"

# 3. Check everything works
make test

# 4. Ask a question from the terminal
.venv/bin/python -m sqlagent.cli "How many customers are there?"

# 5. Or run the web interface (add SQLAGENT_AUTH_REQUIRED=false to .env first;
#    without it the API asks for sign-in, which needs the setup in docs/self-hosting.md)
make api      # terminal 1 — http://localhost:8000
make web      # terminal 2 — http://localhost:5173
```

---

## Ways to use it

Aperture is self-hosted: you run the server against your own database and model
account ([run your own](docs/self-hosting.md)). Then there are these ways in.

| Way | What it is | Docs |
|---|---|---|
| **Web app** | Chat, history, uploads, schema graph. Sign-in with owner approval, or open on a trusted machine. | [self-hosting](docs/self-hosting.md) |
| **REST API** | The routes the web app uses, with API-key auth. | [docs/api.md](docs/api.md) |
| **Python SDK** | `pip install aperture-sql` | [sdk/python](sdk/python/README.md) |
| **TypeScript SDK** | `npm install aperture-sql` | [sdk/typescript](sdk/typescript/README.md) |
| **MCP server** | Lets Claude Desktop or any MCP client ask your database. | [docs/mcp.md](docs/mcp.md) |
| **CLI** | `python -m sqlagent.cli "How many customers?"` | below |

```python
from aperture_sql import Aperture

client = Aperture("http://127.0.0.1:8000", api_key="YOUR_KEY")
answer = client.ask("How many customers are there?")
print(answer.answer, answer.sql, answer.rows)
```

The CLI reads the same `.env` as the server and asks the database directly, with no
server running. `--schema` prints the structure and `-q` hides progress.

---

## Documentation

Written for someone who has not used these technologies before. Start at the
top and work down.

| # | Document | What it covers |
|---|---|---|
| 1 | [What this is and why](docs/01-what-and-why.md) | The problem, the shape of the solution, why text-to-SQL is hard |
| 2 | [Every technology, from scratch](docs/02-technologies.md) | Python tooling, SQLAlchemy, NetworkX, sqlglot, Pydantic, LangGraph, FastAPI (for Express developers), React, Bedrock Mantle, SigV4 |
| 3 | [Architecture](docs/03-architecture.md) | How the pieces fit, the request lifecycle, both repair loops |
| 4 | [Code walkthrough](docs/04-code-walkthrough.md) | Every module, what it does and why it is written that way |
| 5 | [The web interface](docs/05-frontend.md) | React app, streaming, the design system |
| 6 | [Running it](docs/06-operations.md) | Configuration, deployment, benchmarking, troubleshooting |
| 7 | [Decisions and measurements](docs/07-decisions.md) | Every significant choice, what it cost, what it bought |
| 8 | [Where the accuracy goes](docs/08-intent-design.md) | What `fast` really is, what the measurements rule out, and the one change the evidence points at |
| — | [Run your own server](docs/self-hosting.md) | Laptop, Docker and server setups; the three access modes; limits and costs |
| — | [REST API](docs/api.md) | Every route, API-key auth, curl examples |
| — | [MCP server](docs/mcp.md) | Tools, stdio for Claude Desktop, HTTP with a key |
| — | [SDKs](sdk/README.md) | Python and TypeScript clients, and [how they are released](sdk/RELEASING.md) |
| — | [Deploying on Render + Neon](deploy/RENDER.md) | The setup this project itself runs |
| — | [Aperture as a service](SAAS.md) | The hosted-product plan. Accounts and plans were removed (2026-10-04); sign-in with owner approval and API keys came back later, see [self-hosting](docs/self-hosting.md) |
| — | [Tickets](TICKETS.md) | Every decision as a ticket with its real status: done, open, or won't-build with the reason |

---

## Project layout

```
sql-agent/
├── src/sqlagent/
│   ├── schema/          Read the database structure, model it as a graph
│   │   ├── introspect.py    Reflection → immutable snapshot + version hash
│   │   ├── graph.py         Tables as nodes, foreign keys as edges
│   │   └── retrieval.py     Bounded breadth-first walk to pick relevant tables
│   ├── llm/
│   │   └── mantle.py        Amazon Bedrock Mantle client: SigV4, Bedrock token or bearer
│   ├── guards/
│   │   ├── validator.py     Prove a statement is a safe, read-only SELECT
│   │   ├── cost.py          Ask the planner what a query costs, before running it
│   │   ├── arithmetic.py    Catch aggregates a join may have multiplied
│   │   ├── critic.py        Loop C: does this query answer the question? (opt-in)
│   │   ├── prescreen.py     Screen the question itself (opt-in)
│   │   └── faithfulness.py  Prove the answer says what the rows say
│   ├── db/
│   │   ├── dialects.py      Per-database differences in one place
│   │   ├── execute.py       Run a query under time and row limits
│   │   ├── sample.py        A couple of example rows, to show real value formats
│   │   └── profile.py       Per-column value profiles (opt-in; measured, see docs/07)
│   ├── api/
│   │   ├── app.py           FastAPI: JSON and Server-Sent Events, serves the built web app
│   │   └── auth_routes.py   Sign-in session, owner approval page, API-key gate
│   ├── auth.py              Neon Auth token check, approvals, sessions
│   ├── objectstore.py       Uploaded originals in S3-compatible storage (Neon)
│   ├── ingest.py            CSV / Excel / pg-dump upload → queryable database
│   ├── pgdump.py            Plain-SQL pg_dump → SQLite, no server needed
│   ├── store.py             Chats and history per user (Neon Postgres or SQLite); dataset registry
│   ├── conversation.py      Turns carried into the prompt so follow-ups resolve
│   ├── summarise.py         Fold turns past the window into a standing note
│   ├── judge.py             Grade a finished answer, offline — is it the answer asked for
│   ├── drift.py             Did recent questions go differently from earlier ones
│   ├── glossary.py          Units, terms and metrics the schema cannot express
│   ├── voting.py            Sample N times, keep the query that recurs (opt-in)
│   ├── clarify.py           Ask, when a question has two defensible answers (opt-in)
│   ├── report.py            Split a multi-part question, answer each (opt-in)
│   ├── suggest.py           Opening questions for an empty screen, from the schema
│   ├── config.py            Settings, validated at startup; per-request overrides
│   ├── mcp_server.py        Offer the agent as MCP tools to another assistant
│   ├── prompts.py           Every prompt, version-controlled
│   ├── agent_graph.py       LangGraph state graph: nodes, edges, both repair loops
│   ├── pipeline.py          SqlAgent public surface, the trace, the individual steps
│   └── cli.py               Terminal interface
├── frontend/            React + TypeScript web interface
│                        (schema graph drawn with React Flow)
├── sdk/
│   ├── python/              aperture-sql for Python
│   └── typescript/          aperture-sql for TypeScript / JavaScript
├── deploy/                  Render + Neon deployment guide
├── Dockerfile, render.yaml  One container: API and built web app
├── neon.ts                  Neon services: auth, object storage, functions
├── benchmarks/
│   ├── bird.py              BIRD mini-dev harness (--judge grades the prose too)
│   ├── ambiguity.py         How often does the ambiguity check agree with itself
│   └── ambiguity_questions.json  30 labelled questions, 15 vague / 15 clear
├── glossaries/
│   └── example.json         Declared units and terms for one real database
├── tests/               828 tests (757 run without a database)
└── docs/                The documentation table above
```

---

## Current measurements

The full BIRD mini-dev set, all 500 questions, `google.gemma-4-31b`, scored
against **corrected gold** (`arcwise_plat_sql.json`; BIRD's own gold SQL is
often wrong — see `benchmarks/CORRECTED_GOLD.md`). Details in
[docs/10](docs/10-the-ablation.md).

| | |
|---|---|
| Execution accuracy (corrected gold, n=415) | **72.3%** (300/415), 2 model calls, 3.2s per question |
| Same run on original BIRD gold, all 500 | 63.0% (315/500) |
| Baseline across 8 repeat runs | 72.4% mean, sd 0.56 |
| + result-intent check + column docs | **76.2%** mean over 3 runs, +16.0 net, median p=0.015 — one extra call |

Published BIRD results place competent systems in the 40–60% band; human
performance is about 92%. The questions are ambiguous, the schemas are real and
messy, and many answers need domain knowledge supplied as a separate evidence
note.

### Against the previous implementation

An earlier comparison, scored on original BIRD gold before the switch to
gemma-4-31b and corrected gold. The same 500 questions, run through both
pipelines:

| | Accuracy | Mean latency |
|---|---|---|
| **sql-agent** (`qwen3-coder-480b`) | **57.6%** | **2.61s** |
| Aperture (`qwen3-coder-next`) | 53.0% | 7.51s |

+4.6 points (p = 0.022; 58 questions won, 35 lost) and **2.9× faster**. Better
on every difficulty tier.

One caveat worth stating: the two runs use different models, so this is not a
clean measure of the pipeline alone. A same-model comparison on 150 questions
put the pipeline's own contribution at +3.4 points, which was *not* significant
(p = 0.458). Most of the gain above is model choice — which is itself a result,
since choosing it was the only intervention all session that moved the number.

See [docs/07-decisions.md](docs/07-decisions.md) for the two bugs that mattered,
and for four further interventions that measured null.

---

## Safety posture

The agent is designed on the assumption that the model will eventually produce
something wrong or dangerous, because it will.

| Layer | Stops | Enforced by |
|---|---|---|
| Read-only database role | All writes | The database |
| SQL parse + statement-type check | `DELETE`, `DROP`, multi-statement injection, writes hidden in CTEs | sqlglot, in-process |
| Table allow-list | Queries reaching tables the model was never shown | The validator |
| Forbidden-function list | Filesystem reads, `dblink`, `pg_sleep` | The validator |
| Row cap | Unbounded result sets exhausting memory | Injected `LIMIT` + a bounded fetch |
| Statement timeout | Queries that scan too much | The database |
| Read-only transaction | Any write that got past everything above | The database |
| Sign-in with owner approval, or API keys | Strangers using the server and spending the model bill | `auth.py`, `api/auth_routes.py` |
| Per-user ownership | One person reading another's chats or uploads (a stranger's id returns 404) | `store.py`, checked on every route |
| Same gates on re-run | The Run button being a way around the validator | `POST /api/query` runs the validator, cost check, row cap and timeout |

With `SQLAGENT_AUTH_REQUIRED=false` the access rows do not apply: anyone who can
reach the server can use it. That is for a laptop or a trusted network.

Sample rows are real customer data and travel to the model provider. They can
be restricted per column or switched off entirely — see
[docs/06-operations.md](docs/06-operations.md#privacy).

---

## License

MIT. See [LICENSE](LICENSE).
