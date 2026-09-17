"""BIRD Mini-Dev harness.

Execution accuracy, not string match: two different queries can both be right,
so the gold SQL and the predicted SQL are both run and their result sets
compared. Order is ignored unless the gold query sorts.

The SQLite split is used deliberately. It needs no data loading, and running
the benchmark through the same Database/validator path as the Postgres demo is
what makes the multi-dialect claim real rather than aspirational.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
import zipfile
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

HERE = Path(__file__).parent
DATA_DIR = HERE / "mini_dev_data"
DB_DIR = DATA_DIR / "dev_databases"
DOWNLOAD = HERE / "downloads" / "minidev.zip"
QUESTIONS_REPO = "birdsql/bird_mini_dev"
QUESTIONS_FILE = "data/mini_dev_sqlite-00000-of-00001.json"


@dataclass
class Case:
    question_id: int
    db_id: str
    question: str
    gold_sql: str
    evidence: str = ""


@dataclass
class Outcome:
    question_id: int
    db_id: str
    question: str
    predicted_sql: str = ""
    correct: bool = False
    status: str = ""
    attempts: int = 0
    error: str = ""
    seconds: float = 0.0
    gold_rows: int = 0
    predicted_rows: int = 0


@dataclass
class Report:
    outcomes: list[Outcome] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def correct(self) -> int:
        return sum(1 for o in self.outcomes if o.correct)

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0

    def summary(self) -> str:
        by_status = Counter(o.status for o in self.outcomes)
        lines = [
            f"execution accuracy: {self.correct}/{self.total} = {self.accuracy:.1%}",
            "statuses: " + ", ".join(f"{k}={v}" for k, v in sorted(by_status.items())),
        ]
        if self.outcomes:
            lines.append(
                f"median latency: {sorted(o.seconds for o in self.outcomes)[self.total // 2]:.1f}s"
            )
        return "\n".join(lines)


def ensure_data() -> None:
    """Unpack the databases and fetch the question file if needed."""
    if not DB_DIR.exists():
        if not DOWNLOAD.exists():
            raise SystemExit(
                f"missing {DOWNLOAD}. Download it first:\n"
                f"  curl -L -o {DOWNLOAD} https://bird-bench.oss-cn-beijing.aliyuncs.com/minidev.zip"
            )
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(DOWNLOAD) as archive:
            archive.extractall(HERE)

    if not questions_path().exists():
        from huggingface_hub import hf_hub_download

        downloaded = hf_hub_download(
            repo_id=QUESTIONS_REPO, filename=QUESTIONS_FILE, repo_type="dataset"
        )
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        questions_path().write_text(Path(downloaded).read_text())


def questions_path() -> Path:
    return DATA_DIR / "mini_dev_sqlite.json"


def find_database(db_id: str) -> Path | None:
    for candidate in DATA_DIR.rglob(f"{db_id}.sqlite"):
        return candidate
    return None


def load_cases(limit: int | None = None, db_filter: str = "") -> list[Case]:
    raw = json.loads(questions_path().read_text())
    cases = []
    for item in raw:
        if db_filter and item.get("db_id") != db_filter:
            continue
        cases.append(
            Case(
                question_id=int(item.get("question_id", len(cases))),
                db_id=item["db_id"],
                question=item["question"],
                gold_sql=item.get("SQL") or item.get("gold_sql") or "",
                evidence=item.get("evidence", ""),
            )
        )
    return cases[:limit] if limit else cases


def run_sqlite(path: Path, sql: str) -> list[tuple]:
    connection = sqlite3.connect(str(path))
    try:
        connection.text_factory = lambda b: b.decode("utf-8", "replace")
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def results_match(gold: list[tuple], predicted: list[tuple]) -> bool:
    """Compare result sets the way BIRD does: as multisets of rows."""
    if len(gold) != len(predicted):
        return False
    normalise = lambda rows: Counter(tuple(str(v) for v in row) for row in rows)
    return normalise(gold) == normalise(predicted)


def evaluate(
    limit: int | None = None,
    db_filter: str = "",
    projected_ceiling: float = 6.0,
    out_path: Path | None = None,
) -> Report:
    from aperture.budget import LEDGER
    from aperture.config import settings
    from aperture.db import Database
    from aperture.graph import build_analyst
    from aperture.graph.nodes import AnalystContext

    ensure_data()
    cases = load_cases(limit, db_filter)
    report = Report()
    graphs: dict[str, tuple] = {}
    spend_at_start = LEDGER.spent_usd()

    for index, case in enumerate(cases, start=1):
        database_path = find_database(case.db_id)
        if database_path is None:
            report.outcomes.append(
                Outcome(case.question_id, case.db_id, case.question, status="missing_db")
            )
            continue

        if case.db_id not in graphs:
            db = Database(f"sqlite:///{database_path}")
            ctx = AnalystContext.create(db)
            graphs[case.db_id] = build_analyst(ctx)
        graph, _ = graphs[case.db_id]

        started = time.perf_counter()
        question = case.question + (f" (hint: {case.evidence})" if case.evidence else "")
        try:
            state = graph.invoke(
                {"question": question},
                {"configurable": {"thread_id": f"bird-{case.question_id}"}},
            )
        except Exception as err:  # keep going; one bad case must not end the run
            report.outcomes.append(
                Outcome(
                    case.question_id,
                    case.db_id,
                    case.question,
                    status="crashed",
                    error=str(err)[:200],
                    seconds=time.perf_counter() - started,
                )
            )
            continue

        outcome = Outcome(
            question_id=case.question_id,
            db_id=case.db_id,
            question=case.question,
            predicted_sql=state.get("sql", ""),
            status=state.get("status", "unknown"),
            attempts=state.get("attempts", 0),
            seconds=time.perf_counter() - started,
        )

        if outcome.predicted_sql:
            try:
                gold_rows = run_sqlite(database_path, case.gold_sql)
                predicted_rows = run_sqlite(database_path, outcome.predicted_sql)
                outcome.gold_rows = len(gold_rows)
                outcome.predicted_rows = len(predicted_rows)
                outcome.correct = results_match(gold_rows, predicted_rows)
            except sqlite3.Error as err:
                outcome.error = str(err)[:200]

        report.outcomes.append(outcome)

        marker = "OK " if outcome.correct else "x  "
        print(
            f"{marker}[{index}/{len(cases)}] {case.db_id} q{case.question_id} "
            f"{outcome.status} {outcome.seconds:.1f}s",
            flush=True,
        )

        # Cost protocol: extrapolate from what has actually been spent and stop
        # before the full run if the projection is unaffordable.
        if index == 20 and len(cases) > 20:
            spent = LEDGER.spent_usd() - spend_at_start
            projected = spent / index * len(cases)
            print(
                f"\n[cost] {index} questions cost ${spent:.2f}; "
                f"{len(cases)} projects to ${projected:.2f} "
                f"(ceiling ${settings().budget_ceiling_usd:.2f})\n",
                flush=True,
            )
            if projected > projected_ceiling:
                print(f"[cost] projection exceeds ${projected_ceiling:.2f}; stopping early")
                break

    if out_path:
        out_path.write_text(
            json.dumps(
                {
                    "accuracy": report.accuracy,
                    "correct": report.correct,
                    "total": report.total,
                    "model": __import__("aperture.config", fromlist=["settings"]).settings().bedrock_model_id,
                    "outcomes": [asdict(o) for o in report.outcomes],
                },
                indent=2,
            )
        )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the BIRD Mini-Dev benchmark.")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--db", default="", help="Restrict to one database id.")
    parser.add_argument("--max-projected-usd", type=float, default=6.0)
    parser.add_argument("--out", default=str(HERE / "results.json"))
    args = parser.parse_args()

    report = evaluate(
        limit=args.limit or None,
        db_filter=args.db,
        projected_ceiling=args.max_projected_usd,
        out_path=Path(args.out),
    )
    print("\n" + report.summary())


if __name__ == "__main__":
    main()
