"""What each plan may do, and what the AI costs.

Why tiers are about the model, not about features
-------------------------------------------------
Most SaaS tiers gate features. This one mostly gates *inference*, because that
is where the money goes: a question costs three model calls at the default
settings and six on ``thorough``, and a single ``--judge`` run over a working
day can exceed a month of everything else. A pricing model that ignores that
sells a fixed-price product with a variable cost of goods, which works until a
customer discovers the thorough toggle.

So the plan controls three AI dimensions:

* **How many questions** per month. The unit a customer understands, and the
  unit that maps to cost.
* **Which quality tier** they may ask for. ``thorough`` is roughly 3x the
  latency and 4x the tokens — a real upgrade, priced as one.
* **Which model tier** answers. The strong model is the expensive one, and a
  free plan pinned to the light model still works.

The names match Unilink
-----------------------
``FREE`` / ``PRO`` / ``ENTERPRISE``, the same values as Unilink's
``SUBSCRIPTION_PLAN`` enum. That is not cosmetic: the two products are meant to
share a billing story, and a mismatch between the enums would mean a
translation table living somewhere for the rest of time.

Why limits are data, not conditionals
-------------------------------------
A ``Plan`` is a record of numbers. Enforcement reads it. The alternative —
``if plan == "pro"`` scattered through the request path — puts pricing policy
in a dozen files, and the day a limit changes, one of them is missed. Here a
new plan is a new row, and every limit is visible in one place beside the
others it has to be consistent with.

What a quota is not
-------------------
Not a hard stop mid-question. A tenant who hits their limit on the third call
of a four-call question gets that question finished, and the *next* one is
refused. Cutting a request in half to save one model call produces a broken
answer, a support ticket and a refund, which costs more than the call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

PlanName = Literal["FREE", "PRO", "ENTERPRISE"]

UNLIMITED = -1
"""Sentinel for "no limit", used instead of ``None``.

