"""Tests for plan tiers and quotas.

These gate inference, not features. A question costs three model calls by
default and six on `thorough`, so a pricing model that ignores the quality tier
sells a fixed price against a variable cost of goods — which works until a
customer finds the toggle.
"""

from __future__ import annotations

import pytest

from sqlagent.saas.plans import (
    PLANS,
    UNLIMITED,
    Usage,
    check_can_connect,
    check_quota,
    clamp_options,
    plan_for,
)

FREE = PLANS["FREE"]
PRO = PLANS["PRO"]
ENTERPRISE = PLANS["ENTERPRISE"]


# --------------------------------------------------------------------------
# Resolving a plan
# --------------------------------------------------------------------------


def test_a_known_plan_resolves():
    assert plan_for("PRO").name == "PRO"


def test_plan_names_are_case_insensitive():
    assert plan_for("pro").name == "PRO"


@pytest.mark.parametrize("name", [None, "", "PROO", "premium", "enterprise-plus"])
def test_an_unknown_plan_resolves_to_the_most_restrictive(name):
    """A typo, a renamed plan, a row written by an older version — every one
    of those should under-serve rather than hand out an enterprise
    entitlement to whoever caused it."""
    assert plan_for(name).name == "FREE"


def test_the_plan_names_match_unilinks_enum():
    """The two products are meant to share a billing story. A mismatch means a
    translation table living somewhere for the rest of time."""
    assert set(PLANS) == {"FREE", "PRO", "ENTERPRISE"}


# --------------------------------------------------------------------------
# Question quota
# --------------------------------------------------------------------------


def test_a_tenant_under_their_limit_may_ask():
    assert check_quota(FREE, Usage(questions=19)) is None


def test_a_tenant_at_their_limit_may_not():
    assert check_quota(FREE, Usage(questions=20)) is not None


def test_an_unlimited_plan_is_never_denied():
    assert check_quota(ENTERPRISE, Usage(questions=10_000_000)) is None


def test_unlimited_is_a_sentinel_not_none():
    """`None` invites `if limit and used > limit`, which is right for None and
    silently wrong for a limit of 0 — a plan meant to allow nothing would allow
    everything."""
    assert ENTERPRISE.questions_per_month == UNLIMITED
    assert UNLIMITED < 0


def test_a_zero_limit_denies_rather_than_permits():
    """The bug the sentinel exists to prevent, asserted directly."""
    from dataclasses import replace

    nothing = replace(FREE, questions_per_month=0)

    assert check_quota(nothing, Usage(questions=0)) is not None


def test_a_denial_carries_the_numbers():
    """"Quota exceeded" with no number is a support ticket. "100 of 100, Pro
    includes 2,000" is a decision."""
    denial = check_quota(FREE, Usage(questions=20))

    assert denial.used == 20
    assert denial.limit == 20
    assert "20 of 20" in denial.render()


def test_a_denial_names_the_plan_that_would_lift_it():
    assert "Pro" in check_quota(FREE, Usage(questions=20)).render()


def test_the_top_plan_has_nothing_to_upgrade_to():
    from dataclasses import replace

    capped = replace(ENTERPRISE, questions_per_month=5)

    assert check_quota(capped, Usage(questions=5)).upgrade_to is None


# --------------------------------------------------------------------------
# Connected databases
# --------------------------------------------------------------------------


def test_free_connects_one_database():
    assert check_can_connect(FREE, Usage(connected_databases=0)) is None
    assert check_can_connect(FREE, Usage(connected_databases=1)) is not None


def test_pro_connects_more():
    assert check_can_connect(PRO, Usage(connected_databases=2)) is None


def test_enterprise_is_unlimited():
    assert check_can_connect(ENTERPRISE, Usage(connected_databases=500)) is None


def test_the_connection_denial_reads_naturally_for_one():
    assert "1 database;" in check_can_connect(FREE, Usage(connected_databases=1)).render()


# --------------------------------------------------------------------------
# The AI dimension
#
# This is where the money goes: `thorough` is ~3x the latency and ~4x the
# tokens of the default.
# --------------------------------------------------------------------------


def test_free_may_not_buy_the_expensive_quality_tier():
    assert clamp_options(FREE, {"quality_tier": "thorough"})["quality_tier"] == "fast"


