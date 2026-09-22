"""Ask, when a question has more than one defensible answer.

Why guessing is the wrong default for some questions
----------------------------------------------------
"Show me our top customers" is not one question. Top by revenue, by order count,
by recency? Over what period? An agent that picks one silently produces a number
that is correct for a question nobody asked, and the user has no way to tell —
the SQL is right there, but reading it is exactly the work the tool was supposed
to save.

Asking costs one round trip. Being confidently wrong costs a decision.

Why this is on by default
-------------------------
It was not, and the reason it was not is worth recording: an automated benchmark
cannot answer a clarifying question, so enabling this would stall every
ambiguous BIRD question and produce a score describing a system nobody runs.

That optimised the measurement rather than the tool. Observed on a real
database: "who are our best customers and how are they doing lately" silently
became "by revenue", with "lately" dropped entirely — no date filter at all —
and returned a confident three-table answer with nothing to indicate it had
guessed twice.

The benchmark now declares ``best_effort`` for itself. That is where the setting
belongs: the harness states that it cannot be asked anything, rather than every
human inheriting a default chosen for the harness's convenience.

Asking about *every* ambiguity matters as much as asking at all. An early
version asked only about "best" and left the agent to invent the period, which
reintroduced exactly the failure the check exists to prevent.

The reply is rarely self-contained
----------------------------------
It is "by revenue", or "the second one", or "yes". So a turn that asked
something is recorded as having asked — see ``conversation.Turn.clarification``
— rather than as a turn with no SQL, which the history would otherwise render as
"that question could not be answered": the opposite of what happened.

What counts as ambiguous
------------------------
Deliberately narrow. Three kinds, and nothing else:

* **An undefined ranking.** "Top", "best", "worst" with no stated measure.
* **An unbounded period** where the data spans many. "Recently", "lately".
* **A term with two plausible tables behind it** — the ``cart_items`` versus
  ``order_items`` problem, when the glossary does not settle it.

Not ambiguous: a question that is merely hard, or one whose answer the model is
unsure of. Uncertainty is not ambiguity, and conflating them produces a tool
that asks a question back every time it is unconfident, which is intolerable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlagent.llm.mantle import json_from_reply

logger = logging.getLogger(__name__)

CLARIFY_SYSTEM_PROMPT_TEMPLATE = """\
You decide whether a question about a database has more than one defensible
answer. You do not answer it.

Reply with JSON only:
  {"ambiguous": false}
  {"ambiguous": true, "asks": [
      {"question": "<short question>", "options": ["a", "b"]},
      {"question": "<another, only if genuinely separate>", "options": ["c", "d"]}
  ]}

One entry per ambiguity. Do NOT merge two ambiguities into one entry: "best by
revenue or frequency / last 30 days or last quarter" is two questions, and
offering it as one leaves the user unable to answer either precisely.

Say ambiguous: true ONLY for these cases:
- a ranking with no stated measure ("top customers" - by revenue? by orders?)
- a time word with no bounds, where the data spans many periods ("recently")
- a word that maps to two different tables or columns, both plausible

Say ambiguous: false for everything else, including:
- questions you find hard
- questions where you are unsure of the answer
- questions with an obvious default reading
- follow-ups whose missing piece the conversation above already supplies. "How
  many of them ordered more than once?" is NOT ambiguous when the previous turn
  established who "them" are. Resolve it against the conversation first, and
  only call it ambiguous if it is still ambiguous afterwards.

Uncertainty is not ambiguity. If one reading is clearly the common one, take it.

"Who are our best customers lately?" is ambiguous twice - what "best" measures,
and what period "lately" covers - so it produces two entries. Asking about only
one leaves the agent to silently invent the other, which is the failure this
check exists to prevent. A question with four genuine ambiguities gets four
entries; do not stop at two because two feels like enough.

Each "question" must be answerable in a few words, and each "options" list must
hold real alternatives, not "please clarify".

Produce as many entries as the question genuinely has ambiguities, up to
{max_asks}. Do not pad: a question with one unclear word gets one entry. Do not
merge to stay under the limit either — if there are three genuine ambiguities,
ask three.
"""



CLARIFICATION_ERROR = "needs_clarification"
"""The ``error`` marker a turn carries when the agent asked instead of answered.

One constant because four places compare against it — the graph that sets it,
the pipeline that sets it on the decomposition path, the API that renders a
clarification differently from a failure, and drift detection that must not
count it as one. It was a literal string in each, and the fourth got it right
only by luck.
"""

MAX_ASKS = 7
"""Default ceiling on how many things to ask at once.

A ceiling, not a target. The model decides how many ambiguities a question
actually has and the cap only stops a runaway — "do a detailed study of the
business" should not come back as a questionnaire.

