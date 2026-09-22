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

## Connecting a tenant's database, and not trusting them about it

A tenant pastes a connection string and says the role is read-only. That is a
claim about *their* database, by someone who may have created the role five
minutes ago from a guide they skimmed. Believing it means the product's central
promise — "it cannot damage your data" — rests on a stranger having configured
`GRANT` correctly.

So `saas/connect.py` **tests** the role before saving the connection. It tries
to write, inside a transaction it rolls back, and requires the database to
refuse:

| Probe | Statement |
|---|---|
| `CREATE TABLE` | can it add objects? |
| `INSERT` | `INSERT INTO t SELECT * FROM t WHERE 1 = 0` |
| `UPDATE` | `UPDATE t SET c = c WHERE 1 = 0` |
| `DELETE` | `DELETE FROM t WHERE 1 = 0` |

Four separate powers, because a role can lack one and hold another, and any one
of them loses data.

**Every probe is a no-op even if it succeeds.** The predicates match nothing,
so a probe that is wrongly permitted *and* whose rollback fails still changes
nothing. Writing a probe that would do damage in order to find out whether
damage is possible is not an acceptable design.

### Why this is the strongest check in the system

Every other defence is code in this process — the SQL validator, the cost gate,
the read-only transaction — and code in this process is the thing most likely to
have a bug in it. The connected role is the only boundary enforced by the
database itself, on the other side of the network, by software nobody here
wrote. It is the one that still holds if everything in this repository is wrong.

Verified against the live production database:

```
aperture_ro      usable=True   all 4 probes refused, 56 tables
app_owner_user  usable=False  "That role can still CREATE TABLE, INSERT,
                                DELETE, UPDATE"
```

Nothing was left behind in either case. A failed probe is a **hard rejection**,
not a warning: a tenant told "we could not verify this is read-only" and allowed
to continue has been given a safety property they do not have, in writing, by a
product whose main promise is that property.

SQLite is refused outright — a file has no role to make read-only, and a tenant
"connecting" one in a hosted product is pointing at our disk. They are told to
upload it instead, which already works.

---

## Tiers, and why they are mostly about the AI

Most SaaS tiers gate features. This one gates **inference**, because that is
where the money goes: a question is three model calls by default and six on
`thorough`. Selling that flat is selling a variable cost of goods at a fixed
price, and it works right up until a customer finds the toggle.

| | Free | Pro | Enterprise |
|---|---|---|---|
| Price / month | $0 | $49 | negotiated |
| Questions / month | 100 | 2,000 | unlimited |
| Quality tier | fast | **thorough** | thorough |
| Strong model | — | yes | yes |
| Connected databases | 1 | 3 | unlimited |
| Uploaded datasets | 1 | 25 | unlimited |
| Seats | 1 | 5 | unlimited |
| Row limit | 500 | 5,000 | 50,000 |
| History retention | 7 days | 180 days | 3 years |

Names match Unilink's `SUBSCRIPTION_PLAN` exactly, so the two products can share
one billing story instead of a translation table living somewhere forever.

**The free tier is a real product.** It is pinned to the light model, which
measured 58.7% on BIRD — good enough to be useful. A free tier that teaches
people the tool does not work is worse than no free tier.

**Options are clamped, not rejected.** A free user who sends
`quality_tier: "thorough"` — from the docs, an old SDK, a shared snippet — gets
a fast answer, not a 402. Refusing the whole question because one optional field
was too ambitious turns an upsell into an outage. Voting and the critic are
clamped alongside it, since they are the same trade under different names.

**Quotas are checked before a question, never during one.** A tenant who hits
the limit on the third call of a four-call question gets that question finished.
Cutting a request in half to save one model call produces a broken answer, a
support ticket and a refund, which costs more than the call.

**Limits are data, not conditionals.** `if plan == "pro"` scattered through the
request path puts pricing policy in a dozen files, and the day a limit changes
one of them is missed. A test asserts each tier is at least as generous as the
one below it across all six numeric limits, because an inversion among eighteen
numbers is invisible by eye.

**Unknown plan names resolve to FREE**, not to the most permissive. A typo, a
renamed plan or a row written by an older version should under-serve rather than
hand out an enterprise entitlement.

---

## Authentication, wired

`SQLAGENT_REQUIRE_AUTH` decides the mode. Off by default: the CLI, the
benchmark and a team running this against their own warehouse should not have
to invent an account, and turning it on is a deliberate act. `/api/health`
reports which mode is running so it is never a guess.

