"""Tests for multi-part question handling.

The failure behind this, observed on a real database: asked for "most ordered
for 2+ 3+ 4+ 5+ 6+ 7+ 8+", the agent answered the 2+ case and said nothing about
the other six, so the reply read as complete.
"""

from __future__ import annotations

from sqlagent.llm.mantle import Completion
from sqlagent.report import MAX_PARTS, build_synthesis_prompt, decompose


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


def split(reply: str = "", **kwargs) -> list[str]:
    return decompose(
        StubClient(reply, **kwargs),
        question="how can I increase revenue?",
        schema_text="Tables: orders, items",
        model="m",
    )


# --------------------------------------------------------------------------
# Deciding whether to split
# --------------------------------------------------------------------------


def test_a_multi_part_question_is_split():
    parts = split(
        '{"parts": ["Which items sell the most?", "Which sell the least?"]}'
    )

    assert parts == ["Which items sell the most?", "Which sell the least?"]


def test_a_single_query_question_is_left_alone():
    """Most questions are not multi-part, and the check should say so cheaply."""
    assert split('{"parts": []}') == []


def test_one_part_is_not_a_decomposition():
    """It is the original question with extra steps — a model call to arrive
    back where it started."""
    assert split('{"parts": ["how can I increase revenue?"]}') == []


def test_parts_are_capped():
    """An open-ended question must not turn into an unbounded bill."""
    many = ", ".join(f'"question {i}"' for i in range(12))
    parts = split(f'{{"parts": [{many}]}}')

    assert len(parts) == MAX_PARTS


def test_blank_parts_are_dropped():
    parts = split('{"parts": ["real question", "   ", ""]}')

    assert parts == []  # only one usable part left, which is not a split


def test_a_model_failure_falls_back_to_answering_whole():
    """Strictly better than failing: it is the behaviour from before this
    existed."""
    assert split(fail=True) == []


def test_a_malformed_reply_falls_back_to_answering_whole():
    assert split("I think you should split this into two parts") == []


def test_non_string_parts_are_ignored():
    assert split('{"parts": [1, 2, 3]}') == []


# --------------------------------------------------------------------------
# Synthesis
# --------------------------------------------------------------------------


def test_the_synthesis_prompt_keeps_each_finding_with_its_question():
    """A figure without its question is uninterpretable — "412" means nothing
    until you know it was orders in April."""
    prompt = build_synthesis_prompt(
        "how did we do?",
        [("How many orders in April?", "412 orders."), ("And in March?", "389 orders.")],
    )

    assert "How many orders in April?" in prompt
    assert "412 orders." in prompt
    assert prompt.index("April") < prompt.index("March")


def test_the_synthesis_prompt_states_the_original_question():
    prompt = build_synthesis_prompt("how did we do?", [("part", "finding")])

    assert "how did we do?" in prompt
