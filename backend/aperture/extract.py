"""Pull SQL out of a model response.

Models answer with prose, then a fenced block, then sometimes a second block
offering a variant. Feeding that whole reply to a parser produces a useless
error ("statement type COLUMN is not a read query") that teaches the repair
loop nothing, so extraction happens before validation and has its own failure
class.

Prose is the adversary here, not malice: "I cannot help *with* that" contains a
SQL keyword, so a candidate is only accepted if it actually parses as a query.
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

_FENCE = re.compile(r"```(?:sql|postgresql|postgres|mysql|sqlite)?\s*\n(.+?)```", re.S | re.I)
_BARE = re.compile(r"\b(?:WITH|SELECT)\b.+", re.S | re.I)


class NoSQLFound(ValueError):
    """The response contained no recoverable SQL statement."""


def _parses_as_query(candidate: str, dialect: str | None) -> bool:
    try:
        tree = sqlglot.parse_one(candidate, dialect=dialect)
    except Exception:
        return False
    return isinstance(tree, exp.Query) and bool(getattr(tree, "expressions", None))


def extract_sql(text: str, *, dialect: str | None = None) -> str:
    """Return the first SQL statement in `text`.

    The *first* fence is the answer; later fences are alternatives the model
    volunteers, and taking the last one silently changes the query.
    """
    if not text or not text.strip():
        raise NoSQLFound("empty response")

    candidates: list[str] = [block.strip() for block in _FENCE.findall(text)]

    bare = _BARE.search(text)
    if bare:
        candidates.append(bare.group(0).strip())

    for candidate in candidates:
        statement = candidate.split(";", 1)[0].strip() if ";" in candidate else candidate
        if _parses_as_query(statement, dialect):
            return statement

    # Nothing parsed. Return the first fenced block anyway if there was one, so
    # the validator can report a precise syntax error the model can act on.
    if candidates and _FENCE.search(text):
        return candidates[0].split(";", 1)[0].strip()
    raise NoSQLFound("response contained no SQL statement")