def test_free_may_not_buy_the_middle_tier_either():
    assert clamp_options(FREE, {"quality_tier": "medium"})["quality_tier"] == "fast"


def test_pro_may():
    assert clamp_options(PRO, {"quality_tier": "thorough"})["quality_tier"] == "thorough"


def test_voting_is_clamped_too():
    """The same trade under a different name — N generations instead of one.
    Clamping the tier and leaving voting open sells the upgrade for free."""
    assert clamp_options(FREE, {"vote_samples": 5})["vote_samples"] == 1


def test_the_critic_is_clamped_too():
    assert clamp_options(FREE, {"use_critic": True})["use_critic"] is False


def test_options_are_clamped_not_rejected():
    """A free user sending `thorough` — from the docs, an old SDK, a shared
    snippet — gets a fast answer, not a 402. Refusing the whole question
    because one optional field was too ambitious turns an upsell into an
    outage."""
    clamped = clamp_options(FREE, {"quality_tier": "thorough", "initial_hops": 2})

    assert clamped["initial_hops"] == 2
    assert clamped["quality_tier"] == "fast"


def test_clamping_does_not_mutate_the_caller_s_options():
    requested = {"quality_tier": "thorough"}
    clamp_options(FREE, requested)

    assert requested["quality_tier"] == "thorough"


def test_clamping_nothing_is_safe():
    assert clamp_options(FREE, {}) == {"vote_samples": 1, "use_critic": False}


# --------------------------------------------------------------------------
# The detailed budget
#
# Separate from the main allowance because a detailed question costs several
# times a standard one. One pooled number lets a customer spend their month in
# an afternoon and then find the product stopped working.
# --------------------------------------------------------------------------


def test_detailed_questions_have_their_own_budget():
    assert check_quota(PRO, Usage(detailed=49), detailed=True) is None
    assert check_quota(PRO, Usage(detailed=50), detailed=True) is not None


def test_spending_the_detailed_budget_leaves_the_standard_one_alone():
    """Counting a detailed question against both would make the headline
    allowance untrue."""
    assert check_quota(PRO, Usage(questions=0, detailed=50)) is None


def test_spending_the_standard_budget_leaves_detailed_alone():
    assert check_quota(PRO, Usage(questions=400, detailed=0), detailed=True) is None


def test_free_has_no_detailed_budget_at_all():
    assert FREE.detailed_per_month == 0
    assert check_quota(FREE, Usage(detailed=0), detailed=True) is not None


def test_running_out_of_detailed_drops_to_medium_not_to_fast():
    """Out of budget is not the same as never having had it. A Pro customer
    keeps the middle tier they are paying for."""
    clamped = clamp_options(PRO, {"quality_tier": "thorough"}, Usage(detailed=50))

    assert clamped["quality_tier"] == "medium"


def test_with_budget_left_a_pro_request_is_not_clamped():
    clamped = clamp_options(PRO, {"quality_tier": "thorough"}, Usage(detailed=10))

    assert clamped["quality_tier"] == "thorough"


def test_the_denial_names_the_right_budget():
    assert "detailed questions" in check_quota(
        PRO, Usage(detailed=50), detailed=True
    ).render()


def test_enterprise_detailed_is_unlimited():
    assert check_quota(ENTERPRISE, Usage(detailed=1_000_000), detailed=True) is None


# --------------------------------------------------------------------------
# Three tiers
# --------------------------------------------------------------------------


def test_medium_is_voting_without_the_critic():
    """The critic measured net zero on 150 BIRD questions for 1.9x the tokens.
    Putting it in the middle tier would be charging for latency."""
    clamped = clamp_options(PRO, {"quality_tier": "medium"})

    assert clamped["use_critic"] is False
    assert "vote_samples" not in clamped or clamped["vote_samples"] != 1


def test_the_tiers_are_ordered_cheapest_first():
    from sqlagent.saas.plans import TIER_ORDER

    assert TIER_ORDER == ("fast", "medium", "thorough")


def test_the_engine_knows_the_same_three_tiers():
    """A tier the pricing page sells and the engine rejects is a 422 nobody can
    act on."""
    from sqlagent.config import Settings
    from sqlagent.saas.plans import TIER_ORDER

    for tier in TIER_ORDER:
        Settings(_env_file=None, database_url="sqlite://", quality_tier=tier)


