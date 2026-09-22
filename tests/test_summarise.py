"""Tests for conversation summarisation.

The failure behind this: history is a sliding window, so a constraint stated in
turn 1 — "delivered orders only, ignore test accounts" — is simply gone by turn
9. The query silently widens, and the September number is computed over a
different population than the March number it is being compared against. No
error, no warning, two figures that are not comparable.
"""

from __future__ import annotations

from sqlagent.conversation import Turn, evicted_turns, render_conversation
from sqlagent.llm.mantle import Completion
from sqlagent.summarise import MAX_SUMMARY_CHARS, SummaryCache, summarise


class StubClient:
    """Records what it was asked, so the prompt can be inspected."""

    def __init__(self, reply: str = "notes", *, fail: bool = False):
        self._reply = reply
        self._fail = fail
        self.prompts: list[str] = []
        self.calls = 0

    def complete(self, prompt, *, model=None, system=None, temperature=None):
        self.calls += 1
        self.prompts.append(prompt)
        if self._fail:
            raise RuntimeError("model unavailable")
        return Completion(
            text=self._reply, input_tokens=5, output_tokens=5, model="m", seconds=0.01
        )


def turns(count: int, start: int = 0) -> list[Turn]:
    return [Turn(f"question {i}", f"SELECT {i}") for i in range(start, start + count)]


def fold(client, evicted, previous: str = "") -> str:
    return summarise(client, evicted=evicted, previous=previous, model="m")


# --------------------------------------------------------------------------
# Where the boundary is
#
# The summariser and the renderer have to agree. A turn in both is duplicated
# context; a turn in neither is silently forgotten, which is the bug this whole
# module exists to prevent.
# --------------------------------------------------------------------------


def test_nothing_is_evicted_while_the_conversation_fits_the_window():
    assert evicted_turns(turns(4), window=4) == []


def test_the_oldest_turns_are_the_evicted_ones():
    evicted = evicted_turns(turns(7), window=4)

    assert [t.question for t in evicted] == ["question 0", "question 1", "question 2"]


def test_the_window_and_the_eviction_do_not_overlap():
    all_turns = turns(9)
    shown = render_conversation(all_turns, window=4)

    for turn in evicted_turns(all_turns, window=4):
        assert turn.question not in shown


def test_every_turn_is_either_shown_or_evicted():
    all_turns = turns(9)
    shown = render_conversation(all_turns, window=4)
    carried = {t.question for t in evicted_turns(all_turns, window=4)}

    for turn in all_turns:
        assert turn.question in shown or turn.question in carried


# --------------------------------------------------------------------------
# Folding
# --------------------------------------------------------------------------


def test_evicted_turns_are_folded_into_a_note():
    assert fold(StubClient("Delivered orders only."), turns(3)) == "Delivered orders only."


def test_nothing_evicted_costs_nothing():
    """Every first question, every benchmark question, every CLI invocation."""
    client = StubClient()

    assert fold(client, []) == ""
    assert client.calls == 0


def test_the_previous_note_is_given_back_to_the_model():
    """Incremental, not recomputed: this is what keeps the cost one call per
    eviction rather than one per turn of the conversation so far."""
    client = StubClient()

    fold(client, turns(1), previous="Delivered orders only.")

    assert "Delivered orders only." in client.prompts[0]


def test_only_the_new_turns_are_sent():
    client = StubClient()

    fold(client, turns(1, start=5), previous="earlier notes")

    assert "question 5" in client.prompts[0]
    assert "question 4" not in client.prompts[0]


def test_a_failure_keeps_the_previous_note():
    """A summarisation failure degrades the conversation to the bare window —
    the behaviour before this existed — rather than breaking the answer."""
    assert fold(StubClient(fail=True), turns(3), previous="kept") == "kept"


def test_none_clears_the_note():
    """A run of unrelated lookups carries nothing forward. The note is cleared
    rather than left asserting something no longer true."""
    assert fold(StubClient("NONE"), turns(3), previous="stale") == ""


