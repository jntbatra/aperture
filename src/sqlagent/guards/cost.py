"""Ask the planner what a query will cost before running it.

Why a gate in front of a statement timeout
------------------------------------------
There is already a statement timeout and a row cap, and both are real
protection. They are also *reactive*: the damage — a sequential scan over
millions of rows, a cross join the model did not intend — happens first, and the
timeout stops it partway through. On a production replica that is thirty seconds
of load caused by a question someone typed in a text box.

``EXPLAIN`` asks the database what it intends to do without doing it. It costs
one cheap round trip and catches the pathological cases up front.

What this is not
----------------
Not a correctness check. A cheap query can be wrong and an expensive one right.
This only refuses queries whose *plan* is extreme, and the thresholds are set
where a false positive is unlikely: the default cost ceiling is far above a
normal analytical query over a few million rows.

It is also not a security boundary. The validator is. A query that reaches this
point has already been proved read-only; this is about protecting the database
from load, not from writes.

Why estimates, honestly
-----------------------
Planner estimates are frequently wrong, sometimes by orders of magnitude, and
they are wrong in *both* directions. That is exactly why the statement timeout
stays: the gate catches the obvious disasters early and cheaply, the timeout
catches everything the planner misjudged. Neither replaces the other.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import Engine, text

from sqlagent.db.dialects import Dialect

logger = logging.getLogger(__name__)


class CostRejected(Exception):
    """The planner's estimate exceeded the configured ceiling."""

    def __init__(self, message: str, *, kind: str = "cost_too_high") -> None:
        super().__init__(message)
        self.kind = kind
        """Routes like any other failure kind — see ``agent_graph.CONTEXT_FAILURES``.

        A cost rejection is a Loop A failure: the schema was fine, the query was
        not. Regenerating with the estimate attached lets the model add the
        filter or limit it forgot.
        """


# PostgreSQL: "Seq Scan on orders  (cost=0.00..123456.78 rows=999 width=40)".
# Only the first line matters — it is the total for the whole plan.
_PG_COST = re.compile(r"cost=[\d.]+\.\.([\d.]+)")
_PG_ROWS = re.compile(r"\brows=(\d+)")


def estimate(
    engine: Engine, sql: str, *, dialect: Dialect
) -> tuple[float | None, int | None]:
    """Return ``(total_cost, estimated_rows)``, either of which may be None.

    Never raises. A database that cannot explain a statement — an unsupported
    dialect, a permission quirk — must not turn a runnable query into a failed
    one. Unknown cost means the gate abstains and the timeout does the work.
    """
    if not dialect.supports_explain:
        return None, None

    try:
        with engine.connect() as connection:
            rows = connection.execute(text(f"EXPLAIN {sql}")).fetchall()
    except Exception as exc:  # noqa: BLE001 - explain is best-effort by design
        logger.debug("could not EXPLAIN: %s", exc)
        return None, None

    if not rows:
        return None, None

    first = " ".join(str(value) for value in rows[0])
    cost_match = _PG_COST.search(first)
    rows_match = _PG_ROWS.search(first)

    return (
        float(cost_match.group(1)) if cost_match else None,
        int(rows_match.group(1)) if rows_match else None,
    )


def check(
    engine: Engine,
    sql: str,
    *,
    dialect: Dialect,
    max_cost: float,
    max_estimated_rows: int,
) -> None:
    """Raise :class:`CostRejected` if the plan is extreme. Otherwise return.

    The message names the estimate and the ceiling, because it goes straight
    back into a repair prompt. "Too expensive" tells the model nothing; "the
    planner estimates 40 million rows, the limit is 5 million" tells it to add a
    filter.
    """
    if max_cost <= 0 and max_estimated_rows <= 0:
        return

    cost, estimated_rows = estimate(engine, sql, dialect=dialect)

    if max_cost > 0 and cost is not None and cost > max_cost:
        raise CostRejected(
            f"The query planner estimates a cost of {cost:,.0f}, above the "
            f"limit of {max_cost:,.0f}. Add a filter, an aggregate or a smaller "
            f"date range."
        )

    if (
        max_estimated_rows > 0
        and estimated_rows is not None
        and estimated_rows > max_estimated_rows
    ):
        raise CostRejected(
            f"The query planner estimates {estimated_rows:,} rows, above the "
            f"limit of {max_estimated_rows:,}. Aggregate or filter instead of "
            f"scanning everything."
        )
