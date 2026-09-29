"""Facts about a query that can be checked rather than judged.

Why this is separate from the intent check
------------------------------------------
Two of the three ways a query silently answers the wrong question are
*arithmetic*, not opinion:

* a filter matched nothing — the value in the ``WHERE`` clause does not occur
  in that column at all
* a filter was dropped — a constraint the question asked for is not in the SQL,
  so the result is the unfiltered population wearing a filtered label

Both were observed on real data. ``'craving deals'`` was a category nobody had
spelled that way (the real name is ``Cravings Deals ⭐``), and rather than
asking, the agent produced a query with no category join at all — returning the
entire customer base under a cohort's name.

Asking a model "does this look right?" about either of those is asking it to
guess at something a query can answer. So this module answers them first, and
the intent check is handed the facts.

Why the checks are conservative
-------------------------------
Everything here feeds a prompt, and a false alarm is worse than a miss: it
teaches the model to distrust a correct query, and the repair it produces is
usually worse than what it replaced. So a check that cannot be sure says
nothing.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import sqlglot
from sqlalchemy import text
from sqlglot import exp

logger = logging.getLogger(__name__)

MAX_LITERAL_CHECKS = 8
"""Literals to verify per query.

Each is a database round trip against a column that may be large. Eight covers
every real query seen; an unbounded loop over a generated ``IN`` list of two
hundred values would turn one question into two hundred queries.
"""

STOPWORDS = frozenset(
    """
    a an the of for from in on at by to and or not with without is are was were
    be been being how many much what which who whom whose when where why me my
    our us we you your show list give tell find get all every each per total
    number count sum average avg min max top bottom first last more than less
    over under between order sort group having select
    """.split()  # noqa: SIM905 - a block of words reads as a block of words
)
"""Words that carry no filtering intent.

