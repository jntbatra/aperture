"""Does the result answer the question that was asked?

The gap this fills
------------------
Measured over 150 BIRD questions: **61 of 62 failures were a valid query that
returned the wrong rows.** Not a syntax error, not a refused statement, not a
timeout. The query parsed, passed every guard, executed, and answered a
different question.

Nothing in the pipeline could see it, because of what each check is given:

===================  ========  =====  ========  ========
check                question  SQL    schema    **rows**
===================  ========  =====  ========  ========
``clarify``          yes       no     names     **no**
``critic``           yes       yes    yes       **no**
``faithfulness``     **no**    no     no        yes
===================  ========  =====  ========  ========

Every one is *question ↔ SQL* or *rows ↔ prose*. **Nothing compares the
question against the rows**, which is precisely where those 61 failures live.

The critic is not the answer, twice measured
--------------------------------------------
Its first rule is already "the query answers a different question than the one
asked", so it is aimed correctly and it is not too strict. It measured net zero
accuracy for 1.9x the tokens (McNemar p = 1.000), then net −6 inside
``thorough``. It fails because its signature has no rows in it: it reads code
and guesses.

On the worst failure seen on real data it would have approved the query. The
SQL looked entirely reasonable. What gave it away was the *result* — a cohort
supposedly of "customers who ordered once from one category" that turned out to
be the entire customer base.

So this runs **after execution**, sees everything, and replaces the critic
rather than joining it. Same cost, strictly more information.

Three outcomes, not two
-----------------------
``answers`` — write the answer.

``mismatch`` — regenerate, with the named reason in the repair prompt. The
reason is required: "this is wrong, try again" reliably produces a
differently-wrong query.

``ask`` — stop and put a question to the user. This is the one a benchmark
cannot reward: an automated harness has nobody to ask, so every ``ask``
scores as a failure and the measured number *understates* the real behaviour.
That is a known and accepted cost. The alternative — guessing silently — is
the exact behaviour that produced the failures above, and a tool that invents a
filter rather than asking about it is worse than one that scores lower.

Facts before opinions
---------------------
Two of the three failure shapes are arithmetic, not judgement, and
:mod:`sqlagent.guards.evidence` settles them against the database before this
runs: a filter comparing against a value that occurs nowhere, and a filter that
excluded nothing. Those are handed over as established facts rather than left
for a model to intuit from SQL text.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from sqlagent.llm.mantle import json_from_reply

logger = logging.getLogger(__name__)

MAX_PREVIEW_ROWS = 10
MAX_PREVIEW_CHARS = 2000
"""How much of the result the check sees.

Enough to judge whether the shape and magnitude answer the question, and
bounded because this runs on every query and a thousand-row result would cost
more in tokens than the query that produced it. The row *count* is always
given in full, which is the part that catches "returned the whole table".
"""

MAX_ASK_OPTIONS = 4

INTENT_SYSTEM_PROMPT = """\
You are given a question, the SQL that was run, and the rows it returned. Decide
whether those rows answer that question.

You are the last check before the user sees an answer. The query is already
known to be valid, safe and cheap — none of that is your concern. Your only
concern is whether it answers what was asked.

Reply with JSON only, one of:
  {"verdict": "answers"}
  {"verdict": "mismatch", "reason": "<one sentence naming what is wrong>"}
  {"verdict": "ask", "question": "<short question>", "options": ["a", "b"]}

Say "mismatch" when the rows demonstrably do not answer the question:
- a constraint the question stated is missing from the query
- the result is labelled as one thing and computed as another (a ratio of A to
  B reported as a ratio of B to A)
- the grain is wrong — per row where per order was meant, or the reverse
- the question asked for several things and only some were computed
- the magnitude contradicts the question: a filtered cohort the size of the
  whole table, a "top 5" with 200 rows

Say "ask" when the question genuinely has more than one defensible reading and
the rows cannot settle which was meant. Prefer this to guessing. A named value
in the question that matches nothing in the data is always an "ask", never a
silent omission — say what you could not find and offer what does exist.

Say "answers" for everything else, including a query you would have written
differently and a result you find surprising. Surprising is not wrong. An
empty result is not automatically wrong: some questions have no matching rows,
and saying so is a correct answer.

