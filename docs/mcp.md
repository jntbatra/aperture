# Aperture as an MCP server

[MCP](https://modelcontextprotocol.io) lets an assistant such as Claude Desktop
call tools. Aperture offers three, all read-only:

| Tool | What it does |
|---|---|
| `ask_database` | Answer a question in plain language. Returns the answer, the SQL and the rows. |
| `list_tables` | List the tables in the database. |
| `describe_table` | Columns and foreign keys of one table. |

There is no tool that takes raw SQL: the model asks in English and Aperture
writes, checks and runs the query.

The MCP server runs on your machine against your database. It reads the same
`.env` as the web app (`SQLAGENT_DATABASE_URL`, the model settings), so set that
up first: [self-hosting](self-hosting.md).

## Local: stdio (Claude Desktop and similar)

The client starts the process itself, so there is nothing to expose. In the
client's MCP configuration:

```json
{
  "mcpServers": {
    "aperture": {
      "command": "/path/to/aperture/.venv/bin/python",
      "args": ["-m", "sqlagent.mcp_server"],
      "cwd": "/path/to/aperture"
    }
  }
}
```

## Remote: streamable HTTP

```bash
SQLAGENT_API_KEYS=$(openssl rand -hex 24) \
  python -m sqlagent.mcp_server --http --host 127.0.0.1 --port 8001
```

The endpoint is `http://127.0.0.1:8001/mcp` and wants
`Authorization: Bearer <key>`. Requests without a valid key get 401. The server
refuses to start over HTTP with no key unless you set
`SQLAGENT_AUTH_REQUIRED=false`, and it binds to `127.0.0.1` unless you pass
`--host`. Put it behind TLS (a reverse proxy) before exposing it beyond your own
machine: the key travels in a header.

Aperture does not run a shared public MCP endpoint. Every call is billed to the
model account of whoever runs the server, so you run your own.
