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
* **How many of those may be detailed.** A separate, much smaller budget,
  because a detailed question costs several times a standard one. One pooled
  number would let a Pro customer spend their whole month in an afternoon.
* **Which model tier** answers. The strong model is the expensive one, and a
  free plan pinned to the light model still works.

Three quality tiers, not two
----------------------------
``fast`` — one generation, mechanical guards only. Roughly 3 model calls. This
is the measured default and it answers most questions correctly.

``medium`` — the query is written three times and the statement that recurs is
kept. Roughly 5 calls. Self-consistency has a real mechanism behind it: where
the model is confident the samples agree and nothing changes, and where it is
guessing, the version that repeats is more often the right one.

``thorough`` (sold as **detailed**) — voting, plus a second model reviewing the
query, plus splitting a multi-part question and answering each part. 8-10 calls
and several times the latency.

**An honest note about what `thorough` is sold on.** The critic was measured on
150 BIRD questions and produced *identical* accuracy — 88/150 either way — for
1.9x the tokens, rescuing four questions and breaking four, McNemar p = 1.000.
So `thorough` is not sold on the critic. It is sold on decomposition, which
answers questions a single query cannot, and on the answer-alignment work. The
critic stays in the tier because a differently-wrong second opinion has value
to someone who has decided they want one, not because it raises the score.

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
    price_monthly_inr: int
    """Priced in both currencies, stored rather than converted.

    A rate that moves would otherwise change the advertised price daily, and
    INR 1,000 is a round number in a way that USD 15 converted is not. Local
    pricing is set, not derived.
    """

    questions_per_month: int
    """Standard questions — `fast` and `medium`. The unit customers
    understand and the one that maps to inference cost."""

    detailed_per_month: int = 0
    """How many may be `thorough`. A separate, much smaller budget.

    Separate rather than weighted against the main allowance, because a
    detailed question is several times the cost and a single pooled number lets
    a customer spend their month in an afternoon and then find the product
    stopped working.

    A plan with 0 here cannot run detailed questions at all; the request is
    clamped down to `medium` rather than refused.
    """

    max_quality_tier: Literal["fast", "medium", "thorough"] = "fast"
    """The most expensive setting this plan may request."""

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
        price_monthly_inr=0,
        questions_per_month=20,
        detailed_per_month=0,
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
        price_monthly_usd=15,
        price_monthly_inr=1_000,
        questions_per_month=400,
        detailed_per_month=50,
        max_quality_tier="thorough",
        strong_model=True,
        max_connected_databases=3,
        max_uploaded_datasets=25,
        max_seats=5,
        row_limit=5_000,
        history_retention_days=180,
        features=frozenset(
            {
                "ask", "upload", "mcp", "api", "sdk",
                "glossary", "drift", "medium", "detailed",
            }
        ),
    ),
    "ENTERPRISE": Plan(
        name="ENTERPRISE",
        label="Enterprise",
        # Negotiated. 0 means "talk to us", not "free" — the interface must
        # render this as a contact link and never as a price.
        price_monthly_usd=0,
        price_monthly_inr=0,
        questions_per_month=UNLIMITED,
        detailed_per_month=UNLIMITED,
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
                "medium", "detailed", "judge", "sso", "audit_log", "byo_model",
            }
        ),
    ),
}

CUSTOM_PRICED = frozenset({"ENTERPRISE"})
"""Plans whose price is negotiated rather than listed.

Named here so the pricing page cannot render "$0" for one of them. A zero price
means two opposite things in this table and the difference must not be left to
whoever writes the template.
"""


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
    """Standard questions — `fast` and `medium`."""

    detailed: int = 0
    """`thorough` questions, counted against their own budget and not against
    ``questions``. Counting a detailed question twice would make the headline
    allowance untrue."""

    connected_databases: int = 0
    uploaded_datasets: int = 0
    seats: int = 1


