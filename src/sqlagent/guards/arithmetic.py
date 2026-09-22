"""Catch aggregates inflated by a join, which no error ever reports.

The failure
-----------
    SELECT c.name, SUM(o.total)
    FROM customers c
    JOIN orders o   ON o.customer_id = c.id
    JOIN order_items i ON i.order_id = o.id
    GROUP BY c.name

Every order's total is counted **once per line item**. A customer who bought
three things has their order total tripled. The SQL is valid, the join
conditions are real foreign keys, the query runs, and the number is wrong.

This is the worst category of bug this system can produce, because there is no
error to catch, no exception to route, and the answer looks entirely reasonable.
The validator cannot see it — the statement is a perfectly legal SELECT — and
execution cannot see it either.

How it is detected
------------------
Statically, from the parsed query. Two conditions together:

1. The query aggregates a column with SUM or AVG — the functions that *multiply*
   under duplication. COUNT is excluded: ``COUNT(*)`` over a fan-out is usually
   what was meant ("how many line items"), and ``COUNT(DISTINCT x)`` is immune.
2. Either the aggregated column belongs to a table on the **one** side of a
   one-to-many join whose many side is also present, **or** some table in the
   query is joined to itself.

The first needs the schema: "is this table duplicated by that join?" is
answerable only if you know which side holds the foreign key, and the schema
graph knows.

The second needs nothing but the query text, and it was added after a miss.
A retention query joined ``orders`` to ``orders`` — to find each customer's
next order — and then summed line-item values across the result. Each order was
repeated once per later order by the same customer, and an average order value
of ₹271 was reported as ₹1,076. Nothing in the foreign-key graph describes that
duplication, because the relationship is a table to itself.

Why a warning and not a rejection
---------------------------------
Because the pattern is sometimes correct. ``SUM(o.total)`` with a join to
``order_items`` is wrong; the same shape with ``SUM(i.price)`` is right, and a
query may legitimately want the duplication. Rejecting outright would block
correct queries, so the finding is attached to the result and surfaced — the
user is told the figure may be inflated, with the reason, rather than being
handed a silently wrong number or being refused an answer.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

logger = logging.getLogger(__name__)

INFLATING_AGGREGATES = (exp.Sum, exp.Avg)
"""Aggregates that multiply when rows are duplicated.