It was 3, which was too low for the questions people actually ask. "Who are
our best customers lately and how are they doing compared to last year" has
four genuine ambiguities — what "best" measures, what "lately" covers, what
"doing" means, and which comparison period — and truncating to three left the
fourth to be silently invented, which is the exact failure this check exists to
prevent. A question that needs four answers and gets three asked is worse than
one that gets all four asked, because the user believes they have pinned it
down.

Overridable per request as ``max_clarifying_questions``: how much
back-and-forth is tolerable is a property of who is asking, not of the code.
"""

def clarify_system_prompt(max_asks: int = MAX_ASKS) -> str:
    """The system prompt with the cap filled in.

    The number is substituted rather than hardcoded so the prompt and the
    enforcement below cannot disagree. They did: the prompt said "at most 3
    entries" while the cap was a separate constant, and raising one without the
    other would silently truncate a model that was doing as it was told.

    A plain replace, not ``str.format``: the prompt shows the model a JSON
    example, and every brace in it would have to be doubled to survive
    formatting — an escaping rule nobody editing a prompt should have to know.
    """
    return CLARIFY_SYSTEM_PROMPT_TEMPLATE.replace("{max_asks}", str(max_asks))


@dataclass(frozen=True, slots=True)
class Ask:
    """One thing to pin down, with the alternatives on offer."""

    question: str
    options: tuple[str, ...] = ()

    def render(self) -> str:
        if not self.options:
            return self.question
        return f"{self.question} ({' / '.join(self.options)})"


@dataclass(frozen=True, slots=True)
class Clarification:
    """Everything to pin down before answering.

    A list, not one question, because a question is frequently ambiguous more
    than once. "Who are our best customers lately?" is ambiguous twice — what
    "best" measures and what period "lately" covers — and an earlier version
    merged both into a single entry reading
    ``(by revenue or by order frequency / last 30 days or last quarter)``.

    That is unanswerable. The options are not alternatives to each other, so
    picking one resolves neither dimension, and the agent went on to hedge both
    ways and silently invent the period anyway — the exact failure this check
    exists to prevent.
    """

    asks: tuple[Ask, ...]

    def render(self) -> str:
        """One string, for storage and for clients that cannot render controls.

        The structured form goes to the browser, which can draw a row of buttons
        per ambiguity. Anything else gets this.
        """
        return "  ".join(ask.render() for ask in self.asks)


def needs_clarification(
    client,
    *,
    question: str,
    schema_text: str,
    model: str,
    conversation: str = "",
    max_asks: int = MAX_ASKS,
    trace=None,
) -> Clarification | None:
    """Decide whether to ask before answering. Never raises.

    ``conversation`` is the rendered history. Without it the check judges every
    question in isolation, and a follow-up is ambiguous in isolation almost by
    definition — "how many of them ordered more than once?" has no referent for
    "them" unless the previous turn is visible. Observed: that exact question
    was questioned back, mid-conversation, when the thread had already
    established who "them" were.

    A failure here resolves to "not ambiguous", so the question is answered on a
    best guess. The alternative — failing the request because the ambiguity
    check could not run — would make an optional feature into a new outage mode.
    """
    try:
        completion = client.complete(
            f"{schema_text}\n\n"
            + (f"{conversation}\n\n" if conversation else "")
            + f"Question: {question}\n\n"
            "Does this question have more than one defensible answer? JSON only.",
            model=model,
            system=clarify_system_prompt(max_asks),
        )
        if trace is not None:
            trace.record(completion)

        payload = json_from_reply(completion.text)
    except Exception as exc:  # noqa: BLE001 - optional check, never fatal
        logger.warning("ambiguity check failed, answering directly: %s", exc)
        return None

    if payload.get("ambiguous") is not True:
        return None

    # `asks` is the current shape; a bare question/options pair is accepted too,
    # because a model asked for JSON will occasionally produce the simpler form
    # and rejecting it would turn a usable clarification into a silent guess.
    entries = payload.get("asks")
    if not isinstance(entries, list) or not entries:
        entries = [{"question": payload.get("question"), "options": payload.get("options")}]

    asks: list[Ask] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        question_text = str(entry.get("question") or "").strip()
        if not question_text:
            continue
        options = entry.get("options") or []
        asks.append(
            Ask(
                question=question_text,
                options=tuple(
                    str(option).strip()
                    for option in options
                    if isinstance(option, str | int | float) and str(option).strip()
                )[:4],
            )
        )

    # A clarification with nothing in it is worse than none: the user is stopped
    # and told nothing. Treated as unambiguous.
    if not asks:
        logger.info("ambiguity reported with no question attached; ignoring")
        return None

    # Enforced after the reply as well as asked for in the prompt. A cap the
    # model is merely told about is not a cap.
    return Clarification(asks=tuple(asks[: max(1, max_asks)]))