``None`` invites ``if limit and used > limit``, which is correct for None and
silently wrong for a limit of 0 — a plan that is meant to allow nothing would
allow everything. A negative number fails the comparison the same way for every
value, and ``is_unlimited()`` says what it means.
"""


@dataclass(frozen=True, slots=True)
class Plan:
    """One tier, as numbers rather than conditionals."""

    name: PlanName
    label: str
    price_monthly_usd: int

    questions_per_month: int
    """The unit customers understand and the one that maps to inference cost."""

    max_quality_tier: Literal["fast", "thorough"] = "fast"
    """The most expensive setting this plan may request.

    ``thorough`` runs the query three times and has a second model review it:
    roughly 3x the latency and 4x the tokens. Selling it flat is selling a
    variable cost at a fixed price.
    """

    strong_model: bool = False
    """Whether questions may be answered by the strong tier.

    The light model measured 58.7% on BIRD against the strong tier's comparable
    number, so a free plan pinned to it is a real product rather than a
    crippled demo — which is the difference between a free tier that converts
    and one that teaches people the tool does not work.
    """

    max_connected_databases: int = 1
    max_uploaded_datasets: int = 1
    max_seats: int = 1

    row_limit: int = 1000
    """Rows a single answer may return. Also a cost control: rows become
    tokens when the answer is written."""

    history_retention_days: int = 30
    """How long questions and their results are kept.

    Storage is cheap; the reason this is tiered is that retained results are
    *customer data sitting in our database*, and a short default on the free
    tier is a smaller thing to protect."""

    features: frozenset[str] = field(default_factory=frozenset)
    """Named capabilities, as an allow-list.

    A feature added later is not implicitly granted to existing plans — the
    same reason ``Principal.scopes`` is an allow-list. Getting this backwards
    means a new feature silently ships to the free tier on deploy day.
    """

    def allows(self, feature: str) -> bool:
        return feature in self.features

    def is_unlimited(self, limit: int) -> bool:
        return limit == UNLIMITED


PLANS: dict[str, Plan] = {
    "FREE": Plan(
        name="FREE",
        label="Free",
        price_monthly_usd=0,
        questions_per_month=100,
        max_quality_tier="fast",
        strong_model=False,
        max_connected_databases=1,
        max_uploaded_datasets=1,
        max_seats=1,
        row_limit=500,
        history_retention_days=7,
        features=frozenset({"ask", "upload", "mcp"}),
    ),
    "PRO": Plan(
        name="PRO",
        label="Pro",
        price_monthly_usd=49,
        questions_per_month=2_000,
        max_quality_tier="thorough",
        strong_model=True,
        max_connected_databases=3,
        max_uploaded_datasets=25,
        max_seats=5,
        row_limit=5_000,
        history_retention_days=180,
        features=frozenset(
            {"ask", "upload", "mcp", "api", "sdk", "glossary", "drift", "thorough"}
        ),
    ),
    "ENTERPRISE": Plan(
        name="ENTERPRISE",
        label="Enterprise",
        price_monthly_usd=0,  # negotiated; 0 means "talk to us", not "free"
        questions_per_month=UNLIMITED,
        max_quality_tier="thorough",
        strong_model=True,
        max_connected_databases=UNLIMITED,
        max_uploaded_datasets=UNLIMITED,
        max_seats=UNLIMITED,
        row_limit=50_000,
        history_retention_days=1_095,
        features=frozenset(
            {
                "ask", "upload", "mcp", "api", "sdk", "glossary", "drift",
                "thorough", "judge", "sso", "audit_log", "byo_model",
            }
        ),
    ),
}


def plan_for(name: str | None) -> Plan:
    """The plan for a stored name, defaulting to FREE.

    An unrecognised name resolves to the *most restrictive* plan, not the most
    permissive. A typo in a database row, a plan that was renamed, a tenant
    written by an older version — every one of those should under-serve rather
    than hand out an enterprise entitlement to whoever caused it.
    """
    return PLANS.get((name or "").upper(), PLANS["FREE"])


@dataclass(frozen=True, slots=True)
class Usage:
    """What a tenant has consumed this period."""

    questions: int = 0
    connected_databases: int = 0
    uploaded_datasets: int = 0
    seats: int = 1


@dataclass(frozen=True, slots=True)
class Denial:
    """A refusal a customer can act on.

    Carries the limit and the plan that would lift it, because "quota exceeded"
    with no number is a support ticket and "you have used 100 of 100 questions
    this month; Pro includes 2,000" is a decision.
    """

    reason: str
    limit: int
    used: int
    upgrade_to: PlanName | None = None

    def render(self) -> str:
        if self.upgrade_to:
            better = PLANS[self.upgrade_to]
            return f"{self.reason} {better.label} includes more."
        return self.reason


def check_quota(plan: Plan, usage: Usage) -> Denial | None:
    """Whether another question may be asked. None means yes.

    Checked before a question starts, never during one. A tenant who reaches
    the limit on the third call of a four-call question gets that question
    finished — cutting a request in half to save one model call produces a
    broken answer and a refund, which costs more than the call.
    """
    limit = plan.questions_per_month
    if plan.is_unlimited(limit):
        return None
    if usage.questions < limit:
        return None

    return Denial(
        reason=(
            f"You have used {usage.questions} of {limit} questions this month."
        ),
        limit=limit,
        used=usage.questions,
        upgrade_to=_next_plan(plan),
    )


def check_can_connect(plan: Plan, usage: Usage) -> Denial | None:
    """Whether another database may be connected."""
    limit = plan.max_connected_databases
    if plan.is_unlimited(limit) or usage.connected_databases < limit:
        return None
    return Denial(
        reason=(
            f"{plan.label} connects {limit} database"
            f"{'' if limit == 1 else 's'}; you have {usage.connected_databases}."
        ),
        limit=limit,
        used=usage.connected_databases,
        upgrade_to=_next_plan(plan),
    )


def clamp_options(plan: Plan, options: dict) -> dict:
    """Reduce requested settings to what the plan permits.

    Clamped rather than rejected. A free-tier user who sends
    ``quality_tier="thorough"`` — from the docs, from an old SDK, from a shared
    snippet — gets a fast answer, not a 402. Refusing the whole question
    because one optional field was too ambitious turns an upsell into an
    outage.

    The one thing that is *not* silently clamped is anything the plan does not
    merely limit but does not have at all; those are absent from ``features``
    and refused at the endpoint, where the message can say so.
    """
    adjusted = dict(options or {})

    if plan.max_quality_tier == "fast" and adjusted.get("quality_tier") == "thorough":
        adjusted["quality_tier"] = "fast"

    # Voting is the same trade under a different name — N generations instead
    # of one — so a plan capped at `fast` must not be able to buy it directly.
    if plan.max_quality_tier == "fast":
        adjusted["vote_samples"] = 1
        adjusted["use_critic"] = False

    return adjusted


def _next_plan(plan: Plan) -> PlanName | None:
    order: list[PlanName] = ["FREE", "PRO", "ENTERPRISE"]
    index = order.index(plan.name)
    return order[index + 1] if index + 1 < len(order) else None