@dataclass(frozen=True, slots=True)
class Denial:
    """A refusal a customer can act on.

    Carries the limit and the plan that would lift it, because "quota
    exceeded" with no number is a support ticket and "you have used 20 of 20
    questions this month; Pro includes 400" is a decision.
    """

    reason: str
    limit: int
    used: int
    upgrade_to: PlanName | None = None

    def render(self) -> str:
        if self.upgrade_to:
            better = PLANS[self.upgrade_to]
            if self.upgrade_to in CUSTOM_PRICED:
                return f"{self.reason} {better.label} lifts the limit."
            return f"{self.reason} {better.label} includes more."
        return self.reason


def tier_of(options: dict | None) -> str:
    """Which budget a request draws on: ``thorough`` or standard."""
    return "thorough" if (options or {}).get("quality_tier") == "thorough" else "standard"


def check_quota(plan: Plan, usage: Usage, *, detailed: bool = False) -> Denial | None:
    """Whether another question may be asked. None means yes.

    Args:
        detailed: Whether this request is a `thorough` one, which draws on the
            separate and much smaller budget.

    Checked before a question starts, never during one. A tenant who reaches
    the limit on the third call of a four-call question gets that question
    finished — cutting a request in half to save one model call produces a
    broken answer and a refund, which costs more than the call.
    """
    if detailed:
        limit, used, noun = plan.detailed_per_month, usage.detailed, "detailed questions"
    else:
        limit, used, noun = plan.questions_per_month, usage.questions, "questions"

    if plan.is_unlimited(limit):
        return None
    if used < limit:
        return None

    return Denial(
        reason=f"You have used {used} of {limit} {noun} this month.",
        limit=limit,
        used=used,
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


TIER_ORDER = ("fast", "medium", "thorough")
"""Cheapest first. Clamping walks down this list rather than special-casing
pairs, so adding a tier later does not mean revisiting every comparison."""


def clamp_options(plan: Plan, options: dict, usage: Usage | None = None) -> dict:
    """Reduce requested settings to what the plan permits, and to what is left.

    Clamped rather than rejected. A free-tier user who sends
    ``quality_tier="thorough"`` — from the docs, from an old SDK, from a shared
    snippet — gets a cheaper answer, not a 402. Refusing the whole question
    because one optional field was too ambitious turns an upsell into an
    outage.

    Two separate reasons a request is clamped, and they are different:

    * the **plan** never allows this tier, so it never will without upgrading
    * the plan allows it but the **detailed budget is spent**, so it will again
      next month

    Both land on the next tier down. The caller is expected to say which
    happened; this function only decides the tier.
    """
    adjusted = dict(options or {})
    requested = adjusted.get("quality_tier")

    if requested in TIER_ORDER:
        ceiling = TIER_ORDER.index(plan.max_quality_tier)
        wanted = TIER_ORDER.index(requested)

        # Out of detailed questions is the same outcome as never having had
        # them: the next tier down.
        if (
            wanted == TIER_ORDER.index("thorough")
            and usage is not None
            and check_quota(plan, usage, detailed=True) is not None
        ):
            ceiling = min(ceiling, TIER_ORDER.index("medium"))

        if wanted > ceiling:
            adjusted["quality_tier"] = TIER_ORDER[ceiling]

    effective = adjusted.get("quality_tier", "fast")

    # Voting and the critic are the same trades under different names — N
    # generations instead of one, and a second model call. A plan capped at
    # `fast` must not be able to buy either of them directly.
    if effective == "fast":
        adjusted["vote_samples"] = 1
        adjusted["use_critic"] = False
    elif effective == "medium":
        # Medium is self-consistency and nothing else. The critic measured net
        # zero on 150 BIRD questions for 1.9x the tokens, so it is not what
        # anyone is paying for in the middle tier.
        adjusted["use_critic"] = False

    return adjusted


def _next_plan(plan: Plan) -> PlanName | None:
    order: list[PlanName] = ["FREE", "PRO", "ENTERPRISE"]
    index = order.index(plan.name)
    return order[index + 1] if index + 1 < len(order) else None
