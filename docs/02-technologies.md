# 2. Every technology, from scratch

Written on the assumption that you have not used any of these before. Each
section says what the thing is, why this project uses it, what it replaced, and
where to look in the code.

**Contents**

- [Python tooling: uv, venv, packaging](#python-tooling)
- [Type hints and dataclasses](#type-hints-and-dataclasses)
- [Pydantic and pydantic-settings](#pydantic)
- [SQLAlchemy](#sqlalchemy)
- [NetworkX](#networkx)
- [sqlglot](#sqlglot)
- [pytest](#pytest)
- [FastAPI — for people who know Express](#fastapi)
- [Server-Sent Events](#server-sent-events)
- [React, TypeScript and Vite](#react-typescript-and-vite)
- [Amazon Bedrock and Mantle](#amazon-bedrock-and-mantle)
- [AWS SigV4 request signing](#sigv4)
- [LangChain and LangGraph](#langchain-and-langgraph)

---

## Python tooling

### Virtual environments

Python installs packages globally by default, so two projects wanting different
versions of the same library conflict. A **virtual environment** is a folder
containing its own Python and its own packages.

```bash
uv venv --python 3.12       # creates .venv/
.venv/bin/python script.py  # this Python sees only this project's packages
```

`.venv/` is in `.gitignore` — it is generated, never committed. The equivalent
in Node is `node_modules/`.

### uv

`uv` is a fast replacement for `pip`. Same job, roughly 10–100× quicker.

```bash
uv pip install --python .venv/bin/python -e ".[dev]"
```

- `-e` — "editable" install: the package is linked, not copied, so editing
  `src/sqlagent/pipeline.py` takes effect immediately with no reinstall.
- `".[dev]"` — install this project plus its `dev` extra (pytest, ruff).

> **A trap worth knowing.** `uv pip install` without `--python` may install into
> a different environment than you expect. This project hit exactly that during
> setup: the install reported success and the import still failed. Always pass
> `--python .venv/bin/python`, which is what the `Makefile` does.

### pyproject.toml

One file describing the package, its dependencies, and its tools' settings —
the rough equivalent of `package.json`.

```toml
[project]
name = "sqlagent"
requires-python = ">=3.12"
dependencies = ["sqlalchemy>=2.0.36", ...]

[project.optional-dependencies]
dev = ["pytest>=8.3.4", "ruff>=0.8.4"]
```

### The src/ layout

Code lives in `src/sqlagent/` rather than `sqlagent/`. The reason is subtle but
real: with a bare `sqlagent/` directory at the repository root, running
`pytest` from that root would import the *directory*, not the installed
package. Tests would then pass against files that were never packaged, and
fail the moment someone installed the project properly. `src/` makes that
mistake impossible.

### ruff

Linter and formatter in one. Catches unused imports, undefined names,
unnecessarily nested `if` statements, lines over 100 characters.

```bash
make lint   # check
make fmt    # fix what can be fixed automatically
```

---

## Type hints and dataclasses

### Type hints

Python does not enforce types at runtime, but annotating them lets editors and
tools catch mistakes before you run anything.

```python
def expand(graph: nx.DiGraph, seeds: list[str], *, max_hops: int = 1) -> Neighbourhood:
```

That signature says: takes a graph, a list of strings, and a keyword-only
integer; returns a `Neighbourhood`.

The `*` matters. Everything after it **must** be passed by name:

```python
expand(graph, ["orders"], max_hops=2)   # fine
expand(graph, ["orders"], 2)            # TypeError
```

This is used throughout for options. `execute(engine, sql, 1000, 30000)` is
unreadable at the call site and easy to get backwards;
`execute(engine, sql, row_limit=1000, statement_timeout_ms=30_000)` cannot be.

`X | None` means "an X, or None" — like `X | null` in TypeScript.

### Dataclasses

A dataclass turns a class declaration into a real class with a constructor,
comparison and a readable `repr`, without boilerplate:

```python
@dataclass(frozen=True, slots=True)
class Column:
    name: str
    type: str
    nullable: bool
    primary_key: bool
```

- `frozen=True` — immutable. Assigning to a field raises. This matters here
  because a `SchemaSnapshot` is cached and shared across requests; accidental
  mutation in one request would corrupt every later one.
- `slots=True` — a smaller, faster object that cannot grow new attributes. A
  typo like `column.nulable = True` raises instead of silently creating a
  second, unused attribute.

---

## Pydantic

Dataclasses describe shapes. **Pydantic** validates and coerces them at the
boundary where data arrives from outside the program.

```python
class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
```

A request whose body has no `question`, or a 10,000-character one, is rejected
before the handler runs. If you know **zod**, this is the same idea, built into
the web framework.

### pydantic-settings

The same validation applied to environment variables:

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SQLAGENT_", env_file=".env")

    row_limit: int = 1000
    temperature: float = 0.0
```

`SQLAGENT_ROW_LIMIT=500` in the environment becomes `settings().row_limit == 500`,
as an `int`. A malformed value fails at startup with a clear message, rather
than at 3am inside a string-versus-number comparison.

See [`config.py`](../src/sqlagent/config.py).

---

## SQLAlchemy

The standard Python database toolkit. This project uses two parts of it.

### Connecting and querying

```python
engine = create_engine("postgresql+psycopg://user:pass@localhost:5432/mydb")

with engine.begin() as connection:          # opens a transaction
    result = connection.execute(text("SELECT 1"))
    rows = result.fetchall()
# transaction committed (or rolled back on exception) on exit
```

`engine.begin()` is a context manager: the `with` block guarantees the
transaction is closed even if an exception is raised. Forgetting to release a
connection is the classic way to exhaust a pool and take down a service.

### Reflection — the important one

**Reflection** asks the database to describe itself:

```python
metadata = MetaData()
metadata.reflect(bind=engine)

for name, table in metadata.tables.items():
    for column in table.columns:
        print(name, column.name, column.type, column.nullable)
    for constraint in table.foreign_key_constraints:
        ...
```

This is why the agent never needs to be told the schema, and why the same code
works against PostgreSQL, MySQL and SQLite — SQLAlchemy queries each one's
catalogue and normalises the result.

> **A real bug this project hit.** Iterating `table.foreign_keys` yields one
> entry *per column*, which tears a composite key like
> `FOREIGN KEY (tenant_id, order_id)` into two unrelated single-column links.
> A query joining on half a composite key returns wrong rows and raises no
> error. Iterating `table.foreign_key_constraints` keeps each constraint whole.
> See [`introspect.py`](../src/sqlagent/schema/introspect.py).

---

## NetworkX

A library for graphs — nodes connected by edges. This project uses it to model
the database: each table is a node, each foreign key is a directed edge.

```python
graph = nx.DiGraph()
graph.add_node("orders")
graph.add_edge("orders", "customers", foreign_key=[fk])   # orders → customers

graph.successors("orders")      # tables orders points AT   (its parents)
graph.predecessors("customers") # tables pointing AT customers (its children)
```

That is nearly the entire API surface used here. The value is not the library;
it is the framing. "Which tables are relevant to this question?" becomes
"which nodes are within N hops of this node?", which is a solved problem with a
well-understood algorithm (breadth-first search).

### Two things that break naive graph code

**The graph is not a tree, and not even acyclic.**

```sql
-- Self-reference: a table that is its own parent
CREATE TABLE employees (id INT PRIMARY KEY, manager_id INT REFERENCES employees(id));

-- Mutual reference: a two-node cycle, and entirely legal
ALTER TABLE orders   ADD FOREIGN KEY (invoice_id) REFERENCES invoices(id);
ALTER TABLE invoices ADD FOREIGN KEY (order_id)   REFERENCES orders(id);
```

Any traversal must track visited nodes or it loops forever. There is a test
for the mutual-reference case that would *hang* rather than fail if the visited
set were removed.

**Direction must be ignored while searching.** Foreign keys point child →
parent. Starting at `customers` and following only outgoing edges finds
nothing, because `customers` is a parent and declares no keys — yet `orders` is
obviously relevant. The walk therefore follows edges in both directions, while
still recording direction on each edge for building the eventual `JOIN`.

See [`graph.py`](../src/sqlagent/schema/graph.py) and
[`retrieval.py`](../src/sqlagent/schema/retrieval.py).

---

## sqlglot

A SQL parser. Given a string, it produces a syntax tree you can inspect and
modify.

```python
statement = sqlglot.parse_one("SELECT * FROM orders", read="postgres")
type(statement)                      # <class 'sqlglot.expressions.Select'>
statement.limit(100).sql()           # 'SELECT * FROM orders LIMIT 100'
```

This is what makes the safety layer trustworthy. Instead of searching text for
scary words, the validator asks structural questions:

- Is this exactly one statement? (`SELECT 1; DROP TABLE orders` is two.)
- Is that statement a `Select`, `Union`, `Except` or `Intersect`? Anything else
  — `Delete`, `Update`, `Drop`, `Grant` — is refused.
- Does any common table expression contain a write?
- Which tables does it read? (Excluding CTE aliases, which look like tables in
  the tree but are defined inside the query.)
- Does it have a `LIMIT`, and is it small enough?

sqlglot also normalises SQL, which is how the benchmark can compare two
differently-written but equivalent queries.

See [`validator.py`](../src/sqlagent/guards/validator.py).

---

## pytest

Python's test framework. A test is a function whose name starts with `test_`
and which uses plain `assert`.

```python
def test_limit_is_added_when_missing():
    result = validate("SELECT * FROM orders", row_limit=100)
    assert "LIMIT 100" in result.sql
```

### Fixtures

A fixture is reusable setup, requested by naming it as a parameter:

```python
@pytest.fixture
def snapshot() -> SchemaSnapshot:
    return SchemaSnapshot(tables=..., version=...)

def test_something(snapshot):      # pytest builds it and passes it in
    assert len(snapshot) == 8
```

`scope="session"` builds it once for the whole run — used here for the database
connection, which is slow to establish.

### Markers

```python
@pytest.mark.integration
def test_reflect_finds_every_table(live_snapshot): ...
```

```bash
make test-unit          # skip anything needing a database
make test-integration   # only those
```

Integration tests skip themselves when no database is reachable, so `pytest`
works on a fresh checkout with nothing running.

---

## FastAPI

If you know Express, FastAPI is the same role with three differences.

### Routes

```js
// Express
app.post('/api/ask', (req, res) => {
  const { question } = req.body;
  res.json({ answer: '...' });
});
```

```python
# FastAPI
@app.post("/api/ask")
async def ask(request: AskRequest) -> AskResponse:
    return AskResponse(answer="...")
```

### Validation is not your problem

Express hands you `req.body` as whatever arrived. Checking it is up to you, and
the check is usually missing or partial.

FastAPI reads the annotation `request: AskRequest`, parses the body into that
model, and returns `422` with a precise description if it does not fit. Your
handler only ever runs with valid input. It is `express.json()` plus zod,
built in and not optional.

### No `res` object

You return a value and FastAPI serialises it. There is no `res.send` to
forget, so a handler cannot hang by falling off the end — a failure mode every
Express codebase has hit.

### Dependencies instead of middleware

```python
AgentDep = Annotated[SqlAgent, Depends(get_agent)]

@app.get("/api/health")
def health(agent: AgentDep) -> HealthResponse: ...
```

"Call `get_agent()` first, pass me the result." Comparable to attaching
something to `req` in Express middleware, except it is typed, explicit at the
point of use, and trivially replaceable in tests.

### async, and why the agent runs in a thread

`async def` handlers share one thread via an event loop. Blocking that thread
blocks *every* concurrent request.

The agent is ordinary blocking code — database calls, HTTPS calls to Bedrock.
Calling it directly inside `async def` would freeze the server for the several
seconds a question takes. So:

```python
result = await run_in_threadpool(agent.ask, request.question)
```

which hands it to a worker thread and awaits the result. In Node the same
hazard exists — a synchronous loop blocks the event loop — and the same fix
(a worker) applies.

### Free interactive docs

Start the server and open `http://localhost:8000/docs`. FastAPI generates an
OpenAPI schema from the type annotations, with a form for trying each endpoint.

See [`api/app.py`](../src/sqlagent/api/app.py).

---

## Server-Sent Events

A question takes a few seconds. A spinner for that long reads as "broken".
Showing "writing SQL… running the query…" requires the server to send several
messages over one request.

**Server-Sent Events (SSE)** is the simplest way to do that: an HTTP response
that stays open and emits text frames.

```
event: generating
data: {"tables": ["orders", "customers"]}

event: executing
data: {"sql": "SELECT count(*) FROM orders"}
```

A frame is an optional `event:` line, a `data:` line, and a blank line that
terminates it. The browser reads it with `EventSource`:

```ts
const source = new EventSource('/api/ask/stream?question=...');
source.addEventListener('generating', (e) => console.log(JSON.parse(e.data)));
```

**SSE versus WebSockets.** WebSockets are bidirectional and need protocol
upgrade support through every proxy in the path. SSE is one-directional
server → client, is plain HTTP, and reconnects automatically. Progress
reporting is one-directional, so SSE is the right size of tool.

Two practical notes, both handled in the code:

- `EventSource` can only issue **GET** requests, which is why the streaming
  endpoint takes the question in the query string.
- Reverse proxies buffer responses by default, which defeats streaming
  entirely. The response sets `X-Accel-Buffering: no`.

---

## React, TypeScript and Vite

**React** renders UI from state. You describe what the screen should look like
for a given state; React works out the DOM changes.

```tsx
const [busy, setBusy] = useState(false);
return <button disabled={busy}>{busy ? 'Working…' : 'Ask'}</button>;
```

`useState` returns the current value and a setter. Calling the setter
re-renders the component.

**TypeScript** is JavaScript with types, checked at build time. The API client
declares exactly what the server returns, so a typo in `result.row_count`
fails the build rather than rendering `undefined`.

**Vite** is the build tool and dev server. `npm run dev` serves the app with
hot reloading; `npm run build` produces static files for deployment.

Two hooks beyond `useState` appear in this app:

- `useEffect` — run something after render, such as fetching the schema once on
  mount. Its return value is a cleanup function, used here to close an open
  SSE connection when the component unmounts.
- `useRef` — a mutable box that does *not* trigger re-renders. Used to hold the
  cancel function for an in-flight request, so asking a new question can
  abandon the previous one rather than racing it.

See [05-frontend.md](05-frontend.md).

---

## Amazon Bedrock and Mantle

**Amazon Bedrock** is AWS's hosted-model service. You call models through AWS
rather than signing up with each provider.

**Bedrock Mantle** is a Bedrock endpoint that speaks **OpenAI's API shape**.
You POST the JSON you would send to OpenAI:

```
POST https://bedrock-mantle.us-east-1.api.aws/v1/chat/completions
{"model": "qwen.qwen3-coder-480b-a35b-instruct", "messages": [...]}
```

and get an OpenAI-shaped response back. The models behind it are the ones AWS
hosts — Qwen, DeepSeek, GLM, Mistral, Llama and others.

### Listed is not the same as callable

`GET /v1/models` returns everything Mantle *hosts*. It does not tell you what
your account may *invoke*. On the account this project was built against,
Anthropic and GPT-5 models appear in that list and return
`not available for this account` when called.

Because of that gap, the CLI has a probe that calls each model with a
sixteen-token prompt and reports which ones actually answer:

```bash
python -m sqlagent.cli --models
```

That probe is how the two model tiers in `config.py` were chosen, rather than
by reading a list.

### Different models, different routes

Not every model is reachable on every path. On this endpoint the Anthropic
models reject `/v1/chat/completions` with *"does not support the
'/v1/chat/completions' API"* and are served at `/anthropic/v1/messages`
instead. Worth knowing before concluding a model is broken.

---

## SigV4

Mantle documents two ways to authenticate: a Bedrock API key, or **SigV4** —
the request-signing scheme every AWS API uses. This project uses SigV4,
because it works with the credentials already in `~/.aws/credentials` and
requires no additional secret to be created, distributed or rotated.

Signing does not encrypt anything. It computes an HMAC over the request's
method, path, headers and a hash of the body, using your secret key, and puts
the result in the `Authorization` header. AWS recomputes it and compares.
Because the body is part of the signature the request cannot be altered in
transit, and because a timestamp is included it cannot be replayed
indefinitely.

The implementation hands the request to botocore's signer — cryptography is not
something to hand-roll — and plugs it into httpx as an auth hook:

```python
class SigV4Auth(httpx.Auth):
    requires_request_body = True     # the body must exist before signing

    def auth_flow(self, request):
        aws_request = AWSRequest(method=..., url=..., data=request.content, headers=...)
        BotocoreSigV4Auth(self._credentials(), service, region).add_auth(aws_request)
        for key, value in aws_request.headers.items():
            request.headers[key] = value
        yield request
```

Credentials are fetched per request rather than cached, so rotating temporary
credentials — an assumed role, an instance profile — keep working without a
restart.

See [`llm/mantle.py`](../src/sqlagent/llm/mantle.py).

---

## LangChain and LangGraph

### What they are

**LangChain** is a library of building blocks for model applications: provider
wrappers, prompt templates, output parsers, memory, retrieval.

**LangGraph** sits on top and models an application as a **state graph**. You
define:

* a **state** — a dictionary whose shape is declared once;
* **nodes** — plain functions taking the state and returning the keys they
  changed;
* **edges** — what runs next, including *conditional* edges that choose a
  destination by inspecting the state.

You compile the graph and invoke it. LangGraph threads the state through, and
provides streaming, checkpointing and a graph you can render.

### How this project uses LangGraph

The orchestration is a LangGraph state graph, in
[`agent_graph.py`](../src/sqlagent/agent_graph.py). The compiled graph:

```
NODES:  __start__, select_tables, build_context, generate_sql,
        validate_and_execute, widen_schema, write_answer, give_up, __end__

EDGES:
  __start__            -> select_tables
  select_tables        -> build_context           (conditional)
  select_tables        -> give_up                 (conditional)
  build_context        -> generate_sql
  generate_sql         -> validate_and_execute
  validate_and_execute -> write_answer            (conditional)
  validate_and_execute -> widen_schema            (conditional)
  validate_and_execute -> build_context           (conditional)
  validate_and_execute -> give_up                 (conditional)
  widen_schema         -> build_context
  write_answer         -> __end__
  give_up              -> __end__
```

### The state

```python
class AgentState(TypedDict, total=False):
    question: str
    seeds: list[str]
    neighbourhood: Neighbourhood
    tables: list[str]
    schema_text: str
    sql: str
    validated_sql: str
    result: QueryResult | None
    last_error: str | None
    failure_kind: str | None
    attempts: int
    answer: str
    trace: Trace
```

`total=False` means no key is required up front; nodes fill them in as the run
progresses. Seeding every key with a placeholder instead would make "not
computed yet" and "computed as empty" indistinguishable.

### A node

Nodes must have the signature `(state) -> dict`, but need the model client, the
database and the configuration. A factory closing over the agent supplies them:

```python
def make_generate_sql(agent: SqlAgent, emit):
    def generate_sql(state: AgentState) -> dict[str, Any]:
        prompt = (
            build_generation_prompt(state["question"], state["schema_text"], ...)
            if state.get("last_error") is None
            else build_repair_prompt(
                state["question"], state["schema_text"],
                state.get("failed_sql") or "", state["last_error"], ...
            )
        )
        completion = agent.client.complete(prompt, model=agent.pick_model(state["tables"]),
                                           system=SQL_SYSTEM_PROMPT)
        state["trace"].record(completion)
        return {"sql": extract_sql(completion.text)}     # ← only what changed
    return generate_sql
```

Returning a **partial update** keeps each node honest about its own effects:
`generate_sql` returns `{"sql": ...}` and demonstrably cannot touch the trace
or the neighbourhood.

### The conditional edge — why the graph earns its place here

The two repair loops are the whole difficulty of this pipeline:

```python
def route_after_execute(state: AgentState) -> str:
    if state.get("last_error") is None:
        return "write_answer"
    if state.get("attempts", 0) > agent.config.max_repair_attempts:
        return "give_up"
    if state.get("failure_kind") in CONTEXT_FAILURES:
        return "widen_schema"       # Loop B — the context was too narrow
    return "build_context"          # Loop A — the SQL was wrong
```

One function, one place. That matters concretely: an earlier version of this
pipeline made the same decision inline inside two separate exception handlers,
and one of them was missing the context-failure case entirely — so the most
common trigger for widening never widened. A conditional edge makes that class
of bug structurally harder, because there is exactly one place the decision can
be written.

### Wiring it up

```python
builder = StateGraph(AgentState)
builder.add_node("generate_sql", make_generate_sql(agent, emit))
...
builder.set_entry_point("select_tables")
builder.add_edge("build_context", "generate_sql")
builder.add_conditional_edges("validate_and_execute", route_after_execute, {
    "write_answer": "write_answer",
    "widen_schema": "widen_schema",
    "build_context": "build_context",
    "give_up": "give_up",
})
builder.add_edge("widen_schema", "build_context")   # Loop B rejoins the path
graph = builder.compile()
```

### One gotcha worth knowing

LangGraph calls `get_type_hints()` on the state schema when the graph is built,
which evaluates annotations **for real**. A type imported only under
`if TYPE_CHECKING:` therefore fails at graph-construction time:

```
NameError: name 'Trace' is not defined
```

The fix is a genuine runtime import. In this codebase that is safe from
circularity because `pipeline` imports `agent_graph` lazily, inside `ask()`.

### What LangChain is *not* used for

Only LangGraph is used, for orchestration. The model client is a direct httpx
call against Bedrock Mantle's documented HTTP endpoint
([`llm/mantle.py`](../src/sqlagent/llm/mantle.py)), rather than a LangChain
provider wrapper — Mantle speaks OpenAI's shape, SigV4 signing is a documented
scheme, and the whole client is about 200 lines. A wrapper would add a
dependency without removing any of that code.

### A graph is not the same as a tool-calling agent

Worth separating two things the word "agent" covers:

* **This**: a graph whose nodes and edges are fixed by the engineer. The model
  is asked to do three bounded things — pick tables, write SQL, phrase an
  answer. Control flow is deterministic and inspectable.
* **A tool-calling loop**: the model is given tools and decides what to call
  next, repeatedly, until it stops. Maximum flexibility, non-deterministic
  control flow, and capable of spending twenty model calls on a question that
  needed two.

LangGraph supports both. This project uses the first, because every question
genuinely follows the same sequence. If the project grows an open-ended
investigation mode — "explore this dataset and tell me what is interesting" —
that mode has no knowable step count and should be built the second way.

### What LangGraph gives this project next

Two things on the roadmap it makes cheap:

* **Checkpointing.** Adding a `MemorySaver` or a Postgres checkpointer lets a
  run pause for a clarifying question and resume later — human-in-the-loop that
  survives a restart.
* **Streaming from inside the graph.** The `ProgressHook` currently emits stage
  events manually; LangGraph's own `astream_events` can emit them from node
  transitions directly.

---

Next: [3. Architecture](03-architecture.md)
