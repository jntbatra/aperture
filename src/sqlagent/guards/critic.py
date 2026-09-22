"""Loop C: a second opinion on SQL that is about to run.

Where this sits among the existing checks
-----------------------------------------
Three things already inspect a query before it executes, and all three are
*mechanical*:

* the validator — is this a read-only SELECT over permitted tables?
* the cost gate — will the planner do something extreme?
* the fan-out check — might this aggregate be multiplied by a join?

None of them ask the question that actually determines whether the answer is
right: **does this query answer what was asked?** A query can be safe, cheap,
un-inflated and about the wrong thing entirely — filtering on the wrong column,
counting orders when the question was about customers, silently answering the
first half of a two-part question.

That judgement is semantic, so it needs a model. This is the one check here that
costs a model call, which is why it is off by default and why it exists as a
toggle the benchmark can measure rather than an assumption.

Why the critic is given a narrow job
------------------------------------
It sees the question, the schema it was written against, and the SQL. It does
**not** see the previous critique, the results, or a conversation. A reviewer
handed everything tends to restate the producer's reasoning back; a reviewer
handed one artefact and one question is harder to talk into agreement.

It is also asked for a verdict first and a reason second. A model that writes
its reasoning first talks itself into approving — the reason becomes a
justification for a conclusion already drifting into view.

Why a rejection is a repair, not a refusal
------------------------------------------
A critique routes back through the normal repair loop with the objection
attached, exactly like a database error. The model that wrote the query gets
told what is wrong with it and writes another. A critic that could only veto
would turn a recoverable mistake into a dead end.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlagent.llm.mantle import json_from_reply

logger = logging.getLogger(__name__)

CRITIC_SYSTEM_PROMPT = """\
You review SQL for correctness against a question. You are not writing SQL.

Answer with JSON only: {"ok": true} or {"ok": false, "problem": "<one sentence>"}.

Say ok: false only for a concrete, nameable defect:
- the query answers a different question than the one asked
- a filter, grouping or ordering the question requires is missing
- a filter uses the wrong column, or a value that cannot occur in that column
- the question has several parts and the query answers only some of them
- an aggregate is computed over the wrong grain (per row when per order is meant)

Say ok: true for anything else, including a query you would have written
differently. Style is not a defect. A query that answers the question by a
route you find inelegant is correct.

If you say ok: false, "problem" must name the specific thing to change. Not
"the query is wrong" but "it counts order_items rows, but the question asks how
many orders".
"""


@dataclass(frozen=True, slots=True)
class Critique:
    """The critic's verdict on one statement."""

    ok: bool
    problem: str = ""

    def __bool__(self) -> bool:
        return self.ok


def build_critic_prompt(question: str, schema_text: str, sql: str) -> str:
    """What the critic is shown.

    The schema is included because most real defects are only visible against
    it — a filter on the wrong column looks perfectly reasonable until you can
    see that the column holds something else.
    """
    return (
        f"{schema_text}\n\n"
        f"Question: {question}\n\n"
        f"Proposed SQL:\n{sql}\n\n"
        "Does this query answer the question? Reply with JSON only."
    )


def review(
    client, *, question: str, schema_text: str, sql: str, model: str, trace=None
) -> Critique:
    """Ask for a second opinion. Never raises.

    A critic that can fail the request is worse than no critic: the query under
    review is already validated and safe, so any error here — a malformed reply,
    a throttled call — resolves to approval. The check exists to catch mistakes,
    not to become a new way to fail.
    """
    try:
        completion = client.complete(
            build_critic_prompt(question, schema_text, sql),
            model=model,
            system=CRITIC_SYSTEM_PROMPT,
        )
        if trace is not None:
            trace.record(completion)

        payload = json_from_reply(completion.text)
    except Exception as exc:  # noqa: BLE001 - advisory check, never fatal
        logger.warning("critic failed, treating the query as acceptable: %s", exc)
        return Critique(ok=True)

    if payload.get("ok") is False:
        problem = str(payload.get("problem", "")).strip()
        # A rejection with no stated defect is not actionable — it would go into
        # a repair prompt as "this is wrong, try again", which reliably produces
        # a differently-wrong query. Treated as approval.
        if not problem:
            logger.info("critic rejected without naming a problem; ignoring")
            return Critique(ok=True)
        return Critique(ok=False, problem=problem)

    return Critique(ok=True)
