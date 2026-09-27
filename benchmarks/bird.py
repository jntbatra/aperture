"""Run the agent against the BIRD mini-dev benchmark.

What BIRD is
------------
BIRD (BIg Bench for LaRge-scale Database grounded text-to-SQL evaluation) is a
standard text-to-SQL benchmark. The "mini-dev" split is 500 questions across 11
real databases — football results, card games, school statistics, medical
records — each with a human-written gold SQL query.

How correctness is measured
---------------------------
**Execution accuracy**: run the agent's SQL and the gold SQL, and compare the
result sets. Not string comparison — there are many correct ways to write the
same query, and comparing text would fail almost all of them.

Row *order* is ignored unless the gold query has an ``ORDER BY``, and values are
normalised (floats rounded, ``Decimal`` coerced) because ``1`` and ``1.0`` are
the same answer.

Usage
-----
    python benchmarks/bird.py --limit 50
    python benchmarks/bird.py --model deepseek.v3.2 --limit 100 --workers 6
    python benchmarks/bird.py --full --out results/full.json
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, text

# Allow running as a script from the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlagent.config import Settings  # noqa: E402
from sqlagent.db.dialects import read_only_url  # noqa: E402
from sqlagent.judge import Verdict, judge_answer, summarise_verdicts  # noqa: E402
from sqlagent.llm.mantle import MantleClient  # noqa: E402
from sqlagent.pipeline import SqlAgent  # noqa: E402
from sqlagent.saas.plans import TIER_ORDER as TIERS  # noqa: E402

# The dataset is large (4 GB of SQLite files) and is not vendored into this
# repository. Point --data at wherever it is unpacked.
DEFAULT_DATA_ROOT = Path(
    "/home/jntbatra/Projects/aperture/benchmarks/minidev/MINIDEV"
)


@dataclass
class Outcome:
    """One question's result."""

    question_id: int
    db_id: str
    question: str
    difficulty: str
    correct: bool
    predicted_sql: str | None
    gold_sql: str
    error: str | None
    repairs: int
    model_calls: int
    input_tokens: int
    output_tokens: int
    seconds: float

    answer: str = ""
    """The prose written from the rows. Recorded so a run can be re-judged
    later without re-running it — judging is cheap, answering is not."""

    intent_rejections: tuple[str, ...] = ()
    """Defects the intent check named after seeing the rows, in order."""

    intent_asked: str = ""
    """The question it put back to the user, when it asked one.

    Recorded because an ``ask`` scores as a *failure* here — a harness has
    nobody to ask — so the accuracy number understates the behaviour by
    exactly these. Read them before reading the score."""

    judged: bool = False
    judge_ok: bool = False
    judge_defects: tuple[str, ...] = ()
    judge_problem: str = ""
    """The judge's verdict, when --judge was passed. ``judged`` is False both
    when the judge was off and when it failed, and the two are distinguished in
    the report: an ungraded answer is not a bad one."""


def load_questions(data_root: Path, *, postgres: bool = False) -> list[dict]:
    path = data_root / ("mini_dev_postgresql.json" if postgres else "mini_dev_sqlite.json")
    if not path.exists():
        raise SystemExit(
            f"Benchmark data not found at {path}.\n"
            "Download BIRD mini-dev and pass --data <path to MINIDEV>."
        )
    return json.loads(path.read_text())


def database_url(data_root: Path, db_id: str) -> str:
    """Read-only SQLite URL for one benchmark database.

    Read-only matters: these files are the benchmark fixture. A generated query
    that somehow modified one would silently corrupt every later run.
    """
    path = data_root / "dev_databases" / db_id / f"{db_id}.sqlite"
    if not path.exists():
        raise FileNotFoundError(path)
    return read_only_url(f"sqlite:///{path}")


def normalise(value: object) -> object:
    """Make two equivalent values compare equal.

    Databases return ``1``, ``1.0`` and ``Decimal('1.00')`` for the same
    number depending on how the query was written. Rounding to six decimal
    places also absorbs floating-point noise from differently-ordered
    arithmetic.
    """
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, int):
        return float(value)
    if value is None:
        return None
    return str(value)


