"""Tests for the clarification and pre-screen checks.

Both are optional, both add a model call, and both share one property worth
protecting: an optional check must never become a new way for the tool to fail.
An error inside either resolves to "carry on".
"""

from __future__ import annotations

from sqlagent.clarify import (
    MAX_ASKS,
    Ask,
    Clarification,
    clarify_system_prompt,
    needs_clarification,
)
from sqlagent.guards.prescreen import screen
from sqlagent.llm.mantle import Completion


class StubClient:
    def __init__(self, reply: str = "", *, fail: bool = False):
        self._reply = reply
        self._fail = fail
        self.systems: list[str | None] = []

    def complete(self, prompt, *, model=None, system=None, temperature=None):
        self.systems.append(system)
        if self._fail:
            raise RuntimeError("model unavailable")
        return Completion(
            text=self._reply, input_tokens=5, output_tokens=5, model="m", seconds=0.01
        )


def clarify(reply: str = "", *, fail: bool = False, client=None, **kwargs):
    return needs_clarification(
        client or StubClient(reply, fail=fail),
        question="show me our top customers",
        schema_text="Tables: customers, orders",
        model="m",
        **kwargs,
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


def many_asks(count: int) -> str:
    entries = ",".join(
        f'{{"question": "q{i}", "options": ["a", "b"]}}' for i in range(count)
    )
    return f'{{"ambiguous": true, "asks": [{entries}]}}'


def test_asks_are_bounded():
    """A ceiling, not a target. Without it, "do a detailed study of the
    business" comes back as a questionnaire."""
    result = clarify(many_asks(20))

    assert len(result.asks) == MAX_ASKS


def test_the_bound_is_seven_not_three():
    """Three was too low for the questions people ask. "Who are our best
    customers lately and how are they doing compared to last year" has four
    genuine ambiguities; truncating to three left the fourth to be silently
    invented — the exact failure this check exists to prevent."""
    assert MAX_ASKS == 7


def test_the_bound_can_be_lowered_per_request():
    """How much back-and-forth is tolerable depends on who is asking."""
    result = clarify(many_asks(20), max_asks=2)

    assert len(result.asks) == 2


def test_a_question_with_fewer_ambiguities_is_not_padded_to_the_cap():
    """The model decides how many; the cap only stops a runaway."""
    result = clarify(many_asks(2))

    assert len(result.asks) == 2


def test_a_cap_of_zero_still_asks_about_one_thing():
    """A clarification with nothing in it stops the user and tells them
    nothing — strictly worse than not asking at all."""
    result = clarify(many_asks(5), max_asks=0)

    assert len(result.asks) == 1


def test_the_prompt_states_the_cap_it_will_be_held_to():
    """The prompt said "at most 3 entries" while the cap was a separate
    constant. Raising one without the other silently truncates a model that was
    doing as it was told."""
    client = StubClient(many_asks(1))
    clarify(client=client, max_asks=5)

    assert "up to\n5." in client.systems[0] or "up to 5" in client.systems[0]


def test_the_prompt_survives_its_own_json_example():
    """It shows the model a JSON object. Substituting with str.format would
    need every brace doubled — an escaping rule nobody editing a prompt should
    have to know."""
    text = clarify_system_prompt(7)

    assert '{"ambiguous": false}' in text
    assert "{max_asks}" not in text


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
