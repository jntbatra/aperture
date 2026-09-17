"""Token + spend ledger with a hard ceiling.

Every model call routes through `Ledger.record`. When the running estimate
crosses the configured ceiling the ledger raises `BudgetExceeded`, which
aborts batch runs (the benchmark, mainly) instead of quietly draining an
account overnight.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import settings


class BudgetExceeded(RuntimeError):
    """Raised when the running spend estimate crosses the configured ceiling."""


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0

    def cost_usd(self, price_in: float, price_out: float) -> float:
        return (
            self.input_tokens / 1_000_000 * price_in
            + self.output_tokens / 1_000_000 * price_out
        )


@dataclass
class Ledger:
    """Process-wide usage ledger, persisted as JSONL for later analysis."""

    path: Path
    total: Usage = field(default_factory=Usage)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def default(cls) -> "Ledger":
        home = Path(os.path.expanduser(settings().home_dir))
        home.mkdir(parents=True, exist_ok=True)
        return cls(path=home / "usage.jsonl")

    def record(
        self,
        input_tokens: int,
        output_tokens: int,
        *,
        label: str = "",
        model_id: str = "",
    ) -> Usage:
        cfg = settings()
        with self._lock:
            self.total.input_tokens += input_tokens
            self.total.output_tokens += output_tokens
            self.total.calls += 1
            spent = self.total.cost_usd(cfg.price_in_per_mtok, cfg.price_out_per_mtok)
            row = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "label": label,
                "model_id": model_id,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cumulative_usd": round(spent, 6),
            }
            with self.path.open("a") as fh:
                fh.write(json.dumps(row) + "\n")

        if spent >= cfg.budget_ceiling_usd:
            raise BudgetExceeded(
                f"spend estimate ${spent:.2f} crossed ceiling "
                f"${cfg.budget_ceiling_usd:.2f} after {self.total.calls} calls"
            )
        return self.total

    def spent_usd(self) -> float:
        cfg = settings()
        return self.total.cost_usd(cfg.price_in_per_mtok, cfg.price_out_per_mtok)

    def summary(self) -> str:
        return (
            f"{self.total.calls} calls · {self.total.input_tokens:,} in / "
            f"{self.total.output_tokens:,} out · ~${self.spent_usd():.2f}"
        )


LEDGER = Ledger.default()