def result_signature(rows: list[tuple], *, ordered: bool) -> object:
    """Reduce a result set to something comparable.

    When the gold query has no ``ORDER BY``, row order carries no meaning, so
    compare as a multiset. When it does, order is part of the answer.
    """
    normalised = [tuple(normalise(cell) for cell in row) for row in rows]
    return normalised if ordered else Counter(normalised)


def run_gold(url: str, sql: str) -> list[tuple]:
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            return [tuple(row) for row in connection.execute(text(sql)).fetchall()]
    finally:
        engine.dispose()


def evaluate_one(
    item: dict,
    data_root: Path,
    config: Settings,
    client: MantleClient,
    postgres_url: str | None = None,
    judge: bool = False,
) -> Outcome:
    question_id = item["question_id"]
    db_id = item["db_id"]
    gold_sql = item["SQL"]

    # BIRD supplies "evidence": a domain hint that the benchmark intends to be
    # available to the model (e.g. what "eligible free rate" means). Including
    # it is standard practice; without it many questions are unanswerable by
    # anyone, human included.
    question = item["question"]
    evidence = (item.get("evidence") or "").strip()
    prompt_question = f"{question}\n\nContext: {evidence}" if evidence else question

    # In PostgreSQL mode every question runs against the same merged database,
    # so the agent must find the right 2-3 tables among 75 rather than being
    # handed a 3-13 table database that already contains only relevant ones.
    url = postgres_url or database_url(data_root, db_id)
    engine = create_engine(url)

    try:
        agent = SqlAgent(engine, client=client, config=config)
        result = agent.ask(prompt_question)

        correct = False
        error = result.error

        if result.ok and result.sql and result.result is not None:
            try:
                gold_rows = run_gold(url, gold_sql)
                ordered = "order by" in gold_sql.lower()
                correct = result_signature(
                    [tuple(row) for row in result.result.rows], ordered=ordered
                ) == result_signature(gold_rows, ordered=ordered)
            except Exception as exc:  # gold query failed to run
                error = f"gold_sql_failed: {exc}"

        # Graded after correctness, never instead of it. The judge reads the
        # answer; execution accuracy reads the rows. A question can be correct
        # and badly answered, and that combination is the interesting one.
        verdict = None
        if judge and result.ok and result.sql and result.result is not None:
            verdict = judge_answer(
                client,
                question=question,
                sql=result.sql,
                result_preview=result.result.preview(limit=20),
                answer=result.answer,
                model=config.judge_model or config.strong_model,
            )

        return Outcome(
            question_id=question_id,
            db_id=db_id,
            question=question,
            difficulty=item.get("difficulty", "unknown"),
            correct=correct,
            predicted_sql=result.sql,
            gold_sql=gold_sql,
            error=error,
            repairs=result.trace.repair_count,
            model_calls=result.trace.model_calls,
            input_tokens=result.trace.input_tokens,
            output_tokens=result.trace.output_tokens,
            seconds=result.trace.seconds,
            answer=result.answer,
            intent_rejections=tuple(result.trace.intent_rejections),
            intent_asked=(
                result.trace.intent_asks[0] if result.trace.intent_asks else ""
            ),
            judged=verdict is not None,
            judge_ok=bool(verdict and verdict.ok),
            judge_defects=tuple(verdict.defects) if verdict else (),
            judge_problem=verdict.problem if verdict else "",
        )
    finally:
        engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the BIRD mini-dev benchmark")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--limit", type=int, default=50, help="how many questions")
    parser.add_argument("--full", action="store_true", help="run all 500")
    parser.add_argument("--model", type=str, default=None, help="override both model tiers")
    parser.add_argument("--workers", type=int, default=4, help="parallel questions")
    parser.add_argument("--seed", type=int, default=7, help="sampling seed")
    parser.add_argument("--samples", type=int, default=2, help="sample rows per table")
    # Deliberately far above the production default. The harness compares full
    # result sets, so a cap that truncates a correct answer scores it as wrong.
    # Measured at 1000, this cost 5 of 65 failures: gold results of 1105-3339
    # rows were marked incorrect purely because our side had been capped.
    parser.add_argument(
        "--row-limit", type=int, default=100_000, help="row cap during evaluation"
    )
    # The PostgreSQL distribution of BIRD merges all 11 databases into a single
    # 75-table `public` schema. That makes it the only available test of schema
    # retrieval at realistic scale: with one SQLite file per database the agent
    # sees 3-13 tables and skips retrieval entirely, so the graph walk that the
    # architecture is built around is never exercised.
    parser.add_argument(
        "--postgres",
        type=str,
        default=None,
        metavar="URL",
        help="run against a merged PostgreSQL database instead of per-database SQLite files",
    )
    # --- the quality toggles -------------------------------------------------
    #
    # Each of these exists precisely so its value can be measured rather than
    # assumed, which is impossible if the harness cannot switch it on. Without
    # these flags the benchmark only ever scores the default configuration, and
    # every toggle's worth stays a matter of opinion.
    #
    # `ask_human` is deliberately absent: an automated run cannot answer a
    # clarifying question, so a benchmark with it enabled would stall on every
    # ambiguous question and report a number describing a system nobody runs.
    parser.add_argument(
        "--tier",
        # Read from the engine rather than written out here. This list said
        # ["fast", "thorough"] for a while after `medium` was added, so a sweep
        # over all three silently skipped the middle one and nobody noticed
        # until the log was read. A literal copy of an enum is a copy that goes
        # stale.
        choices=list(TIERS),
        default=None,
        help="quality tier: fast, medium (voting), thorough (voting, critic, decomposition)",
    )
    parser.add_argument(
        "--critic", action="store_true", help="Loop C: review the SQL before running it"
    )
    parser.add_argument(
        "--intent", action="store_true",
        help="Loop D: after the query runs, ask whether the ROWS answer the "
             "question. Note that an `ask` cannot be answered by a harness and "
             "scores as a failure, so this number understates the behaviour",
    )
    parser.add_argument(
        "--vote", type=int, default=None, metavar="N",
        help="generate the query N times and keep the one that recurs",
    )
    parser.add_argument(
        "--vote-temperature", type=float, default=None,
        help="temperature for voting samples (voting is pointless at 0)",
    )
    parser.add_argument(
        "--decompose", action="store_true",
        help="split a multi-part question and answer each part",
    )
    parser.add_argument(
        "--prescreen", action="store_true", help="screen the question before acting"
    )
    parser.add_argument(
        "--glossary", type=Path, default=None,
        help="domain notes: units, terms and metrics the schema cannot express",
    )
    parser.add_argument(
        "--no-cost-gate", action="store_true",
        help="skip the EXPLAIN cost check (it is on by default)",
    )
    parser.add_argument(
        "--no-faithfulness", action="store_true",
        help="skip checking the answer against the rows it describes",
    )
    parser.add_argument(
        "--cache", action="store_true",
        help="reuse SQL across identical questions (off here: it would flatter a "
             "benchmark that asks each question once)",
    )

    parser.add_argument(
        "--judge", action="store_true",
        help="grade each answer's prose with a second model: does it answer the "
             "question asked, is it complete, does it claim only what the rows "
             "show. Measures the sentence, which execution accuracy does not",
    )
    parser.add_argument(
        "--judge-model", type=str, default=None,
        help="model that grades the answers. Defaults to the strong tier; "
             "a different family is better, since a judge sharing the author's "
             "blind spot scores a bad answer well",
    )

    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    questions = load_questions(args.data, postgres=bool(args.postgres))

    if not args.full:
        # A fixed seed makes runs comparable: the same subset every time, so a
        # score change reflects a code change rather than a different sample.
        random.Random(args.seed).shuffle(questions)
        questions = questions[: args.limit]

    overrides: dict[str, object] = {
        "sample_rows": args.samples,
        "row_limit": args.row_limit,
        # Off unless asked for. A cache would make a re-run of the same
        # questions look faster and cheaper than the system actually is, and
        # BIRD asks each question once anyway.
        "cache_sql": args.cache,
        # The harness cannot answer a clarifying question, so it declares that
        # here rather than relying on a product default chosen for its
        # convenience. With `ask_human` — now the default for real users — every
        # ambiguous question would stall and the score would be meaningless.
        "ambiguity_handling": "best_effort",
    }
    if args.model:
        overrides["light_model"] = args.model
        overrides["strong_model"] = args.model
    if args.judge_model:
        # Set even when --judge is not passed, so a run that was not graded
        # still records which judge would have graded it.
        overrides["judge_model"] = args.judge_model
    if args.tier:
        overrides["quality_tier"] = args.tier
    if args.critic:
        overrides["use_critic"] = True
    if args.intent:
        overrides["check_result_intent"] = True
    if args.vote:
        overrides["vote_samples"] = args.vote
    if args.vote_temperature is not None:
        overrides["vote_temperature"] = args.vote_temperature
    if args.decompose:
        overrides["decompose_questions"] = True
    if args.prescreen:
        overrides["prescreen_input"] = True
    if args.glossary:
        overrides["glossary_path"] = str(args.glossary)
    if args.no_cost_gate:
        overrides["max_plan_cost"] = 0
        overrides["max_plan_rows"] = 0
    if args.no_faithfulness:
        overrides["check_answer_faithfulness"] = False

    config = Settings(**overrides)
    client = MantleClient(config)

    if args.postgres:
        print("mode: merged PostgreSQL schema (retrieval under test)")
    # Print the configuration under test, not just the model. A score is
    # meaningless without it, and a result file that does not say which toggles
    # were on cannot be compared against another six weeks later.
    enabled = [
        name
        for name, on in (
            (f"tier={config.quality_tier}", config.quality_tier != "fast"),
            ("critic", config.use_critic),
            ("intent", config.check_result_intent),
            (f"vote={config.vote_samples}@{config.vote_temperature}", config.vote_samples > 1),
            ("decompose", config.decompose_questions),
            ("prescreen", config.prescreen_input),
            (f"glossary={config.glossary_path}", bool(config.glossary_path)),
            ("cache", config.cache_sql),
            ("cost-gate", config.max_plan_cost > 0),
            ("faithfulness", config.check_answer_faithfulness),
            (f"judge={config.judge_model or config.strong_model}", args.judge),
        )
        if on
    ]
    print(
        f"BIRD mini-dev: {len(questions)} questions | "
        f"light={config.light_model} strong={config.strong_model} | "
        f"workers={args.workers}\n"
        f"enabled: {', '.join(enabled) if enabled else 'nothing beyond the base pipeline'}",
        flush=True,
    )

    started = time.monotonic()
    outcomes: list[Outcome] = []

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(
                evaluate_one, item, args.data, config, client, args.postgres, args.judge
            ): item
            for item in questions
        }
        for index, future in enumerate(as_completed(futures), start=1):
            item = futures[future]
            try:
                outcome = future.result()
            except Exception as exc:  # noqa: BLE001 - one bad question must not stop the run
                outcome = Outcome(
                    question_id=item["question_id"],
                    db_id=item["db_id"],
                    question=item["question"],
                    difficulty=item.get("difficulty", "unknown"),
                    correct=False,
                    predicted_sql=None,
                    gold_sql=item["SQL"],
                    error=f"harness_error: {exc}",
                    repairs=0,
                    model_calls=0,
                    input_tokens=0,
                    output_tokens=0,
                    seconds=0.0,
                )

            outcomes.append(outcome)
            running = sum(1 for o in outcomes if o.correct) / len(outcomes)
            mark = "ok  " if outcome.correct else "FAIL"
            print(
                f"[{index:>3}/{len(questions)}] {mark} {running:>6.1%}  "
                f"{outcome.db_id[:22]:<22} {outcome.question[:50]}",
                flush=True,
            )

    elapsed = time.monotonic() - started
    report(outcomes, elapsed, config, args.out)
    return 0


