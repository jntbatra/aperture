"""Post-execution verification.

A query can be syntactically valid, execute cleanly, return a plausible number,
and still be wrong. The two ways that happens most often on a real schema are
both arithmetic, so both can be checked by arithmetic rather than by asking a
model for a second opinion:

* **Join fan-out.** Joining a child table multiplies parent rows, so any SUM or
  AVG over parent columns is inflated. No error is raised; the number is simply
  wrong.
* **Dropped groups.** A GROUP BY over a nullable column silently omits rows
  whose key is NULL, so the parts no longer add up to the whole.

Each check re-runs a cheap COUNT-shaped probe derived from the query's own AST
and compares two numbers. Findings are reported, never used to rewrite the
query: the SQL is shown to the user, and a caveat they can see beats a silent
correction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

from .db.connection import Database, QueryFailed
from .db.introspect import SchemaSnapshot

log = logging.getLogger(__name__)

AGGREGATES = (exp.Sum, exp.Avg, exp.Count, exp.Min, exp.Max)


@dataclass
class Finding:
    kind: str
    message: str
    severity: str = "warning"


def _base_table(tree: exp.Expr) -> tuple[str, str] | None:
    """Return (table name, how to reference it) for the FROM clause.

    The reference matters: a probe that says "orders"."id" against a query
    written as `FROM orders o` fails, and the check disappears silently.
    """
    # sqlglot 30 renamed this arg from "from" to "from_"; accept both so the
    # check does not quietly vanish on a version bump.
    from_clause = tree.args.get("from_") or tree.args.get("from")
    if not from_clause:
        return None
    table = from_clause.this if isinstance(from_clause, exp.From) else None
    if not isinstance(table, exp.Table):
        return None
    return table.name, (table.alias or table.name)


def _primary_key(snapshot: SchemaSnapshot, table: str) -> str | None:
    info = snapshot.tables.get(table)
    if not info:
        return None
    keys = [c.name for c in info.columns if c.is_pk]
    return keys[0] if len(keys) == 1 else None


def _strip_to_count(tree: exp.Expr, count_expression: str) -> str | None:
    """Rebuild the query as a single COUNT over the same FROM/JOIN/WHERE."""
    if not isinstance(tree, exp.Select):
        return None
    probe = tree.copy()
    for key in ("group", "order", "limit", "offset", "having", "qualify", "distinct", "with"):
        probe.set(key, None)
    probe.set("expressions", [sqlglot.parse_one(count_expression)])
    return probe.sql()


def _has_aggregate(tree: exp.Expr) -> bool:
    return any(True for _ in tree.find_all(*AGGREGATES))


def check_fan_out(
    db: Database, tree: exp.Expr, snapshot: SchemaSnapshot
) -> Finding | None:
    """Did the joins multiply the rows the aggregate is computed over?"""
    if not list(tree.find_all(exp.Join)) or not _has_aggregate(tree):
        return None

    resolved = _base_table(tree)
    if not resolved:
        return None
    base, reference = resolved
    key = _primary_key(snapshot, base)
    if not key:
        return None

    joined_sql = _strip_to_count(tree, "COUNT(*)")
    distinct_sql = _strip_to_count(tree, f'COUNT(DISTINCT "{reference}"."{key}")')
    if not joined_sql or not distinct_sql:
        return None

    try:
        joined = db.scalar(joined_sql)
        distinct = db.scalar(distinct_sql)
    except (QueryFailed, Exception) as err:  # a probe must never break an answer
        log.debug("fan-out probe failed: %s", err)
        return None

    if not joined or not distinct or distinct >= joined:
        return None

    ratio = joined / distinct
    if ratio < 1.05:
        return None
    return Finding(
        kind="fan_out",
        message=(
            f"The joins multiply {base} rows {ratio:.1f}x "
            f"({distinct:,} distinct {base} rows produce {joined:,} joined rows). "
            f"Any SUM or AVG over {base} columns is inflated by roughly that factor; "
            f"aggregate the joined table in a subquery, or use COUNT(DISTINCT ...)."
        ),
    )


def check_group_coverage(db: Database, tree: exp.Expr) -> Finding | None:
    """Do the groups account for every row, or did NULL keys drop some?"""
    group = tree.args.get("group") if isinstance(tree, exp.Select) else None
    if not group:
        return None

    keys = [e for e in group.expressions if isinstance(e, exp.Column)]
    if not keys:
        return None

    total_sql = _strip_to_count(tree, "COUNT(*)")
    if not total_sql:
        return None

    conditions = " OR ".join(f"{key.sql()} IS NULL" for key in keys)
    probe = sqlglot.parse_one(total_sql)
    existing = probe.args.get("where")
    combined = f"({conditions})"
    if existing:
        combined = f"{existing.this.sql()} AND {combined}"
    probe.set("where", exp.Where(this=sqlglot.parse_one(combined)))

    try:
        total = db.scalar(total_sql)
        dropped = db.scalar(probe.sql())
    except Exception as err:
        log.debug("group coverage probe failed: %s", err)
        return None

    if not total or not dropped:
        return None

    share = dropped / total
    return Finding(
        kind="dropped_groups",
        message=(
            f"{dropped:,} of {total:,} rows ({share:.0%}) have a NULL grouping key and are "
            "absent from these groups, so the breakdown does not sum to the overall total."
        ),
    )


def verify(db: Database, sql: str, snapshot: SchemaSnapshot, *, dialect: str) -> list[Finding]:
    """Run every applicable check against an executed query."""
    try:
        tree = sqlglot.parse_one(sql, dialect=dialect)
    except Exception:
        return []

    findings = []
    for check in (
        lambda: check_fan_out(db, tree, snapshot),
        lambda: check_group_coverage(db, tree),
    ):
        try:
            finding = check()
        except Exception as err:
            log.debug("verification check failed: %s", err)
            continue
        if finding:
            findings.append(finding)
    return findings
