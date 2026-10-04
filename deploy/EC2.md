# Running the sql-agent service on EC2

For an Ubuntu EC2 box running pm2, with RDS PostgreSQL. The service
runs beside the existing backend, listens on `127.0.0.1:8000` only, and the backend
calls it through the TypeScript client (`sdk/README.md`).

Nothing here has been run on the target box yet. Treat each step as something to
check, not something known to work there.

---

## 1. AWS credentials — the same ones `aws configure` set up

The model client signs requests with the **default AWS credential chain**,
exactly what the AWS CLI uses: `~/.aws/credentials` / `~/.aws/config` for the
user the service runs as, or `AWS_PROFILE`, or the EC2 instance role. No keys go
in this repo or in the env file.

```bash
# as the user pm2 runs under (ubuntu)
aws configure            # skip if ~/.aws/credentials already exists
aws sts get-caller-identity   # must print the account you expect
```

The identity needs permission to call Bedrock Mantle (`bedrock-mantle:*`
actions) in **us-east-1** — the region the model endpoint lives in, which is
not the box's region. Calls go over HTTPS to us-east-1; that is expected.
Check with step 5.

## 2. A read-only database role

The one boundary that cannot be talked around. Run once against RDS (from the
EC2 box — RDS is not reachable from a laptop):

```sql
CREATE ROLE sqlagent_ro LOGIN PASSWORD '<choose one>';
GRANT CONNECT ON DATABASE <database> TO sqlagent_ro;
GRANT USAGE ON SCHEMA public TO sqlagent_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO sqlagent_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO sqlagent_ro;
REVOKE CREATE ON SCHEMA public FROM sqlagent_ro;
```

To hide sensitive tables (OTPs, tokens, payment details), grant per table
instead of `ALL TABLES`. The agent can only read what this role can read.

## 3. Install

```bash
sudo apt-get install -y python3.12 python3.12-venv   # needs Python >= 3.12
curl -LsSf https://astral.sh/uv/install.sh | sh       # or: pipx install uv
git clone <sql-agent repo> /var/www/sql-agent
cd /var/www/sql-agent && uv sync --no-dev
```

## 4. Configuration — `/var/www/sql-agent/.env` (mode 600)

```bash
SQLAGENT_DATABASE_URL="postgresql+psycopg://sqlagent_ro:<password>@<rds-host>:5432/<database>?sslmode=require"
SQLAGENT_MANTLE_REGION="us-east-1"
SQLAGENT_SERVICE_TIER=""          # standard. "flex" is half price but slower — fine for batch, not for a dashboard
SQLAGENT_GLOSSARY_PATH="/var/www/sql-agent-private/client-glossary.json"   # optional, see below
```

**Glossary.** Domain facts the schema cannot express: units (money in
minor units), and columns that look right but are not. Keep the
real file **outside the repo** (it describes the client's data) — start from
`glossaries/example.json`, whose column notes are correct and whose figures are
placeholders.

**Column docs.** Read automatically from PostgreSQL comments. To see whether
the database has any:
```sql
SELECT count(*) FROM pg_description d JOIN pg_class c ON c.oid = d.objoid WHERE d.objsubid > 0;
```

**Tiers need no server config.** `cheap` / `expensive` are chosen per question
by the client.

## 5. Check it before starting the service

```bash
cd /var/www/sql-agent
uv run python -m sqlagent.cli --help >/dev/null && echo "installed"
uv run uvicorn sqlagent.api.app:app --host 127.0.0.1 --port 8000 &
sleep 5; curl -s http://127.0.0.1:8000/api/health    # "status":"ok" and a table count
curl -s -X POST http://127.0.0.1:8000/api/ask -H 'Content-Type: application/json' \
  -d '{"question":"How many orders are there?","options":{"ambiguity_handling":"best_effort"}}'
kill %1
```

A 401/403 from Bedrock in the second call means the AWS identity lacks
Mantle permission or the region is wrong. A database error means the URL or
the role.

## 6. Run under pm2

```bash
cd /var/www/sql-agent
pm2 start "uv run uvicorn sqlagent.api.app:app --host 127.0.0.1 --port 8000 --workers 2" \
  --name sql-agent
pm2 save
```

`--host 127.0.0.1` is deliberate: only processes on this box can reach it. Do
not open port 8000 in the security group. **The service has no login** — anyone
who can reach the port can query the database (read-only) — so the private
address and the backend route's own auth are the only gates.

Point the backend at it with `SQL_AGENT_URL=http://127.0.0.1:8000`.

## Updating

```bash
cd /var/www/sql-agent && git pull && uv sync --no-dev && pm2 restart sql-agent
```

## What runs by default

`cache_sql` on (identical questions reuse their SQL; the query still re-runs, so
answers stay fresh), row cap 1,000, statement timeout 30s, read-only
transaction, SQL validator, cost gate. See `docs/06-operations.md`.
