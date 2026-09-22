# Aperture as a service

Turning a single-tenant tool into a hosted product, across five fronts.

## The five fronts

Four are built and have no authentication. The fifth — the SDK — is new, and
is how anyone actually embeds this in their own product.

| Front | Today | Needs |
|---|---|---|
| Web UI | React app, calls `localhost:8000` | Session cookie, login, tenant switcher |
| REST API | Open on the port | API keys, per-tenant rate limits |
| MCP server | Reads one config, one database | Token-scoped tenant, still no raw-SQL tool |
| CLI | Local, reads `.env` | API key against the hosted API |
| **SDK** | **Does not exist** | Python first, then TypeScript |

Every one of them resolves a request to the same thing — a `Principal` naming a
tenant — and everything downstream is scoped by it. One resolution path, five
ways in.

### The SDK, and the one thing it must not do

A thin, typed client over the REST API. Thin is the requirement, not a
simplification: any logic that lives in the SDK is logic that cannot be fixed
without every customer upgrading, and it is logic the MCP server and the CLI
would then need their own copy of.

```python
from aperture import Aperture

client = Aperture(api_key="ak_live_...")
answer = client.ask("how many orders were delivered in June?")
print(answer.text, answer.sql)

with client.conversation() as chat:      # threads, so follow-ups resolve
    chat.ask("revenue by month for 2026")
    chat.ask("and just the top three")   # means nothing without the first
```

**There is no `run_sql`.** Same rule as the MCP server: offering one would let
a caller route around the validator, the cost gate and the read-only role in a
single line, which is the entire boundary this project is built on. The SDK
sends questions.

**A browser SDK must never hold an API key.** A key in frontend JavaScript is a
key in everyone's DevTools, and it carries a tenant's full access. The
TypeScript client therefore ships in two shapes:

| Shape | Credential | Where |
|---|---|---|
| Server-side (Node) | API key | your backend |
| Browser | short-lived token minted by *your* backend | the page |

Making the browser case awkward is deliberate. The easy version of this
feature is a security incident with a nice ergonomics story attached.

## What actually blocks this

Verified in the code, not assumed:

| Blocker | Where |
|---|---|
| **No authentication anywhere** | zero auth dependencies in `api/app.py` |
| One config per process | `settings()` is `@lru_cache(maxsize=1)` |
| One database per process | `get_agent()` is `@lru_cache(maxsize=1)` |
| History has no tenant column | `store.py` `SCHEMA` |
| SQL cache is not tenant-keyed | `cache.py` `SqlCache.key()` |
| Summary cache is not tenant-keyed | `summarise.py` `SummaryCache` |
| CORS is localhost-only | `api/app.py` |

`docs/07-decisions.md` already listed multi-tenancy as not built, for exactly
the reason that matters: *"Cache and memory keys would need tenant scoping to
avoid cross-tenant leaks."*

## The rule everything else follows

**Fail closed.** A request that does not resolve to a tenant is rejected. It
does *not* fall back to the server's configured database.

That fallback is the single most dangerous line that could be written here, and
it is the one a reasonable person writes by accident — `resolve_agent()` today
returns the default agent when `dataset_id` is None, and the tenant version of
that shape hands one customer's connection to an unauthenticated caller. There
is a test asserting the absence of that fallback on every front.

## Order of work

Vertically, in dependency order. Each is finished and tested before the next.

| # | Ticket | Why this order |
|---|---|---|
| 53 | Tenant identity: `Tenant`, `Principal`, API keys, control-plane store | Nothing below is safe without it |
| 54 | Fail-closed auth on all four fronts | The boundary, before anything is exposed |
| 55 | Per-tenant agent registry, bounded and evictable | Replaces the `maxsize=1` singletons |
| 56 | Tenant-scoped caches | The documented leak — SQL and summaries |
| 57 | Tenant-scoped store | History, conversations, datasets |
| 58 | Encrypted tenant database credentials | You hold customer read-only DSNs |
| 59 | Quotas and rate limits per tenant | A tenant must not be able to spend another's budget |
| 60 | Python SDK over the REST API | Needs auth and scoping to exist first |
| 61 | TypeScript SDK, server and browser shapes | After the Python one settles the surface |
| 62 | Short-lived browser tokens, so a key never reaches a page | Required before the browser SDK ships |
| 63 | AWS deployment: ECS Fargate, RDS, CloudFront, Secrets Manager | Last, because it packages the above |

## Hosting shape

