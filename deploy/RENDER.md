# Deploying on Render

One Docker web service: the FastAPI API plus the built frontend, same origin.
Accounts, approvals and uploads' originals live on Neon; the model is Bedrock
Mantle. Render rebuilds and redeploys on every push to `master` **after the
GitHub checks pass** (`.github/workflows/ci.yml`, `autoDeployTrigger:
checksPass` in `render.yaml`). That is the whole CI/CD: merge, wait, it is live.

Nothing here has been run on Render yet. Treat each step as something to check.

## 1. Create the service

1. Push this repo to GitHub (`origin` is `jntbatra/aperture`).
2. Render dashboard, **New, Blueprint**, pick the repo. It reads `render.yaml`.
3. Render asks for every variable marked `sync: false`. Fill them from the table
   below. Nothing secret is stored in the repository.

The service is on the `free` plan: it sleeps when idle and has no persistent
disk, so anything under `/var/data` (history, conversations, CSV/Excel
datasets) is lost on restart. Accounts and approvals live in Neon Postgres and
uploaded originals in Neon Object Storage, so those survive. To keep local
state, move to a paid plan and add a disk back to `render.yaml`.

## 2. Environment

| Variable | Where the value comes from |
|---|---|
| `DATABASE_URL`, `NEON_AUTH_BASE_URL`, `NEON_AUTH_JWKS_URL` | your local `.env` (written by `neon deploy`) |
| `SQLAGENT_S3_ENDPOINT`, `SQLAGENT_S3_ACCESS_KEY_ID`, `SQLAGENT_S3_SECRET_ACCESS_KEY` | `.env`: `AWS_ENDPOINT_URL_S3`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` (Neon's storage keys) |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | **a different pair**: an IAM user allowed to call Bedrock Mantle (`bedrock-mantle:*`). See below |
| `SQLAGENT_DATABASE_URL` | the database the agent answers about, with a **read-only role** |
| `SQLAGENT_LLM_BASE_URL` | from your local `.env` |
| `SQLAGENT_ADMIN_EMAIL` | who approves sign-ups |
| `SQLAGENT_PUBLIC_URL` | `https://<your domain>`; approval links are built from it |
| `SQLAGENT_SMTP_*` | an SMTP account (Amazon SES SMTP works). Unset, approval links are only written to the service log |

**Two sets of "AWS" keys, on purpose.** Neon's storage keys use the standard
`AWS_*` names, and so does Bedrock's default credential chain. On Render they
would collide, so Neon's go in `SQLAGENT_S3_*` (which the app prefers) and
`AWS_*` stays for Bedrock. Do not reuse your personal `archon-dev` keys: create
a dedicated IAM user with only the Mantle permission, so it can be rotated or
revoked alone.

## 3. Neon Auth must trust the site

Neon Auth only redirects to allow-listed origins:

```bash
neon neon-auth domain add https://<your domain>
neon neon-auth domain add https://<service>.onrender.com   # while testing
```

The frontend's Auth URL is baked into the bundle at build time
(`VITE_NEON_AUTH_URL`, defaulted in the `Dockerfile` to this project's public
Auth URL). It is not a secret.

## 4. Domain

Render, **Settings, Custom Domains**, add the domain and create the DNS record
Render shows. Render issues the certificate. Then set `SQLAGENT_PUBLIC_URL` to
the domain.

## 5. First user

Sign-up is open but access is not: the first person to sign up (you) is
`pending` like everyone else. With SMTP configured, the approval link arrives
at `SQLAGENT_ADMIN_EMAIL`. Without it, find the link in the Render service logs
(`mail not sent (no SMTP configured)`). Opening it shows a page; press Approve.

## 6. Health checks

Render checks `/api/healthz`, which touches nothing else. `/api/health` builds
the agent and needs the database under analysis, so it is not used for routing.

## Known limits

- Free plan: cold start after idle, and local files do not survive a restart.
- The MCP server (`mcp_server.py`) is not part of this service and has no
  authentication of its own. Do not expose it publicly as it is.
- Restoring uploaded Postgres dumps needs `SQLAGENT_POSTGRES_ADMIN_URL`; leave
  it unset until there is an isolated server for it. CSV and Excel work now.
