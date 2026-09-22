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
    assert check_quota(FREE, Usage(questions=99)) is None


def test_a_tenant_at_their_limit_may_not():
    assert check_quota(FREE, Usage(questions=100)) is not None


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
    denial = check_quota(FREE, Usage(questions=100))

    assert denial.used == 100
    assert denial.limit == 100
    assert "100 of 100" in denial.render()


def test_a_denial_names_the_plan_that_would_lift_it():
    assert "Pro" in check_quota(FREE, Usage(questions=100)).render()


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