If you say "mismatch", the reason must name the specific thing to change. Not
"the query is wrong" but "it counts every customer, but the question asks only
about customers whose first order was in that category".
"""


@dataclass(frozen=True, slots=True)
class Verdict:
    """What to do with a result that has already been produced."""

    verdict: Literal["answers", "mismatch", "ask"]
    reason: str = ""
    question: str = ""
    options: tuple[str, ...] = ()

    withheld_question: str = ""
    """What it would have asked, when asking was switched off.

    Recorded rather than only logged because the benchmark runs with asking
    off, so without this the harness cannot report how many answers it gave by
    guessing — and "0 asks" would read as a finding when it is a setting.
    """

    @property
    def ok(self) -> bool:
        return self.verdict == "answers"

    def render(self) -> str:
        if self.verdict == "mismatch":
            return self.reason
        if self.verdict == "ask":
            if self.options:
                return f"{self.question} ({' / '.join(self.options)})"
            return self.question
        return ""


def build_intent_prompt(
    *,
    question: str,
    conversation: str,
    sql: str,
    schema_text: str,
    result_preview: str,
    row_count: int,
    truncated: bool,
    evidence: str = "",
) -> str:
    """Assemble the one prompt this check makes.

    ``evidence`` is the deterministic findings, placed *before* the rows and
    labelled as established. A model shown "this filter matches nothing" as a
    fact behaves differently from one asked to notice it.
    """
    parts = []
    if conversation:
        parts.append(conversation)
    parts.append(f"Question: {question}")
    parts.append(f"Schema available:\n{schema_text}")
    parts.append(f"SQL that ran:\n{sql}")

    shown = f"{row_count} rows" + (" (truncated)" if truncated else "")
    parts.append(f"Returned {shown}:\n{result_preview}")

    if evidence:
        parts.append(
            "Already established about this query, by checking the database "
            "directly — these are facts, not guesses:\n" + evidence
        )

    parts.append("Do these rows answer the question? JSON only.")
    return "\n\n".join(parts)


def check_intent(
    client,
    *,
    question: str,
    sql: str,
    schema_text: str,
    result_preview: str,
    row_count: int,
    model: str,
    conversation: str = "",
    truncated: bool = False,
    evidence: str = "",
    allow_ask: bool = True,
    trace=None,
) -> Verdict:
    """Judge a finished result. Never raises.

    Args:
        allow_ask: When False, an "ask" is downgraded to "answers" rather than
            stopping. Set by the benchmark harness, which has nobody to ask —
            and downgrading to *answers* rather than to *mismatch* is
            deliberate: an unanswerable question should not also trigger a
            regeneration that cannot possibly be better informed.

    Returns:
        A verdict. Any failure returns ``answers``, because this is the last
        thing between a working result and the user: a check that cannot run
        must not be able to withhold an answer that was correctly produced.
    """
    prompt = build_intent_prompt(
        question=question,
        conversation=conversation,
        sql=sql,
        schema_text=schema_text,
        result_preview=result_preview,
        row_count=row_count,
        truncated=truncated,
        evidence=evidence,
    )

    try:
        completion = client.complete(prompt, model=model, system=INTENT_SYSTEM_PROMPT)
        if trace is not None:
            trace.record(completion)
        payload = json_from_reply(completion.text)
    except Exception as exc:  # noqa: BLE001 - last gate, never fatal
        logger.warning("intent check failed, accepting the result: %s", exc)
        return Verdict("answers")

    if not isinstance(payload, dict):
        return Verdict("answers")

    verdict = str(payload.get("verdict", "")).strip().lower()

    if verdict == "mismatch":
        reason = str(payload.get("reason", "")).strip()
        # A rejection with no named defect goes into a repair prompt as "this
        # is wrong, try again", which reliably produces a differently-wrong
        # query. Treated as acceptance — the same rule the critic already
        # learned the hard way.
        if not reason:
            logger.info("intent: mismatch with no reason given; accepting")
            return Verdict("answers")
        return Verdict("mismatch", reason=reason)

    if verdict == "ask":
        asked = str(payload.get("question", "")).strip()
        if not asked:
            return Verdict("answers")
        if not allow_ask:
            logger.info("intent: would have asked %r; answering instead", asked)
            return Verdict("answers", withheld_question=asked)
        options = tuple(
            str(o).strip()
            for o in (payload.get("options") or [])
            if isinstance(o, str | int | float) and str(o).strip()
        )[:MAX_ASK_OPTIONS]
        return Verdict("ask", question=asked, options=options)

    return Verdict("answers")