Used only to decide whether a *phrase from the question* going missing from the
SQL is worth mentioning. Deliberately generous — the cost of a word being in
here is a missed hint, and the cost of one being absent is a false alarm on
every query.
"""


@dataclass(frozen=True, slots=True)
class Evidence:
    """What could be established about a query without asking a model."""

    empty: bool = False
    """The query returned no rows at all."""

    unmatched_literals: tuple[tuple[str, str], ...] = ()
    """``(column, value)`` pairs where the value occurs nowhere in that column.

    The strongest signal here. A filter comparing against a value that does not
    exist is not a narrow result, it is a wrong one — and it is a fact, checked
    against the data, not an impression.
    """

    unfiltered_count: int | None = None
    """Rows the same query would return with its WHERE clause removed.

    None when it could not be determined. Compared against the real count by
    the caller: equal counts mean the filter changed nothing, which is what a
    silently dropped constraint looks like from the outside.
    """

    dropped_terms: tuple[str, ...] = field(default_factory=tuple)
    """Distinctive phrases from the question that appear nowhere in the SQL.

    The weakest signal and the one most likely to be noise, so it is heavily
    filtered: quoted phrases and multi-word capitalised runs only, never bare
    nouns. It exists because it is the only thing that would have caught a
    category filter being dropped entirely.
    """

    def worth_mentioning(self, row_count: int) -> bool:
        """Whether anything here is worth putting in front of a model."""
        return bool(
            self.empty
            or self.unmatched_literals
            or self.dropped_terms
            or (self.unfiltered_count is not None and self.unfiltered_count == row_count)
        )

    def render(self, row_count: int) -> str:
        """The facts, as prompt text. Empty when there is nothing to say."""
        lines: list[str] = []

        if self.empty:
            lines.append("- The query returned NO rows.")

        for column, value in self.unmatched_literals:
            lines.append(
                f"- The filter compares {column} against {value!r}, and that "
                f"value does not occur anywhere in {column}. The filter matches "
                f"nothing."
            )

        if self.unfiltered_count is not None and self.unfiltered_count == row_count:
            lines.append(
                f"- Removing the WHERE clause returns the same {row_count} rows, "
                f"so the filters in this query excluded nothing."
            )

        if self.dropped_terms:
            joined = ", ".join(repr(t) for t in self.dropped_terms)
            lines.append(
                f"- The question mentions {joined}, which appears nowhere in the "
                f"SQL. Check whether a constraint was dropped."
            )

        return "\n".join(lines)


def gather(
    connection,
    *,
    question: str,
    sql: str,
    dialect: str,
    row_count: int,
) -> Evidence:
    """Establish what can be established. Never raises.

    Runs inside the caller's existing read-only transaction, so every probe is
    subject to the same role, timeout and read-only guarantees as the query it
    is about.
    """
    try:
        statement = sqlglot.parse_one(sql, read=dialect)
    except Exception as exc:  # noqa: BLE001 - advisory, never fatal
        logger.debug("evidence: could not parse: %s", exc)
        return Evidence(empty=row_count == 0)

    return Evidence(
        empty=row_count == 0,
        unmatched_literals=_unmatched_literals(connection, statement, dialect),
        unfiltered_count=_unfiltered_count(connection, statement, dialect),
        dropped_terms=_dropped_terms(question, sql),
    )


def _unmatched_literals(connection, statement, dialect: str) -> tuple[tuple[str, str], ...]:
    """Equality comparisons whose value does not occur in the column.

    Only ``column = 'literal'``. Not ``LIKE``, not ranges, not numbers: a
    number absent from a column is ordinary (nobody is 200 years old), and a
    ``LIKE`` pattern is not a value to look up. Strings compared for equality
    are where a misspelled category or status actually goes wrong.
    """
    findings: list[tuple[str, str]] = []
    aliases = {
        (t.alias or t.name): t.name for t in statement.find_all(exp.Table) if t.name
    }

    for comparison in statement.find_all(exp.EQ):
        column = comparison.find(exp.Column)
        literal = comparison.find(exp.Literal)
        if column is None or literal is None or not literal.is_string:
            continue
        if not column.name:
            continue

        table = aliases.get(column.table, column.table) or None
        if table is None:
            # Unqualified in a multi-table query: resolving it properly needs
            # more schema than is worth threading here, and guessing produces
            # warnings about the wrong column.
            if len(aliases) != 1:
                continue
            table = next(iter(aliases.values()))

        if len(findings) >= MAX_LITERAL_CHECKS:
            break

        value = literal.this
        try:
            quoted_table = _quote(connection, table)
            quoted_column = _quote(connection, column.name)
            found = connection.execute(
                text(
                    f"SELECT 1 FROM {quoted_table} WHERE {quoted_column} = :v LIMIT 1"
                ),
                {"v": value},
            ).fetchone()
        except Exception as exc:  # noqa: BLE001 - a probe that fails usually proves nothing
            # …with one exception that is stronger evidence than the query
            # itself. Comparing an enum column against a value outside the
            # enum raises rather than returning no rows:
            #
            #   invalid input value for enum "OrderStatus": "DELIVRED"
            #
            # That is the database stating the value cannot occur in that
            # column — a better answer than an empty result, which could also
            # mean the table is empty.
            if _means_value_cannot_exist(exc):
                findings.append((f"{table}.{column.name}", value))
            else:
                logger.debug("evidence: literal probe failed: %s", exc)
            continue

        if found is None:
            findings.append((f"{table}.{column.name}", value))

    return tuple(findings)


def _unfiltered_count(connection, statement, dialect: str) -> int | None:
    """What the same query would return with its WHERE clause removed.

    Only attempted for a simple single-select with a WHERE and no GROUP BY:
    stripping the predicate from anything more involved changes the meaning in
    ways that make the comparison meaningless rather than informative.
    """
    if not isinstance(statement, exp.Select):
        return None
    if statement.args.get("group") or statement.args.get("having"):
        return None

    # The docstring above has always said "simple single-select". Nothing
    # checked it, and on a join this probe is a cartesian product: the WHERE
    # is what was holding the tables together, so stripping it counts every
    # row against every other row.
    #
    # That is not theoretical. It ran for 56 minutes at 189% CPU on two
    # questions of a 500-question benchmark, and it is why the first
    # intent-check run took 3,760 seconds against 494 for the same questions
    # without it — a slowdown blamed on API throttling at the time.
    #
    # SQLite makes it unrecoverable rather than merely slow: only the
    # PostgreSQL dialect emits a statement timeout, so there is nothing to
    # stop it.
    if statement.args.get("joins"):
        return None
    if len({t.name for t in statement.find_all(exp.Table) if t.name}) > 1:
        return None

    # An un-grouped aggregate returns exactly one row whatever the WHERE says,
    # so "the same number of rows with and without the filter" is true of every
    # correct COUNT query ever written. Comparing them fired on
    # `SELECT count(*) FROM orders WHERE status = 'DELIVERED'` — a perfectly
    # good query — which is exactly the false alarm this module is supposed to
    # avoid: it teaches the model to distrust correct SQL.
    if any(
        projection.find(exp.AggFunc) is not None for projection in statement.expressions
    ):
        return None
    if statement.args.get("with") or statement.find(exp.Subquery):
        return None
    if statement.args.get("where") is None:
        return None

    stripped = statement.copy()
    stripped.set("where", None)
    stripped.set("limit", None)
    stripped.set("order", None)

    try:
        counted = exp.select(exp.func("COUNT", exp.Star())).from_(
            stripped.subquery(alias="unfiltered")
        )
        return connection.execute(text(counted.sql(dialect=dialect))).scalar()
    except Exception as exc:  # noqa: BLE001
        logger.debug("evidence: unfiltered count failed: %s", exc)
        return None


_QUOTED = re.compile(r"['\"“‘]([^'\"”’]{2,40})['\"”’]")
_CAPITALISED_RUN = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b")


def _dropped_terms(question: str, sql: str) -> tuple[str, ...]:
    """Distinctive phrases in the question that appear nowhere in the SQL.

    Two sources only, both deliberately narrow:

    * something the user put in quotes — they were naming a value
    * a run of two or more capitalised words — a proper noun

    A bare lowercase noun is not considered. "orders", "customers" and
    "revenue" appear in every question and their absence from the SQL text
    means nothing, so including them would fire on almost every query and the
    signal would be ignored within a day.
    """
    haystack = sql.lower()
    seen: list[str] = []

    for pattern in (_QUOTED, _CAPITALISED_RUN):
        for match in pattern.finditer(question):
            phrase = match.group(1).strip()
            words = [w for w in re.split(r"\W+", phrase.lower()) if w]
            if not words or all(w in STOPWORDS for w in words):
                continue
            # Present if any distinctive word of the phrase is in the SQL. A
            # category called "Cravings Deals" counts as present when the SQL
            # says "craving" — the point is whether the constraint is there at
            # all, not whether it is spelled identically.
            if any(w[:6] in haystack for w in words if w not in STOPWORDS and len(w) > 3):
                continue
            if phrase not in seen:
                seen.append(phrase)

    return tuple(seen[:3])


_CANNOT_EXIST = (
    "invalid input value for enum",
    "invalid input syntax for type",
)
"""Driver errors that mean the value is not representable in that column.

Matched on text because the alternative is importing every driver's exception
hierarchy into a module that must work across dialects. Narrow on purpose: a
timeout, a permission error or a missing column all mean the probe failed and
prove nothing about the value.
"""


def _means_value_cannot_exist(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(fragment in message for fragment in _CANNOT_EXIST)


def _quote(connection, identifier: str) -> str:
    return connection.dialect.identifier_preparer.quote(identifier)
