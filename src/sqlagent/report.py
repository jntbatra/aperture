"""Answer a question that is really several questions.

The failure this is for
-----------------------
Observed on a real database:

    Q: Give me a table for most ordered for 2+ 3+ 4+ 5+ 6+ 7+ 8+
    A: <the 2+ list, and nothing else>

Seven questions were asked. One was answered. Nothing in the reply admitted the
other six had been dropped, so the answer read as complete.

    Q: So finally how can I increase revenue?? do a detailed map study
    A: <one query, three sentences>

"A detailed study" is a request for several angles — what sells, what does not,
when, to whom — and a single SELECT cannot be one.

Both failures come from the same gap: the pipeline had exactly one shape, one
question to one query, and no way to express a question that needs more.

How this works
--------------
1. **Decompose.** A model call splits the question into sub-questions, or
   declines to. Most questions are not multi-part, and the check says so
   cheaply.
2. **Answer each** through the ordinary pipeline — same retrieval, same
   validator, same cost gate, same everything. A sub-question is just a
   question.
3. **Synthesise** one answer from the parts, quoting figures from each.

Why not let the model write one enormous query
-----------------------------------------------
It can, and the result is a query nobody can check. The value of this tool is
that the SQL is readable next to the answer; a three-hundred-line statement with
seven CTEs is technically that and practically not. Several small queries, each
inspectable, keeps the property that matters.

Why decomposition is bounded
----------------------------
Hard-capped at ``MAX_PARTS``. Without a cap, "do a detailed study" produces
however many sub-questions the model feels like, each costing model calls and a
database round trip — an open-ended question turning into an unbounded bill.
Four is enough for every real multi-part question seen so far, and the answer
says when more were requested than were run.
"""

from __future__ import annotations

import logging

from sqlagent.llm.mantle import json_from_reply

logger = logging.getLogger(__name__)

MAX_PARTS = 4
"""Hard cap on sub-questions.

Not a suggestion to the model — enforced after it replies. An open-ended
question must not be able to turn into an unbounded bill.
"""

DECOMPOSE_SYSTEM_PROMPT = """\
You split a question into the smallest number of separate database questions
needed to answer it fully.

Reply with JSON only:
  {"parts": []}                      - one query answers this; do not split
  {"parts": ["...", "..."]}          - each part is one self-contained question

Split only when the question genuinely needs several different queries:
- it asks for several distinct things ("revenue by month AND by category")
- it asks for a comparison between groups that need separate aggregation
- it asks for an open-ended analysis ("how can I increase revenue")

Do NOT split:
- a question one query answers, however complex the query
- a question that only needs more GROUP BY columns
- a follow-up that refines a previous question
- a question answered by ONE query with joins. "Get the latest order with all
  its details and the details of the customer who placed it" is a single query
  over orders, users and customer_profiles, ordered by date. Splitting it turns
  one correct answer into two that have to be stitched back together.

If a later part genuinely depends on an earlier one's result, say so in its
wording - "for the order found above, ..." - so the dependency is visible.
Parts are answered in order and each one can see what the earlier ones
returned. Do not invent an id or a value to bridge them.

Never write SQL. Produce at most 4 parts.
"""

SYNTHESIS_SYSTEM_PROMPT = """\
You write one answer from several findings, for someone who asked a single
question.

Rules:
- Answer the original question directly. Do not narrate the process.
- Quote real figures from the findings. Never invent one, and never carry a
  figure across from one finding to another.
- Where findings connect, say how. That connection is the reason several
  queries were run instead of one.
- If a finding is empty or failed, say so plainly rather than omitting it.
- No headings unless there are genuinely separate sections. A few sentences
  beats a template.
"""


def decompose(client, *, question: str, schema_text: str, model: str, trace=None) -> list[str]:
    """Split a question, or return an empty list to leave it alone.

    Never raises. A decomposition failure falls back to answering the question
    whole, which is the previous behaviour — strictly better than failing.
    """
    try:
        completion = client.complete(
            f"{schema_text}\n\nQuestion: {question}\n\n"
            "Does this need several separate queries? JSON only.",
            model=model,
            system=DECOMPOSE_SYSTEM_PROMPT,
        )
        if trace is not None:
            trace.record(completion)

        payload = json_from_reply(completion.text)
    except Exception as exc:  # noqa: BLE001 - optional step, never fatal
        logger.warning("decomposition failed, answering whole: %s", exc)
        return []

    parts = [
        str(part).strip()
        for part in payload.get("parts", [])
        if isinstance(part, str) and str(part).strip()
    ]

    # A single part is not a decomposition — it is the original question with
    # extra steps, and running it through the multi-part path would cost a model
    # call to arrive back where it started.
    if len(parts) < 2:
        return []

    if len(parts) > MAX_PARTS:
        logger.info("decomposition produced %d parts; keeping %d", len(parts), MAX_PARTS)

    return parts[:MAX_PARTS]


def build_synthesis_prompt(question: str, findings: list[tuple[str, str]]) -> str:
    """Assemble one prompt from the sub-answers.

    Findings are ``(sub-question, answer)`` pairs. The sub-questions are kept
    alongside their answers because a figure without its question is
    uninterpretable — "412" means nothing until you know it was orders in April.
    """
    body = "\n\n".join(
        f"Question: {sub}\nFinding: {answer}" for sub, answer in findings
    )
    return (
        f"The user asked: {question}\n\n"
        f"Separate queries produced these findings:\n\n{body}\n\n"
        "Write one answer to the user's original question."
    )
