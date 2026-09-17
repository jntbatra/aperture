"""Pre-execution cost guard.

Asks the planner what a query will cost before running it. A query that plans
to scan forty million rows is rejected without ever touching the data -- the
read-only role stops writes, this stops accidental table scans.

One subtlety drives the design: on Postgres, `EXPLAIN` *raises* for an
undefined column, so a plain typo surfaces here rather than at execution. That
is a repairable error, not an expensive query, and the two need different
prompts and different retry budgets -- hence `kind`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from ..config import settings
from ..db.connection import Database, DbError, describe_error

CostKind = Literal["ok", "too_expensive", "invalid", "unsupported"]


@dataclass
class CostEstimate:
    kind: CostKind
    total_cost: float | None = None
    estimated_rows: float | None = None
    reason: str = ""
    error: DbError | None = None

    @property
    def ok(self) -> bool:
        return self.kind in {"ok", "unsupported"}

    @property
    def repairable(self) -> bool:
        """`invalid` means the SQL is wrong; the loop can fix that."""
        return self.kind == "invalid"


def estimate_cost(db: Database, sql: str) -> CostEstimate:
    cfg = settings()

    if db.dialect == "postgresql":
        try:
            with db.engine.connect() as conn:
                conn.execute(text("SET statement_timeout = 5000"))
                raw = conn.execute(text(f"EXPLAIN (FORMAT JSON) {sql}")).scalar()
        except SQLAlchemyError as err:
            error = describe_error(err)
            return CostEstimate(kind="invalid", reason=error.primary or str(err), error=error)

        plan = json.loads(raw) if isinstance(raw, str) else raw
        root = plan[0]["Plan"]
        total_cost = float(root.get("Total Cost", 0.0))
        rows = float(root.get("Plan Rows", 0.0))
        if total_cost > cfg.max_estimated_cost:
            return CostEstimate(
                kind="too_expensive",
                total_cost=total_cost,
                estimated_rows=rows,
                reason=(
                    f"planner cost {total_cost:,.0f} exceeds limit "
                    f"{cfg.max_estimated_cost:,.0f}; add a filter or aggregate"
                ),
            )
        return CostEstimate(kind="ok", total_cost=total_cost, estimated_rows=rows)

    if db.dialect == "mysql":
        try:
            with db.engine.connect() as conn:
                rows_out = conn.execute(text(f"EXPLAIN {sql}")).fetchall()
        except SQLAlchemyError as err:
            error = describe_error(err)
            return CostEstimate(kind="invalid", reason=error.primary or str(err), error=error)
        scanned = sum(float(r._mapping.get("rows") or 0) for r in rows_out)
        if scanned > cfg.max_estimated_cost:
            return CostEstimate(
                kind="too_expensive",
                estimated_rows=scanned,
                reason=f"estimated {scanned:,.0f} rows scanned exceeds limit",
            )
        return CostEstimate(kind="ok", estimated_rows=scanned)

    # SQLite's EXPLAIN QUERY PLAN has no cost model worth gating on.
    return CostEstimate(kind="unsupported", reason="no cost model for this dialect")