### The dependency is the design

`PrincipalDep` resolves a request to a tenant or raises 401. Every route that
touches tenant data declares it — and the test that guarantees that **walks the
app's own route table** rather than a hand-written list:

```
assert not missing, f"routes with no authentication: {sorted(set(missing))}"
```

A list maintained by hand would be missing exactly the route that was missing
from the code. Proven to bite: removing the dependency from `/api/stats` fails
the test naming that path.

Public by design and by name: `/api/health` (a load balancer probes it before
anyone signs in), `/api/plans` (a pricing page is read before there is an
account), `/api/options` (static descriptions, no tenant data).

### Single-tenant mode returns a real principal, not None

`Principal(tenant_id="local")`. A None that downstream code has to check is a
fallback waiting to be forgotten; a real principal means every scoping path —
cache keys, history filters, usage counting — runs identically in both modes,
which is the only way that code is ever exercised.

### What is hashed with what, and why they differ

| Value | Scheme | Why |
|---|---|---|
| API key secret | SHA-256 | 256 bits of our own randomness; nothing for a work factor to buy |
| Session token | SHA-256 | same |
| **Password** | **scrypt**, 32768/8/1 | chosen by a person, short, probably reused — the whole defence is making each guess expensive |
| Tenant DSN | Fernet (encrypted) | it has to be used, so it cannot be hashed |

Getting the first and third the same way round is a common and expensive
mistake. Cost parameters are stored *in* each hash (`scrypt$n$r$p$salt$hash`),
so raising them later does not lock anyone out — `needs_rehash` upgrades a
password at sign-in, which is the only moment the plaintext exists to rehash
with. A scheme that reads its parameters from a constant cannot raise them, so
in practice it never does. Measured at **63ms** per hash.

### Not leaking who has an account

Sign-in returns one message for "no such user" and "wrong password", and
`verify_user` runs a scrypt hash against a dummy value when the user is missing
so the *timing* does not say what the message refuses to. Verified:

```
unknown address  -> {"detail":"Wrong email or password."}
wrong password   -> {"detail":"Wrong email or password."}
```

Sign-**up** does say an address is taken, and that is unavoidable: a signup form
that refuses to is unusable, and the same information is available from it
anyway.

### The cookie

`httponly` — JavaScript cannot read it, so an XSS bug cannot exfiltrate the
session. `samesite=lax`, not strict: strict drops the cookie on any cross-site
navigation, so arriving from a link in an email would sign you out. `secure`
follows `SQLAGENT_SECURE_COOKIES`, off locally only because a Secure cookie is
silently dropped over plain HTTP and the resulting "sign-in does nothing" is
genuinely hard to diagnose.

Signing out deletes the row **and** clears the cookie, in that order. Clearing
the cookie alone leaves the token valid for anyone who captured it.

### One origin, which was not optional

This bit immediately. Sign-up returned 200 and set the cookie; the very next
`/api/auth/me` was 401. The page was on `127.0.0.1:5173` and the client pointed
at `localhost:8000` — same machine, different hosts, therefore cross-site,
therefore `SameSite=Lax` withheld the cookie. It looks exactly like a broken
login and is nothing of the sort.

Relaxing `SameSite` to `None` would have "fixed" it by turning the session into
a cookie any site can cause to be sent, which is the trade CSRF exists because
of. Instead the Vite dev server now proxies `/api` and the client defaults to a
same-origin base, which removes the problem and matches production — where the
frontend and the API sit behind one domain.

### Quotas

Enforced in one function called by both the JSON and the streaming ask
handlers, because two handlers answering questions means two places a quota can
be forgotten.

Checked **before** a question and counted **after** it, and only when it
succeeded — a question that failed on a model timeout must not consume an
allowance, and a customer charged for an error writes a support ticket that
costs more than the question did.

A spent allowance is 402 with the numbers in it. A tier the plan does not
include is *clamped*, not refused.

### Verified end to end in a browser

```
1. landing                     shown to a signed-out visitor
2. weak password               refused server-side: "Use at least 12 characters."
3. sign-up                     console reached, cookie set
4. account panel               Free plan · 2026-09 · 0/20 · 0/0 detailed · 0/1 databases
5. asked one real question     Questions 1 / 20
6. sign out                    back to landing
7. reload                      still signed out
   console errors              none
```

---

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
