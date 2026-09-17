"""Pre-execution cost guard.

Asks the planner what a query will cost before running it. A query that plans
to scan 40 million rows is rejected without ever touching the data -- the
read-only role stops writes, this stops accidental table scans.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import text

from ..config import settings
from ..db.connection import Database


@dataclass
class CostEstimate:
    supported: bool
    ok: bool
    total_cost: float | None = None
    estimated_rows: float | None = None
    reason: str = ""


def estimate_cost(db: Database, sql: str) -> CostEstimate:
    cfg = settings()

    if db.dialect == "postgresql":
        try:
            with db.engine.connect() as conn:
                conn.execute(text("SET statement_timeout = 5000"))
                raw = conn.execute(text(f"EXPLAIN (FORMAT JSON) {sql}")).scalar()
        except Exception as err:  # planner failures are a real signal, surface them
            return CostEstimate(supported=True, ok=False, reason=f"EXPLAIN failed: {err}")

        plan = json.loads(raw) if isinstance(raw, str) else raw
        root = plan[0]["Plan"]
        total_cost = float(root.get("Total Cost", 0.0))
        rows = float(root.get("Plan Rows", 0.0))
        if total_cost > cfg.max_estimated_cost:
            return CostEstimate(
                supported=True,
                ok=False,
                total_cost=total_cost,
                estimated_rows=rows,
                reason=(
                    f"planner cost {total_cost:,.0f} exceeds limit "
                    f"{cfg.max_estimated_cost:,.0f}; narrow the query"
                ),
            )
        return CostEstimate(supported=True, ok=True, total_cost=total_cost, estimated_rows=rows)

    if db.dialect == "mysql":
        try:
            with db.engine.connect() as conn:
                rows = conn.execute(text(f"EXPLAIN {sql}")).fetchall()
        except Exception as err:
            return CostEstimate(supported=True, ok=False, reason=f"EXPLAIN failed: {err}")
        scanned = sum(float(r._mapping.get("rows") or 0) for r in rows)
        if scanned > cfg.max_estimated_cost:
            return CostEstimate(
                supported=True,
                ok=False,
                estimated_rows=scanned,
                reason=f"estimated {scanned:,.0f} rows scanned exceeds limit",
            )
        return CostEstimate(supported=True, ok=True, estimated_rows=scanned)

    # SQLite's EXPLAIN QUERY PLAN has no cost model worth gating on.
    return CostEstimate(supported=False, ok=True, reason="no cost model for this dialect")
