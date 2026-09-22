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

**Bring your own data.** Upload a CSV, an Excel workbook or a PostgreSQL dump
and ask questions about it immediately. Every question is searchable, alongside
the SQL it produced and the conversation it came from.

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
no tool that accepts raw SQL.

---

## Quick start

You need Python 3.12+, Docker (for the test database), Node 20+ (for the web
interface), and AWS credentials with Bedrock access.

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

# 5. Or run the web interface
make api      # terminal 1 — http://localhost:8000
make web      # terminal 2 — http://localhost:5173
```

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
| — | [Aperture as a service](SAAS.md) | The five fronts, multi-tenancy, tiers, and what a hosted deployment needs |
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
│   │   └── mantle.py        Amazon Bedrock Mantle client, SigV4-signed
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
│   │   └── app.py           FastAPI: JSON and Server-Sent Events
│   ├── ingest.py            CSV / Excel / pg-dump upload → queryable database
│   ├── store.py             History, conversations and dataset registry (SQLite)
│   ├── conversation.py      Turns carried into the prompt so follow-ups resolve
│   ├── summarise.py         Fold turns past the window into a standing note
│   ├── judge.py             Grade a finished answer, offline — is it the answer asked for
│   ├── drift.py             Did recent questions go differently from earlier ones
│   ├── glossary.py          Units, terms and metrics the schema cannot express
│   ├── cache.py             Question → SQL. The statement, never the rows
│   ├── voting.py            Sample N times, keep the query that recurs (opt-in)
│   ├── clarify.py           Ask, when a question has two defensible answers (opt-in)
│   ├── report.py            Split a multi-part question, answer each (opt-in)
│   ├── suggest.py           Opening questions for an empty screen, from the schema
│   ├── config.py            Settings, validated at startup; per-request overrides
│   ├── mcp_server.py        Offer the agent as MCP tools to another assistant
│   ├── prompts.py           Every prompt, version-controlled
│   ├── agent_graph.py       LangGraph state graph: nodes, edges, both repair loops
│   ├── pipeline.py          SqlAgent public surface, the trace, the individual steps
│   ├── cli.py               Terminal interface
│   └── saas/                Who is asking, and whose data they may reach
│       ├── tenancy.py           Tenant, ApiKey, Principal; keys stored hashed
│       ├── passwords.py         scrypt — deliberately not the API-key hash
│       ├── secrets.py           Fernet for tenant connection strings
│       ├── connect.py           Prove a role cannot write, before saving it
│       ├── plans.py             Tiers as data: limits, budgets, clamping
│       ├── auth.py              One resolution path for all five fronts
│       └── control.py           Accounts and entitlements, a separate database
├── frontend/            React + TypeScript web interface
│                        (schema graph drawn with React Flow)
├── benchmarks/
│   ├── bird.py              BIRD mini-dev harness (--judge grades the prose too)
│   ├── ambiguity.py         How often does the ambiguity check agree with itself
│   └── ambiguity_questions.json  30 labelled questions, 15 vague / 15 clear
├── glossaries/
│   └── example.jsonDeclared units and terms for one real database
├── tests/               487 tests
└── docs/                The documentation table above
```

---

## Current measurements

Against the **full BIRD mini-dev set — all 500 questions**, using
`qwen.qwen3-coder-480b-a35b-instruct`:

| Metric | Value |
|---|---|
| Execution accuracy | **57.8%** (289/500) |
| Correct on first attempt | 56.6% |
| Needed a repair | 3.8% |
| Mean time per question | 2.6s |
| Model calls per question | 2 |
| simple / moderate / challenging | 70.3% / 55.6% / 45.1% |

Published BIRD results place competent systems in the 40–60% band; human
performance is about 92%. The questions are ambiguous, the schemas are real and
messy, and many answers need domain knowledge supplied as a separate evidence
note.

### Against the previous implementation

The same 500 questions, run through both pipelines:

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

Sample rows are real customer data and travel to the model provider. They can
be restricted per column or switched off entirely — see
[docs/06-operations.md](docs/06-operations.md#privacy).