COUNT is excluded on purpose: ``COUNT(*)`` across a fan-out is normally the
intended question, and COUNT(DISTINCT ...) cannot be inflated at all.
MIN and MAX are idempotent under duplication.
"""


@dataclass(frozen=True, slots=True)
class Inflation:
    """One aggregate that may be multiplied by a join in the same query."""

    aggregate: str
    """The rendered aggregate, e.g. ``SUM(o.total)``."""

    table: str
    """The table whose rows are duplicated."""

    multiplied_by: str
    """The table doing the duplicating — the many side, or the self-joined one."""

    self_join: bool = False
    """Whether the duplication comes from a table joined to itself."""

    reaggregated: bool = False
    """Whether this sums a value that was already an aggregate."""

    def render(self) -> str:
        if self.reaggregated:
            return (
                f"{self.aggregate} re-aggregates {self.multiplied_by}, which is "
                f"already a COUNT or SUM - the total counts each underlying row "
                f"once per group it appeared in"
            )
        if self.self_join:
            return (
                f"{self.aggregate} may be inflated: {self.multiplied_by} is "
                f"joined to itself, so its rows repeat"
            )
        return (
            f"{self.aggregate} may be inflated: each {self.table} row is "
            f"repeated once per matching {self.multiplied_by} row"
        )


def find_inflated_aggregates(
    sql: str, *, dialect: str, one_to_many: dict[str, set[str]]
) -> list[Inflation]:
    """Report SUM/AVG aggregates that a join in the same query may multiply.

    Args:
        sql: The statement to inspect. Already validated by the time this runs.
        dialect: sqlglot dialect name.
        one_to_many: ``parent table -> {child tables}``, derived from foreign
            keys. A child holds the foreign key, so many child rows point at one
            parent row — which is exactly the duplication being looked for.

    Returns:
        A finding per suspicious aggregate, empty when nothing looks wrong.
        Never raises: a parse failure means no finding, because a guard that
        can break a working query is worse than a guard that misses one.
    """
    try:
        statement = sqlglot.parse_one(sql, read=dialect)
    except Exception as exc:  # noqa: BLE001 - advisory check, never fatal
        logger.debug("could not parse for inflation check: %s", exc)
        return []

    # alias -> real table name, so `SUM(o.total)` can be traced to `orders`.
    aliases: dict[str, str] = {}
    present: set[str] = set()
    for table in statement.find_all(exp.Table):
        name = table.name
        present.add(name)
        aliases[(table.alias or name)] = name

    # A table joined to itself duplicates its own rows, and therefore every row
    # joined to it. No foreign key describes this, so it has to be spotted from
    # the query text.
    #
    # Counted only across the *outer* FROM and JOINs. A table named again inside
    # a subquery — `WHERE id IN (SELECT order_id FROM order_items)` — produces no
    # duplication at all, and counting every mention flagged that as a self-join.
    #
    # CTE names are excluded. sqlglot represents a CTE *reference* as a Table,
    # so a CTE used in two places looked exactly like a self-join and was
    # reported as one — a confusing message about a table that does not exist.
    cte_names = _cte_names(statement)
    appearances: Counter[str] = Counter()
    for table in statement.find_all(exp.Table):
        if _inside_subquery(table) or table.name in cte_names:
            continue
        appearances[table.name] += 1

    self_joined = {name for name, count in appearances.items() if count > 1}

    # CTE output columns that are themselves aggregates. Summing one of these
    # double counts: `COUNT(DISTINCT o.id)` grouped per (kitchen, item), then
    # SUMmed per kitchen, counts an order once per item it contained.
    #
    # Observed: 269 cancelled orders reported as 393, a 46% overstatement, with
    # no error and no fan-out the table-level check could see — the aggregate
    # reads a CTE column, and a CTE is not in the foreign-key graph.
    aggregated_cte_columns = _aggregate_cte_columns(statement)

    findings: list[Inflation] = []
    seen: set[tuple[str, str]] = set()

    for aggregate in statement.find_all(*INFLATING_AGGREGATES):
        column = aggregate.find(exp.Column)
        if column is None:
            continue

        # An unqualified column in a multi-table query cannot be attributed to a
        # table without resolving it properly, which needs more schema than is
        # worth threading here. Skipped rather than guessed.
        qualifier = column.table
        if not qualifier:
            continue

        owner = aliases.get(qualifier, qualifier)

        # Re-aggregation. Checked on the column name, because a CTE referenced
        # through an alias (`co.order_count`) has already lost the CTE's name by
        # the time the column is reached.
        if column.name in aggregated_cte_columns:
            key = (aggregate.sql(dialect=dialect), f"reagg:{column.name}")
            if key not in seen:
                seen.add(key)
                findings.append(
                    Inflation(
                        aggregate=aggregate.sql(dialect=dialect),
                        table=owner,
                        multiplied_by=column.name,
                        reaggregated=True,
                    )
                )

        # The self-join case. Reported against whichever table is duplicated,
        # including when the aggregate reads a *different* table — summing
        # order_items across a self-joined orders inflates just as surely.
        for duplicated in sorted(self_joined):
            key = (aggregate.sql(dialect=dialect), f"self:{duplicated}")
            if key in seen:
                continue
            seen.add(key)
            findings.append(
                Inflation(
                    aggregate=aggregate.sql(dialect=dialect),
                    table=owner,
                    multiplied_by=duplicated,
                    self_join=True,
                )
            )

        for child in one_to_many.get(owner, set()):
            if child not in present or child == owner:
                continue
            key = (aggregate.sql(dialect=dialect), child)
            if key in seen:
                continue
            seen.add(key)
            findings.append(
                Inflation(
                    aggregate=aggregate.sql(dialect=dialect),
                    table=owner,
                    multiplied_by=child,
                )
            )

    return findings


def _cte_names(statement: exp.Expression) -> set[str]:
    """Names defined by a WITH clause, which are not base tables."""
    with_clause = statement.find(exp.With)
    if with_clause is None:
        return set()
    return {cte.alias for cte in with_clause.expressions if cte.alias}


def _aggregate_cte_columns(statement: exp.Expression) -> set[str]:
    """Names of CTE output columns that are themselves aggregates.

    ``WITH c AS (SELECT ..., COUNT(DISTINCT o.id) AS order_count ...)`` puts
    ``order_count`` in this set. A later ``SUM(c.order_count)`` is then summing
    a count, which is the shape that silently double counts.
    """
    names: set[str] = set()

    # `find`, not `args["with"]`: sqlglot does not always hang the WITH clause
    # off the select's args, and reading it from there silently returned nothing.
    with_clause = statement.find(exp.With)
    if with_clause is None:
        return names

    for cte in with_clause.expressions:
        inner = cte.this
        if not isinstance(inner, exp.Select):
            continue
        for projection in inner.expressions:
            # Only aliased projections can be referenced by name later.
            if not isinstance(projection, exp.Alias):
                continue
            if projection.this.find(exp.Count, exp.Sum, exp.Avg) is not None:
                names.add(projection.alias)

    return names


def _inside_subquery(node: exp.Expression) -> bool:
    """Whether this table reference sits inside a nested SELECT.

    A table named again in a subquery — ``WHERE id IN (SELECT order_id FROM
    order_items)`` — duplicates nothing in the outer result, so counting every
    mention reported an ordinary query as a self-join.
    """
    parent = node.parent
    while parent is not None:
        if isinstance(parent, exp.Subquery | exp.Exists | exp.In):
            return True
        parent = parent.parent
    return False


def one_to_many_map(snapshot) -> dict[str, set[str]]:
    """Build ``parent -> {children}`` from the schema's foreign keys.

    The child declares the constraint, so ``orders.customer_id -> customers.id``
    means many orders per customer: ``customers`` is the parent, ``orders`` the
    child that duplicates it.
    """
    mapping: dict[str, set[str]] = {}
    for table in snapshot.tables.values():
        for fk in table.foreign_keys:
            mapping.setdefault(fk.target_table, set()).add(fk.source_table)
    return mapping
