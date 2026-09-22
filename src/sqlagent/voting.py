"""Generate a query several times and keep the one that recurs.

The idea
--------
Self-consistency: sample the same prompt more than once and take the majority.
Where a model is confident the samples agree and voting changes nothing; where
it is guessing they scatter, and the modal answer is more often right than any
single draw.

Why it needs a non-zero temperature to mean anything
----------------------------------------------------
This system runs at temperature 0 by default, deliberately — the same question
must produce the same SQL, which is what makes caching meaningful and benchmark
runs reproducible. Three samples at temperature 0 are three identical strings
and a bill for three model calls.

So voting raises the temperature for its samples, and only for those. That is
the trade being made explicit: reproducibility is given up for the questions
where voting is switched on, and kept everywhere else.

Comparing queries, not strings
------------------------------
``SELECT a, b FROM t`` and ``select a,b from t`` are the same query. Votes are
counted over a normalised form — parsed and re-rendered by sqlglot — so
formatting differences do not split a majority three ways. Parsing failures fall
back to comparing whitespace-collapsed text, because an unparseable candidate
should still be able to lose a vote rather than crash the count.

What this does not do
---------------------
It does not *execute* the candidates and compare result sets. That would be a
stronger signal and costs three times the database load per question, on a
production replica, for a benchmark-time gain. Not worth it by default; the
hook is here if it ever is.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass

import sqlglot

logger = logging.getLogger(__name__)

_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class Vote:
    """The outcome of one round of voting."""

    sql: str
    """The winning statement, in the form the model actually wrote it.

    The normalised form is used for *counting* only. Re-rendering a query
    through a formatter before executing it would mean running something the
    model never produced, which makes a failure harder to trace back.
    """

    agreement: int
    """How many candidates agreed with the winner."""

    total: int
    """How many candidates were generated."""

    @property
    def unanimous(self) -> bool:
        return self.agreement == self.total

    @property
    def confidence(self) -> float:
        return self.agreement / self.total if self.total else 0.0


def canonical(sql: str, *, dialect: str) -> str:
    """A form in which equivalent queries compare equal.

    Parsed and re-rendered, so case, whitespace and alias formatting stop
    splitting a majority. A query that will not parse falls back to collapsed
    text — it should be able to lose a vote, not break the count.
    """
    try:
        return sqlglot.parse_one(sql, read=dialect).sql(dialect=dialect, normalize=True)
    except Exception:  # noqa: BLE001 - an unparseable candidate simply loses
        return _WHITESPACE.sub(" ", sql.strip()).casefold()


def tally(candidates: list[str], *, dialect: str) -> Vote | None:
    """Pick the statement the most candidates agree on.

    Ties are broken by order of generation, which favours the first sample.
    With no signal to separate two equally-supported queries, the earliest is
    the one produced at the lowest effective temperature drift, and picking
    deterministically keeps the same inputs producing the same output.
    """
    usable = [sql for sql in candidates if sql and sql.strip()]
    if not usable:
        return None

    counts = Counter(canonical(sql, dialect=dialect) for sql in usable)
    winner, agreement = counts.most_common(1)[0]

    for sql in usable:
        if canonical(sql, dialect=dialect) == winner:
            return Vote(sql=sql, agreement=agreement, total=len(usable))

    return None  # pragma: no cover - unreachable; the winner came from `usable`