def test_the_benchmark_harness_knows_the_same_three_tiers():
    """It did not. `--tier` was declared `["fast", "thorough"]` and stayed that
    way when `medium` was added, so a sweep over all three skipped the middle
    one and reported nothing but a usage message — which is only visible to
    someone reading the log. A measurement you cannot take is worse than one
    that comes out wrong, because nothing signals it."""
    import importlib.util
    import sys
    from pathlib import Path

    from sqlagent.saas.plans import TIER_ORDER

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("bird_cli", root / "benchmarks" / "bird.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["bird_cli"] = module
    spec.loader.exec_module(module)

    source = (root / "benchmarks" / "bird.py").read_text()
    assert "choices=list(TIERS)" in source, (
        "the harness should read the tiers from the engine, not restate them"
    )
    assert module.TIERS == TIER_ORDER


def test_medium_and_thorough_both_vote():
    from sqlagent.config import Settings, vote_samples

    for tier in ("medium", "thorough"):
        config = Settings(_env_file=None, database_url="sqlite://", quality_tier=tier)
        assert vote_samples(config) >= 3


def test_only_thorough_runs_the_critic_and_decomposition():
    from sqlagent.config import Settings, critic_enabled, decompose_enabled

    def config(tier):
        return Settings(_env_file=None, database_url="sqlite://", quality_tier=tier)

    assert not critic_enabled(config("medium"))
    assert critic_enabled(config("thorough"))
    assert not decompose_enabled(config("medium"))
    assert decompose_enabled(config("thorough"))


# --------------------------------------------------------------------------
# Pricing
# --------------------------------------------------------------------------


def test_pro_is_priced_in_both_currencies():
    """Stored, not converted. A rate that moves would change the advertised
    price daily, and 1,000 is a round number in a way that 15 converted is
    not."""
    assert PRO.price_monthly_usd == 15
    assert PRO.price_monthly_inr == 1_000


def test_enterprise_is_not_free():
    """Zero means two opposite things in this table. A pricing page that reads
    the number without the flag renders "$0" beside "unlimited"."""
    from sqlagent.saas.plans import CUSTOM_PRICED

    assert ENTERPRISE.name in CUSTOM_PRICED
    assert FREE.name not in CUSTOM_PRICED


def test_free_really_is_free():
    assert FREE.price_monthly_usd == 0
    assert FREE.price_monthly_inr == 0


def test_free_is_pinned_to_the_light_model():
    """It measured 58.7% on BIRD, so a free plan on it is a real product
    rather than a crippled demo — the difference between a free tier that
    converts and one that teaches people the tool does not work."""
    assert not FREE.strong_model
    assert PRO.strong_model


# --------------------------------------------------------------------------
# Features are an allow-list
# --------------------------------------------------------------------------


def test_a_plan_only_has_what_it_was_given():
    assert FREE.allows("ask")
    assert not FREE.allows("sso")


def test_a_new_feature_is_not_implicitly_granted():
    """Getting this backwards means a new feature silently ships to the free
    tier on deploy day."""
    for plan in PLANS.values():
        assert not plan.allows("a_feature_invented_after_this_test_was_written")


def test_paid_features_are_absent_from_free():
    for feature in ("api", "sdk", "drift", "thorough", "sso", "audit_log"):
        assert not FREE.allows(feature), feature


def test_the_free_tier_can_still_do_the_main_thing():
    """A free tier that cannot ask a question is a landing page."""
    assert FREE.allows("ask")
    assert FREE.questions_per_month > 0


# --------------------------------------------------------------------------
# Limits are consistent with each other
# --------------------------------------------------------------------------


def test_each_plan_is_at_least_as_generous_as_the_one_below():
    """Written as a test rather than trusted, because the limits are eleven
    numbers across three plans and an inversion is invisible by eye."""
    order = [FREE, PRO, ENTERPRISE]
    fields = (
        "questions_per_month",
        "max_connected_databases",
        "max_uploaded_datasets",
        "max_seats",
        "row_limit",
        "history_retention_days",
    )

    for lower, higher in zip(order, order[1:], strict=False):
        for field in fields:
            low, high = getattr(lower, field), getattr(higher, field)
            if high == UNLIMITED:
                continue
            assert low <= high, f"{field}: {lower.name}={low} > {higher.name}={high}"


def test_features_accumulate_up_the_tiers():
    assert FREE.features <= PRO.features <= ENTERPRISE.features