def report(
    outcomes: list[Outcome], elapsed: float, config: Settings, out: Path | None
) -> None:
    total = len(outcomes)
    correct = sum(1 for o in outcomes if o.correct)
    accuracy = correct / total if total else 0.0

    # First-attempt accuracy separates "the model got it right" from "the repair
    # loop rescued it", which are different things to improve.
    first_try = sum(1 for o in outcomes if o.correct and o.repairs == 0)
    repaired = sum(1 for o in outcomes if o.repairs > 0)

    print("\n" + "=" * 64)
    print(f"Accuracy           {accuracy:.1%}  ({correct}/{total})")
    print(f"Correct first try  {first_try / total:.1%}" if total else "")
    print(f"Needed a repair    {repaired / total:.1%}" if total else "")
    print(f"Wall clock         {elapsed:.0f}s")
    print(f"Mean per question  {sum(o.seconds for o in outcomes) / max(total, 1):.1f}s")
    print(f"Tokens in/out      {sum(o.input_tokens for o in outcomes):,} / "
          f"{sum(o.output_tokens for o in outcomes):,}")

    print("\nBy difficulty:")
    for level in ("simple", "moderate", "challenging"):
        subset = [o for o in outcomes if o.difficulty == level]
        if subset:
            hit = sum(1 for o in subset if o.correct)
            print(f"  {level:<12} {hit / len(subset):>6.1%}  ({hit}/{len(subset)})")

    # Answer quality, where it was measured. Reported next to accuracy and never
    # folded into it: a query can return exactly the gold rows and be written up
    # as an answer to a different question, and a single combined number would
    # hide which of the two went wrong.
    graded = [o for o in outcomes if o.judged]
    if graded:
        good = sum(1 for o in graded if o.judge_ok)
        ungraded = sum(1 for o in outcomes if not o.judged)
        print(f"\nAnswer quality     {good / len(graded):.1%}  ({good}/{len(graded)})")
        print(f"  ungraded         {ungraded}")
        defects = Counter(defect for o in graded for defect in o.judge_defects)
        for defect, count in defects.most_common():
            print(f"  {defect:<18} {count}")

        # The combination worth looking at by hand: right rows, wrong write-up.
        # Nothing else in this harness can surface it.
        both = sum(1 for o in graded if o.correct and not o.judge_ok)
        if both:
            print(f"  correct but badly answered  {both}")

    failures = Counter(
        (o.error or "wrong_result").split(":")[0] for o in outcomes if not o.correct
    )
    if failures:
        print("\nFailure reasons:")
        for reason, count in failures.most_common(8):
            print(f"  {reason:<28} {count}")

    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "accuracy": round(accuracy, 4),
                    "correct": correct,
                    "total": total,
                    "answer_quality": summarise_verdicts(
                        [
                            Verdict.from_defects(o.judge_defects, o.judge_problem)
                            if o.judged
                            else None
                            for o in outcomes
                        ]
                    ).to_dict()
                    if any(o.judged for o in outcomes)
                    else None,
                    "light_model": config.light_model,
                    "strong_model": config.strong_model,
                    # The configuration is recorded with the score. A result
                    # file that does not say which toggles were on cannot be
                    # compared against another one six weeks later, and an
                    # accuracy number without its settings is not a measurement.
                    "config": {
                        name: getattr(config, name)
                        for name in (
                            "quality_tier", "use_critic", "vote_samples",
                            "vote_temperature", "decompose_questions",
                            "prescreen_input", "ambiguity_handling",
                            "glossary_path", "cache_sql",
                            "max_plan_cost", "check_answer_faithfulness",
                            "check_inflated_aggregates", "sample_rows",
                            "full_schema_threshold", "initial_hops", "max_hops",
                            "max_repair_attempts", "row_limit",
                            "judge_model",
                        )
                    },
                    "seconds": round(elapsed, 1),
                    "outcomes": [asdict(o) for o in outcomes],
                },
                indent=2,
            )
        )
        print(f"\nWrote {out}")


if __name__ == "__main__":
    raise SystemExit(main())