def test_none_is_recognised_with_punctuation():
    assert fold(StubClient("None."), turns(3), previous="stale") == ""


def test_the_note_is_capped():
    """Enforced on the reply, not requested in the prompt. A cap the model is
    merely told about is not a cap."""
    long_reply = "Sentence about the data. " * 200

    assert len(fold(StubClient(long_reply), turns(3))) <= MAX_SUMMARY_CHARS


def test_a_capped_note_ends_at_a_sentence():
    """Cut mid-clause, the last fact reads as something other than it is."""
    summary = fold(StubClient("Delivered only. " * 200), turns(3))

    assert summary.endswith(".")


def test_a_capped_note_without_sentences_is_still_capped():
    """No boundary to cut at is not a reason to return an uncapped note."""
    summary = fold(StubClient("x" * (MAX_SUMMARY_CHARS * 3)), turns(3))

    assert len(summary) <= MAX_SUMMARY_CHARS


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def test_the_note_is_rendered_as_still_in_force():
    """Not as history. "Delivered orders only" said nine turns ago constrains
    this question; labelled as a past exchange it reads as something that once
    applied."""
    text = render_conversation(turns(5), summary="Delivered orders only.")

    assert "Delivered orders only." in text
    assert "still apply" in text


def test_the_note_comes_before_the_turns():
    text = render_conversation(turns(5), summary="STANDING")

    assert text.index("STANDING") < text.index("question 4")


def test_a_note_alone_is_enough_to_render():
    """The window can be zero — or every turn evicted — and the standing
    context still has to reach the model."""
    assert "STANDING" in render_conversation([], summary="STANDING")


def test_no_turns_and_no_note_renders_nothing():
    """A first question must produce exactly the prompt it produced before any
    of this existed, or the benchmark numbers stop being comparable."""
    assert render_conversation([], summary="") == ""


def test_a_blank_note_is_not_rendered_as_a_section():
    assert "Standing context" not in render_conversation(turns(2), summary="   ")


# --------------------------------------------------------------------------
# The cache
# --------------------------------------------------------------------------


def test_the_same_turns_return_the_same_note():
    cache = SummaryCache()
    cache.put(turns(3), "notes")

    assert cache.get(turns(3)) == "notes"


def test_the_key_is_the_content_not_the_object():
    """History is rebuilt from the database on every request, so the Turn
    objects are new each time and only their text is stable."""
    cache = SummaryCache()
    cache.put(turns(3), "notes")

    rebuilt = [Turn(t.question, t.sql) for t in turns(3)]
    assert cache.get(rebuilt) == "notes"


def test_different_turns_are_a_miss():
    cache = SummaryCache()
    cache.put(turns(3), "notes")

    assert cache.get(turns(4)) is None


def test_the_longest_known_prefix_is_found():
    """One more turn evicted since the last question is the usual case, and it
    must fold one turn in rather than replaying the conversation."""
    cache = SummaryCache()
    cache.put(turns(3), "notes so far")

    previous, pending = cache.nearest(turns(4))

    assert previous == "notes so far"
    assert [t.question for t in pending] == ["question 3"]


def test_an_unknown_conversation_starts_from_nothing():
    previous, pending = SummaryCache().nearest(turns(3))

    assert previous == ""
    assert len(pending) == 3


def test_the_cache_is_bounded():
    cache = SummaryCache(max_entries=2)
    for i in range(1, 6):
        cache.put(turns(i), f"note {i}")

    assert len(cache) == 2


def test_the_most_recently_used_survives_eviction():
    cache = SummaryCache(max_entries=2)
    cache.put(turns(1), "one")
    cache.put(turns(2), "two")
    cache.get(turns(1))
    cache.put(turns(3), "three")

    assert cache.get(turns(1)) == "one"
    assert cache.get(turns(2)) is None
