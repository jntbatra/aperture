"""Keep the part of a long conversation that the turn window drops.

The failure this is for
-----------------------
History is a sliding window of the last few turns (see
:mod:`sqlagent.conversation` for why it is bounded at all). That works for a
drill-down and breaks for a *session*:

    turn 1:  only delivered orders, ignore the test accounts
    ...
    turn 9:  and what about September?

By turn 9 the constraint set in turn 1 has fallen out of the window. Nothing in
the prompt says "delivered only" any more, so the query silently widens and the
September number is computed on a different population than the March number it
is being compared against. No error, no warning, two numbers that are not
comparable — which is the same failure mode as an inflated aggregate, arriving
by a different route.

What is kept, and what is not
-----------------------------
Only **standing facts**: constraints that were stated once and are still in
force, what the user is working on, the names and ids they have been using.
Deliberately not a précis of every turn. A summary that tries to preserve the
whole conversation grows without bound, which is the problem the window exists
to solve — moving it into a paragraph does not fix it, it hides it.

``MAX_SUMMARY_CHARS`` is enforced after the model replies, not asked for in the
prompt. A cap the model is merely told about is not a cap.

Why it is incremental
---------------------
Recomputing the summary from all evicted turns on every question costs one
model call per turn for the rest of the conversation. Instead the previous
summary is passed back in with only the newly-evicted turns, so the cost is one
call *per eviction* and the cache below makes a re-asked question free.

Why a failure here is silent
----------------------------
A summarisation failure falls back to the bare window — exactly the behaviour
before this module existed. The conversation is degraded, not broken, and that
is the right trade for an enhancement that runs on the side of the real work.
"""

from __future__ import annotations

import hashlib
import logging
from collections import OrderedDict

from sqlagent.conversation import Turn

logger = logging.getLogger(__name__)

MAX_SUMMARY_CHARS = 700
"""Hard cap on the carried summary.

Enforced on the reply, not requested in the prompt. Roughly two short
paragraphs — enough for a handful of standing constraints and the subject under
discussion, and small enough that it cannot crowd out the schema, which is what
actually makes the next query correct.
"""

SUMMARY_SYSTEM_PROMPT = """\
You maintain the standing context of a conversation between an analyst and a
database.

You are given the running notes so far and some older exchanges that are about
to be forgotten. Rewrite the notes so nothing still relevant is lost.

Keep only what constrains a FUTURE question:
- filters and scopes the user established and has not withdrawn
  ("delivered orders only", "excluding test accounts", "FY25")
- what the user is investigating, in their own words
- specific entities under discussion, with the exact id or name as it appeared
- anything the user corrected you about

Drop:
- figures that were reported - they are answers, not context
- the SQL, the table names, the mechanics of how anything was computed
- exchanges that were answered and closed

Write plain sentences, no headings, no bullet points, under 120 words. If
nothing is worth keeping, reply with exactly: NONE
"""


def summarise(
    client,
    *,
    evicted: list[Turn] | tuple[Turn, ...],
    previous: str = "",
    model: str,
    trace=None,
) -> str:
    """Fold turns that fell out of the window into a standing-context note.

    Args:
        evicted: Turns no longer rendered in full, oldest first. Only the ones
            new since ``previous`` was written should be passed.
        previous: The summary these turns are being folded into. Empty the
            first time.
        model: Model id. The light tier is the right choice — this is
            compression, not reasoning.

    Returns:
        The new summary, or ``previous`` unchanged on any failure. Never
        raises: this runs alongside answering and must not be able to break it.
    """
    if not evicted:
        return previous

    body = "\n\n".join(turn.render() for turn in evicted)
    notes = previous.strip() or "(none yet - this is the first summary)"

    try:
        completion = client.complete(
            f"Running notes:\n{notes}\n\n"
            f"Exchanges about to be forgotten:\n\n{body}\n\n"
            "Rewrite the running notes.",
            model=model,
            system=SUMMARY_SYSTEM_PROMPT,
        )
        if trace is not None:
            trace.record(completion)
    except Exception as exc:  # noqa: BLE001 - side channel, never fatal
        logger.warning("conversation summarisation failed, keeping previous: %s", exc)
        return previous

    text = (completion.text or "").strip()

    # "NONE" is the model saying the evicted turns carried nothing forward —
    # which is the common case for a sequence of unrelated lookups. Treated as
    # "no standing context", not as a failure, so the notes are cleared rather
    # than left saying something that is no longer true.
    if text.upper().strip(" .") == "NONE":
        return ""

    if len(text) > MAX_SUMMARY_CHARS:
        # Cut at a sentence boundary when there is one in reach, so the summary
        # does not end mid-clause and read as a fact that was cut short.
        clipped = text[:MAX_SUMMARY_CHARS]
        stop = clipped.rfind(". ")
        text = clipped[: stop + 1] if stop > MAX_SUMMARY_CHARS // 2 else clipped.rstrip()

    return text


class SummaryCache:
    """Summaries by the turns they were built from.

    Two reasons this exists. The obvious one is cost: without it, re-opening a
    long chat and asking one more question re-summarises everything.

    The one that matters more is *incrementality*. Given the turns evicted so
    far, this can find the summary written for all-but-the-newest of them and
    hand it back as ``previous``, so each eviction folds in one turn instead of
    replaying the conversation. Without the lookup there is nothing to be
    incremental against, because the agent holds no per-conversation state
    between requests.

    Bounded and in-process. A summary is derived data — losing it costs one
    model call, so it does not belong in the database.
    """

    def __init__(self, max_entries: int = 256) -> None:
        self._entries: OrderedDict[str, str] = OrderedDict()
        self._max_entries = max_entries

    @staticmethod
    def key(evicted: list[Turn] | tuple[Turn, ...]) -> str:
        """A stable digest of the turns a summary was built from.

        Over the rendered turns, not over ``id()`` or the objects themselves:
        the history is rebuilt from the database on every request, so the
        objects are new each time and only their content is stable.
        """
        digest = hashlib.sha256()
        for turn in evicted:
            digest.update(turn.render().encode("utf-8"))
            digest.update(b"\x00")
        return digest.hexdigest()

    def get(self, evicted: list[Turn] | tuple[Turn, ...]) -> str | None:
        """The summary for exactly these turns, if it has been computed."""
        found = self._entries.get(self.key(evicted))
        if found is not None:
            self._entries.move_to_end(self.key(evicted))
        return found

    def put(self, evicted: list[Turn] | tuple[Turn, ...], summary: str) -> None:
        key = self.key(evicted)
        self._entries[key] = summary
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def nearest(
        self, evicted: list[Turn] | tuple[Turn, ...]
    ) -> tuple[str, list[Turn]]:
        """The longest known prefix of ``evicted``, and the turns after it.

        Returns ``("", all of them)`` when nothing is known, which is the
        correct starting state rather than an error.

        Searched longest-prefix-first so the usual case — one new eviction
        since the last question — is the first lookup and hits immediately.
        """
        turns = list(evicted)
        for cut in range(len(turns) - 1, 0, -1):
            found = self._entries.get(self.key(turns[:cut]))
            if found is not None:
                return found, turns[cut:]
        return "", turns

    def __len__(self) -> int:
        return len(self._entries)
