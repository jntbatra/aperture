# Run your own Aperture

Aperture is self-hosted: you run the server, point it at your database, and
everything else (the web app, the REST API, the SDKs, the MCP server, the CLI)
talks to your server. Nobody else's data or model bill is involved.

## What you need

- **A database to ask questions of.** PostgreSQL is the main target. You can also
  upload CSV, Excel or plain-SQL `pg_dump` files in the web app, with no database
  at all.
- **A model.** Aperture calls a model through an OpenAI-compatible endpoint. The
  tested setup is Amazon Bedrock Mantle with `google.gemma-4-31b`. Any
  OpenAI-compatible server works with `SQLAGENT_LLM_AUTH=bearer`.
- **Python 3.12+** and **Node 20+** to run from source, or **Docker**.

## 1. On your laptop

```bash
git clone https://github.com/jntbatra/aperture && cd aperture
make install
cp .env.example .env
```

Edit `.env`:

```bash
SQLAGENT_DATABASE_URL=postgresql+psycopg://readonly_user:password@localhost:5432/yourdb
SQLAGENT_LLM_BASE_URL=https://bedrock-mantle.us-east-1.api.aws/openai/v1
SQLAGENT_LLM_AUTH=bearer
SQLAGENT_LLM_API_KEY=<a Bedrock API key>
SQLAGENT_AUTH_REQUIRED=false      # no sign-in: only on a laptop or a trusted network
```

```bash
make api    # http://localhost:8000
make web    # http://localhost:5173
```

Use a database role that can only `SELECT`. Aperture also refuses anything that
is not a single read-only statement, but the role is your last line of defence.

## 2. With Docker

```bash
docker build -t aperture .
docker run -p 10000:10000 -v aperture-data:/var/data \
  -e SQLAGENT_DATABASE_URL=... \
  -e SQLAGENT_LLM_BASE_URL=... -e SQLAGENT_LLM_AUTH=bearer -e SQLAGENT_LLM_API_KEY=... \
  -e SQLAGENT_API_KEYS=$(openssl rand -hex 24) \
  aperture
```

The image serves the web app and the API on one port. Mount a volume at
`/var/data`: uploaded files and local chat history live there.

## 3. On a server you expose to the internet

Read **Who can get in** below first. [`deploy/RENDER.md`](../deploy/RENDER.md) walks
through the setup this project itself runs: Render for the container, Neon for
accounts, chat history and file storage, with sign-ups approved by the owner.

## Who can get in

There are three modes. Pick one on purpose.

| Mode | Setting | Use it for |
|---|---|---|
| **Sign-in with approval** | `NEON_AUTH_BASE_URL`, `NEON_AUTH_JWKS_URL`, `DATABASE_URL` (Neon), `SQLAGENT_ADMIN_EMAIL`, SMTP | A server other people use. Each new sign-up waits for the admin to approve it from an emailed link. |
| **API keys** | `SQLAGENT_API_KEYS=key1,key2` | Programs: the SDKs, scripts, the MCP server over HTTP. Keys are 16+ characters; make one with `openssl rand -hex 24`. Each key is its own user, with its own chats and uploads. |
| **Open** | `SQLAGENT_AUTH_REQUIRED=false` | A laptop or a network you already trust. Anyone who can reach the server can ask questions, upload files and run queries. |

With sign-in on and an API key set, both work: browsers sign in, programs send
`Authorization: Bearer <key>`. If you build the web app yourself with sign-in, pass
your Neon Auth URL: `docker build --build-arg VITE_NEON_AUTH_URL=https://... .`

## Settings you will want

All settings are environment variables prefixed `SQLAGENT_` unless noted. The
full list, with what each does, is in `src/sqlagent/config.py`.

| Variable | Meaning |
|---|---|
| `SQLAGENT_DATABASE_URL` | The database to ask about. Read-only role. Without it, the web app still works on uploaded files. |
| `SQLAGENT_LLM_BASE_URL`, `SQLAGENT_LLM_AUTH`, `SQLAGENT_LLM_API_KEY` | The model endpoint. `bearer` sends the key as-is. |
| `SQLAGENT_LIGHT_MODEL`, `SQLAGENT_STRONG_MODEL` | Model names. |
| `SQLAGENT_API_KEYS` | Comma-separated keys for programs. |
| `SQLAGENT_AUTH_REQUIRED` | `false` turns sign-in off. |
| `SQLAGENT_DATA_DIR` | Where uploads and local state live. |
| `SQLAGENT_ROW_LIMIT`, `SQLAGENT_STATEMENT_TIMEOUT_MS` | Cap on rows returned and on query time. |
| `DATABASE_URL` | A Postgres URL for accounts and chat history (Neon). Without it, chats are kept in a local SQLite file. |

## Know the limits

- **Cost.** Every question calls your model. A question costs roughly 2 model
  calls on the default tier; see the measurements in the README. Anyone with a key
  spends your money, so keep keys to people you trust.
- **Uploads on ephemeral disks.** Uploaded files are converted to SQLite on the
  server's local disk. On a host that wipes its disk on restart (a free tier),
  uploads and their records are lost. Use a persistent volume.
- **`pg_dump` uploads** are converted to SQLite: tables, rows, primary keys and
  foreign keys are kept; views, functions, indexes and enums are not. Custom-format
  dumps are refused; export with `pg_dump --format=plain`. To restore into a real
  Postgres instead, set `SQLAGENT_POSTGRES_ADMIN_URL`.
- **It reads, it does not write.** No path in Aperture runs `INSERT`, `UPDATE`,
  `DELETE` or DDL against your database.
