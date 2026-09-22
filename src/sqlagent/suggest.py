"""Opening questions worth clicking.

Why the derived ones were not
-----------------------------
The first version built suggestions from the schema alone: rank tables by how
often they are referenced, then fill a template. On the production database that
produced

    How many rows are in kitchen_profiles?
    Show me kitchen_profiles joined with users
    What are the most common values in orders?

Every one is answerable and none is a question anybody has. "How many rows are
in kitchen_profiles" is a fact about the storage, not about the business, and
the join suggestion is a shape with no question in it at all. They are the
questions a *schema browser* would ask.

The suggestions are the one moment a new user decides whether this understands
their database, and spending it on row counts wastes it.

What replaces them
------------------
One model call, at startup, given the table names and the glossary — the same
two things that tell the agent what the business is. It is asked for questions a
person who runs this business would actually ask.

Why this is cheap
-----------------
Once per schema version, cached. The schema hash already changes on exactly the
events that should invalidate them, so a column rename refreshes the
suggestions and nothing else does.

Why the derived ones are still here
-----------------------------------
As the fallback. If the model call fails the interface must still offer
something, and a mediocre suggestion is better than an empty screen that makes
the tool look broken.
"""

from __future__ import annotations

import logging

from sqlagent.llm.mantle import json_from_reply

logger = logging.getLogger(__name__)

SUGGEST_SYSTEM_PROMPT = """\
You propose opening questions for a natural-language database analyst.

Reply with JSON only: {"questions": ["...", "...", "...", "..."]}

Write questions the person who RUNS this business would ask — about money,
customers, products, time, growth. Not questions about the database.

Good:   Which items made the most revenue last month?
        How many customers ordered more than once?
        Which day of the week is busiest?
Bad:    How many rows are in kitchen_profiles?      (a fact about storage)
        Show me orders joined with users            (a shape, not a question)
        What are the most common values in orders?  (nobody asks this)

Rules:
- Exactly 4 questions, each one line, no numbering.
- Each must be answerable from the tables listed, by one query.
- Prefer the business's own words, from the table names and domain notes.
- Vary them: one count, one ranking, one over time, one comparison.
- Never name a table that is not in the list.
- Skip framework tables entirely (migrations, sessions, tokens, logs).
"""

MAX_SUGGESTIONS = 4


def propose(
    client, *, tables: list[str], glossary: str, model: str
) -> list[str]:
    """Ask for opening questions. Returns an empty list on any failure.

    Never raises. Suggestions are decoration on an otherwise working interface;
    a model outage must cost chips, not the page.
    """
    if not tables:
        return []

    prompt = (
        "Tables in this database:\n"
        + "\n".join(f"  {name}" for name in tables)
        + (f"\n\n{glossary}" if glossary else "")
        + "\n\nPropose 4 opening questions. JSON only."
    )

    try:
        completion = client.complete(prompt, model=model, system=SUGGEST_SYSTEM_PROMPT)
        payload = json_from_reply(completion.text)
    except Exception as exc:  # noqa: BLE001 - decoration, never fatal
        logger.warning("could not propose suggestions: %s", exc)
        return []

    questions = [
        text.strip()
        for text in payload.get("questions", [])
        if isinstance(text, str) and text.strip()
    ]
    return questions[:MAX_SUGGESTIONS]
