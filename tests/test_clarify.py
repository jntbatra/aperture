"""Tests for the clarification and pre-screen checks.

Both are optional, both add a model call, and both share one property worth
protecting: an optional check must never become a new way for the tool to fail.
An error inside either resolves to "carry on".
"""

from __future__ import annotations

from sqlagent.clarify import MAX_ASKS, Ask, Clarification, needs_clarification
from sqlagent.guards.prescreen import screen
from sqlagent.llm.mantle import Completion


class StubClient:
    def __init__(self, reply: str = "", *, fail: bool = False):
        self._reply = reply
        self._fail = fail

    def complete(self, prompt, *, model=None, system=None, temperature=None):
        if self._fail:
            raise RuntimeError("model unavailable")
        return Completion(
            text=self._reply, input_tokens=5, output_tokens=5, model="m", seconds=0.01
        )


def clarify(reply: str = "", **kwargs):
    return needs_clarification(
        StubClient(reply, **kwargs),
        question="show me our top customers",
        schema_text="Tables: customers, orders",
        model="m",
    )


# --------------------------------------------------------------------------
# Clarification
# --------------------------------------------------------------------------


def test_an_undefined_ranking_produces_a_question():
    """"Top customers" is not one question — by revenue, orders, recency?"""
    result = clarify(
        '{"ambiguous": true, "asks": [{"question": "Top by what measure?", '
        '"options": ["total revenue", "number of orders"]}]}'
    )

    assert result is not None
    assert "measure" in result.asks[0].question
    assert "total revenue" in result.asks[0].options


def test_two_ambiguities_produce_two_separate_asks():
    """Merged into one entry they are unanswerable: the options are not
    alternatives to each other, so picking one resolves neither."""
    result = clarify(
        '{"ambiguous": true, "asks": ['
        '{"question": "Best by what?", "options": ["revenue", "order count"]},'
        '{"question": "What period?", "options": ["last 30 days", "last quarter"]}]}'
    )

    assert len(result.asks) == 2
    assert result.asks[0].options == ("revenue", "order count")
    assert result.asks[1].options == ("last 30 days", "last quarter")


def test_asks_are_bounded():
    """Past three it stops being a clarification and becomes a form."""
    many = ",".join(
        f'{{"question": "q{i}", "options": ["a", "b"]}}' for i in range(8)
    )
    result = clarify(f'{{"ambiguous": true, "asks": [{many}]}}')

    assert len(result.asks) == MAX_ASKS


def test_the_older_single_question_shape_is_still_accepted():
    """A model asked for JSON will occasionally produce the simpler form, and
    rejecting it would turn a usable clarification into a silent guess."""
    result = clarify(
        '{"ambiguous": true, "question": "Top by what?", "options": ["revenue"]}'
    )

    assert result.asks[0].question == "Top by what?"


def test_a_clear_question_is_answered_not_queried():
    assert clarify('{"ambiguous": false}') is None


def test_ambiguity_with_no_question_attached_is_ignored():
    """The user would be stopped and told nothing."""
    assert clarify('{"ambiguous": true}') is None
    assert clarify('{"ambiguous": true, "asks": []}') is None
    assert clarify('{"ambiguous": true, "asks": [{"options": ["a"]}]}') is None


def test_a_model_failure_answers_the_question_anyway():
    """Failing the request because an optional check could not run would make a
    feature into an outage mode."""
    assert clarify(fail=True) is None


def test_a_malformed_reply_answers_the_question_anyway():
    assert clarify("I think it might be ambiguous?") is None


def test_options_are_bounded():
    """A clarifying question with nine options is not a clarification."""
    result = clarify(
        '{"ambiguous": true, "asks": [{"question": "Which?", '
        '"options": ["a", "b", "c", "d", "e", "f"]}]}'
    )

    assert len(result.asks[0].options) == 4


def test_blank_options_are_dropped():
    result = clarify(
        '{"ambiguous": true, "asks": [{"question": "Which?", '
        '"options": ["revenue", "  ", ""]}]}'
    )

    assert result.asks[0].options == ("revenue",)


def test_a_clarification_renders_every_ask_for_clients_without_controls():
    rendered = Clarification(
        asks=(
            Ask("Top by what?", ("revenue", "orders")),
            Ask("What period?", ("last month",)),
        )
    ).render()

    assert "Top by what? (revenue / orders)" in rendered
    assert "What period? (last month)" in rendered


def test_an_ask_without_options_renders_plainly():
    assert Ask("Which month?").render() == "Which month?"


# --------------------------------------------------------------------------
# Pre-screen
# --------------------------------------------------------------------------


def screen_with(reply: str = "", **kwargs):
    return screen(
        StubClient(reply, **kwargs), question="how many orders?", model="m"
    )


def test_an_ordinary_question_is_allowed():
    assert screen_with('{"allow": true}')


def test_a_refusal_carries_its_reason():
    verdict = screen_with(
        '{"allow": false, "reason": "requests bulk contact details"}'
    )

    assert not verdict.allowed
    assert "contact details" in verdict.reason


def test_a_refusal_without_a_reason_still_says_something():
    """A user stopped with a blank message has no idea what happened."""
    verdict = screen_with('{"allow": false}')

    assert not verdict.allowed
    assert verdict.reason


def test_a_model_failure_allows_the_question():
    """Deliberate: this is the outermost and weakest layer, the real protections
    sit behind it, and deny-on-error would turn a throttled model into an
    outage."""
    assert screen_with(fail=True).allowed


def test_a_malformed_reply_allows_the_question():
    assert screen_with("seems fine to me").allowed