```
CloudFront + S3            the built frontend
        |
       ALB                 TLS, WAF
        |
   ECS Fargate             the API; IAM task role -> Bedrock
        |
  +-----+------+
  |            |
RDS Postgres   Secrets Manager
(control        (tenant database
 plane)          credentials)
```

No static model credentials anywhere: the task role mints Bedrock bearer tokens
through the same `provide_token()` path that already works locally. That
property is worth protecting — it is why there is no `ANTHROPIC_API_KEY` or
equivalent in this repository to leak.

## What a tenant may bring

Both, as chosen:

- **Their own database.** A read-only connection string they supply. Strongest
  isolation; you store an encrypted DSN and never their rows.
- **Uploaded data.** CSV, Excel or a dump, through the ingest path that already
  exists. You host it, which makes you a processor of their data.

The existing `dataset_id` split is the right shape for this. It becomes
tenant-scoped rather than global.

---

## The design language

Taken from `jntbatra/unilink` (private, Next.js + shadcn/ui "new-york" +
Prisma/Neon), intermingled with Aperture's existing system rather than
replacing it.

### What came from where

**Structure from Unilink.** shadcn's token *names* — `--background`,
`--foreground`, `--primary`, `--muted-foreground`, the full `--sidebar-*` set,
`--chart-1..5`, `--radius: 0.65rem`, the `.dark` class variant.

That is the load-bearing decision. It means any shadcn component drops in
unmodified, a designer moving between the two products reads one vocabulary,
and the Unilink dashboard and the Aperture console can share a component
library later without a translation layer. A third naming scheme would have
made that impossible for no gain.

**Warmth from Aperture.** Unilink's neutrals are zinc, hue 285, cool. Aperture's
are hue ~85, warm paper. Adopting shadcn's palette wholesale would have
produced a generic dashboard, and the warm paper is what stops a text-heavy
analyst tool from reading like a spreadsheet. Instrument Serif stays for display
type for the same reason.

### The primary, measured rather than eyeballed

Both palettes were converted sRGB -> OKLab -> OKLCH rather than matched by eye:

| | OKLCH | |
|---|---|---|
| Unilink primary | `oklch(0.705 0.213 47.6)` | bright orange |
| Aperture clay | `oklch(0.567 0.136 35.6)` | muted terracotta |
| **SaaS primary** | **`oklch(0.620 0.170 40.0)`** | Aperture's hue, Unilink's energy |

Same hue family — that was luck, and it is why the two blend rather than clash.
The SaaS surface sits between them because a product with pricing and
onboarding needs a call to action that carries further than an accent used
three times on a page of prose.

OKLCH throughout, not hex: lightness is perceptual, so contrast can be checked
by reading the first number and a dark variant is a lightness change rather
than a fresh guess.

### Dark mode

Aperture had none. Unilink does, and a tool analysts leave open on a second
monitor needs one.

Not an inversion — surfaces *lift*: the background is a warm near-black and
cards sit above it, which is how depth reads without shadows. Borders become
low-alpha white so they stay correct over any surface lightness. The primary
gets brighter, because a mid-lightness accent that reads as confident on paper
disappears against dark.

Three states, not two: `light`, `dark`, `system`. Collapsing to a boolean makes
the first click pin the user to whichever theme they happened to be seeing, and
leaves "go back to following my desktop" with no gesture.

Applied by a blocking script in `index.html` before React mounts. React mounting
is asynchronous, so a theme applied in an effect arrives one paint late and
every load flashes white.

### Migration without a rewrite

Aperture's CSS names colours after what they are made of (`--paper`, `--ink`,
`--clay`); shadcn names them after what they do. Rather than find-and-replace
across 1,400 lines of working CSS, the old names became aliases onto the new
ones. Every existing rule keeps working and gets dark mode for free.

All 17 remaining hardcoded colours were tokenised. One of them was a bug this
introduced and the browser check caught: `color: var(--muted, #6b7280)` in the
Drift panel. In shadcn `--muted` is a *surface*, not text — the moment the token
started existing, that label became near-white on white. The text token is
`--muted-foreground`.

### What was deliberately not taken

Unilink's domain — `Workspace`, `Folder`, `Video`, `Member`, `Invite`,
`College` — is a different product. Its `Subscription` model is worth copying
structurally when billing lands (`SUBSCRIPTION_PLAN` FREE/PRO/ENTERPRISE,
Stripe `customerId`/`priceId`/`status`/period bounds, `cancelAtPeriodEnd`), and
its `Member` + `WorkspaceMemberRole` shape is close to what tenant seats will
need. Noted for #59 and the billing work, not imported now.
