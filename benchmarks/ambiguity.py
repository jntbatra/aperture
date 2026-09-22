#!/usr/bin/env python3
"""Measure how stable the ambiguity check is.

The problem
-----------
``ambiguity_handling="ask_human"`` is the product default: a question with more
than one defensible answer is put back to the user rather than guessed at. It
is a model call, and a model call is not a function. The same question asked
twice can be judged ambiguous once and answered outright the next time, and
there is no way to tell from the outside which of the two you got.

That matters more here than for most model calls. An unstable *answer* is
visible — the figures differ. An unstable *decision to ask* is invisible: the
user who gets the question back concludes the tool is careful, and the user who
does not concludes it is decisive, and they are running the same system.

This is not a benchmark of accuracy
-----------------------------------
Two numbers come out, and they are different things:

* **stability** — how often ``k`` repeats of the same question reach the same
  verdict. A property of the system, measured with no ground truth at all.
* **agreement with the labels** — how often the majority verdict matches what a
  human said the question was. A property of the prompt and the model, and
  only as good as the labels, which are one person's judgement about their own
  database.

Stability is the number this file exists for. It needs no labels and cannot be
argued with, which is the opposite of the accuracy number.

Cost
----
``questions x k`` model calls and nothing else — no database, no SQL, no
answers. The default of 30 questions at k=5 is 150 calls of a few hundred
tokens each.

Usage
-----
    python benchmarks/ambiguity.py --questions benchmarks/ambiguity_questions.json -k 5
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import create_engine  # noqa: E402  (imported after the path insert)

from sqlagent.clarify import needs_clarification  # noqa: E402
from sqlagent.config import Settings  # noqa: E402
from sqlagent.llm.mantle import MantleClient  # noqa: E402
from sqlagent.pipeline import SqlAgent  # noqa: E402


@dataclass
class QuestionResult:
    """One question asked ``k`` times."""

    question: str
    expected: bool | None
    """What a human said it was, or None for an unlabelled question.

    Unlabelled questions still contribute to stability, which needs no labels.
    """

    verdicts: list[bool] = field(default_factory=list)
    asks: list[list[str]] = field(default_factory=list)
    """The clarifying questions each run produced. Two runs can both say
    "ambiguous" and disagree completely about what is unclear, which counts as
    stable here and is worth being able to see."""

    @property
    def stable(self) -> bool:
        return len(set(self.verdicts)) <= 1

    @property
    def majority(self) -> bool:
        return sum(self.verdicts) * 2 > len(self.verdicts)

    @property
    def ambiguous_rate(self) -> float:
        return sum(self.verdicts) / len(self.verdicts) if self.verdicts else 0.0


def summarise(results: list[QuestionResult]) -> dict:
    """Reduce the runs to the numbers worth quoting.

    Kept separate from the running so it can be tested without a model, and so
    a saved run can be re-summarised without being repeated.
    """
    graded = [r for r in results if r.verdicts]
    labelled = [r for r in graded if r.expected is not None]

    stable = sum(1 for r in graded if r.stable)
    agreed = sum(1 for r in labelled if r.majority == r.expected)

    # Which way an unstable question leans is the actionable part. A question
    # that comes back ambiguous 4 times in 5 needs a prompt fix; one at 1 in 5
    # is closer to noise.
    unstable = sorted(
        (r for r in graded if not r.stable),
        key=lambda r: abs(r.ambiguous_rate - 0.5),
    )

    return {
        "questions": len(graded),
        "repeats": max((len(r.verdicts) for r in graded), default=0),
        "stability": round(stable / len(graded), 4) if graded else 0.0,
        "stable": stable,
        "unstable": len(graded) - stable,
        "labelled": len(labelled),
        "agreement": round(agreed / len(labelled), 4) if labelled else 0.0,
        # Split by label: asking about a clear question and failing to ask about
        # a vague one are different failures with different fixes, and one rate
        # hides which is happening.
        "asks_when_vague": _rate(labelled, expected=True),
        "asks_when_clear": _rate(labelled, expected=False),
        "least_stable": [
            {"question": r.question, "ambiguous_rate": round(r.ambiguous_rate, 3)}
            for r in unstable[:10]
        ],
    }


def _rate(results: list[QuestionResult], *, expected: bool) -> float:
    subset = [r for r in results if r.expected is expected]
    if not subset:
        return 0.0
    return round(sum(1 for r in subset if r.majority) / len(subset), 4)


def run_one(agent: SqlAgent, config: Settings, item: dict, repeats: int) -> QuestionResult:
    question = item["question"]
    result = QuestionResult(question=question, expected=item.get("ambiguous"))

    schema_text = "Tables: " + ", ".join(sorted(agent.snapshot.tables))
    for _ in range(repeats):
        clarification = needs_clarification(
            agent.client,
            question=question,
            schema_text=schema_text,
            model=config.light_model,
        )
        result.verdicts.append(clarification is not None)
        result.asks.append(
            [ask.question for ask in clarification.asks] if clarification else []
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--questions",
        type=Path,
        default=ROOT / "benchmarks" / "ambiguity_questions.json",
        help="JSON list of {question, ambiguous} — `ambiguous` may be omitted",
    )
    parser.add_argument("-k", "--repeats", type=int, default=5)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--model", type=str, default=None)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    items = json.loads(args.questions.read_text())
    overrides: dict = {}
    if args.model:
        overrides["light_model"] = args.model

    config = Settings(**overrides)
    client = MantleClient(config)
    engine = create_engine(config.database_url)
    agent = SqlAgent(engine, client=client, config=config)
    agent.warm()

    print(
        f"ambiguity stability: {len(items)} questions x {args.repeats} repeats "
        f"= {len(items) * args.repeats} model calls | model={config.light_model}",
        flush=True,
    )

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(
            pool.map(lambda item: run_one(agent, config, item, args.repeats), items)
        )

    report = summarise(results)
    print("\n" + "=" * 56)
    print(f"Stability          {report['stability']:.1%}  "
          f"({report['stable']}/{report['questions']} always agreed with themselves)")
    if report["labelled"]:
        print(f"Agreement          {report['agreement']:.1%}  "
              f"over {report['labelled']} labelled questions")
        print(f"  asks when vague  {report['asks_when_vague']:.1%}")
        print(f"  asks when clear  {report['asks_when_clear']:.1%}  (lower is better)")

    if report["least_stable"]:
        print("\nLeast stable:")
        for entry in report["least_stable"]:
            print(f"  {entry['ambiguous_rate']:.2f}  {entry['question'][:70]}")

    spread = Counter(len(set(r.verdicts)) for r in results)
    print(f"\nVerdict spread: {dict(sorted(spread.items()))} (1 = every repeat agreed)")

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(
                {
                    "summary": report,
                    "model": config.light_model,
                    "repeats": args.repeats,
                    "results": [asdict(r) for r in results],
                },
                indent=2,
            )
        )
        print(f"\nWrote {args.out}")

    engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
