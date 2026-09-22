#!/usr/bin/env python3
"""Re-grade a finished run's answers without re-running it.

Why this is possible at all
---------------------------
Every outcome stores the answer text, not just the verdict. That was a design
decision with exactly this in mind: **judging is cheap, answering is not.** A
150-question run costs a few hundred thousand tokens and several minutes of
database work; grading those same 150 answers costs 150 short prompts and no
database at all.

Why it is needed
----------------
The judge that ran inline may not be the judge you want. On this deployment it
was not: Mantle's ``/openai/v1`` bearer route serves exactly one model, so the
benchmark's ``--judge`` graded gemma-4-31b's answers with gemma-4-31b. A
reviewer and an author failing the same way is the failure mode of any
self-review scheme, and here that failure produces a **good score for a bad
answer** rather than a bad answer — the score is the output, so a shared blind
spot corrupts the measurement itself rather than the product.

The other models live on ``/v1`` with SigV4. This builds a second client
pointed there, so the grading is done by a model from a different family
against answers already on disk.

Usage
-----
    python benchmarks/rejudge.py benchmarks/results/run.json \\
        --judge-model qwen.qwen3-coder-480b-a35b-instruct \\
        --judge-base-url https://bedrock-mantle.us-east-1.api.aws/v1 \\
        --judge-auth sigv4
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlagent.config import Settings  # noqa: E402
from sqlagent.judge import judge_answer, summarise_verdicts  # noqa: E402
from sqlagent.llm.mantle import MantleClient  # noqa: E402


def grade(client, model: str, outcome: dict):
    """Grade one stored outcome, or None if there is nothing to grade.

    An outcome with no answer was never answered — a failure, a refusal, a
    clarification. Sending it to the judge would ask a question about a
    sentence that does not exist, and whatever came back would be noise added
    to the very number this is meant to sharpen.
    """
    answer = (outcome.get("answer") or "").strip()
    sql = outcome.get("predicted_sql")
    if not answer or not sql:
        return None

    return judge_answer(
        client,
        question=outcome["question"],
        sql=sql,
        # The rows themselves are not stored — only the answer and the SQL.
        # The judge is told so rather than being handed an empty table, which
        # it would read as "the query returned nothing" and mark every answer
        # unsupported.
        result_preview=(
            "(rows not retained from the original run; judge whether the answer "
            "is an answer to the question and internally consistent with the "
            "query, and do not mark it unsupported merely for rows you cannot "
            "see)"
        ),
        answer=answer,
        model=model,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("results", type=Path, help="a run written by bird.py --out")
    parser.add_argument("--judge-model", type=str, required=True)
    parser.add_argument("--judge-base-url", type=str, default=None)
    parser.add_argument(
        "--judge-auth", choices=("sigv4", "bedrock_token", "bearer"), default=None
    )
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    payload = json.loads(args.results.read_text())
    outcomes = payload["outcomes"]

    overrides: dict = {}
    if args.judge_base_url:
        overrides["llm_base_url"] = args.judge_base_url
    if args.judge_auth:
        overrides["llm_auth"] = args.judge_auth
    client = MantleClient(Settings(**overrides))

    gradable = [o for o in outcomes if (o.get("answer") or "").strip()]
    print(
        f"re-judging {len(gradable)} of {len(outcomes)} answers "
        f"with {args.judge_model}",
        flush=True,
    )

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        verdicts = list(pool.map(lambda o: grade(client, args.judge_model, o), outcomes))

    report = summarise_verdicts(verdicts).to_dict()

    print("\n" + "=" * 56)
    print(f"Answer quality     {report['quality']:.1%}  ({report['good']}/{report['graded']})")
    print(f"  ungraded         {report['ungraded']}")
    for defect, count in sorted(report["by_defect"].items(), key=lambda kv: -kv[1]):
        print(f"  {defect:<18} {count}")

    # The combination nothing else surfaces: the query returned the gold rows
    # and the write-up was still wrong.
    both = [
        o for o, v in zip(outcomes, verdicts, strict=True)
        if v is not None and o.get("correct") and not v.ok
    ]
    if both:
        print(f"\ncorrect but badly answered: {len(both)}")
        for outcome, verdict in (
            (o, v) for o, v in zip(outcomes, verdicts, strict=True)
            if v is not None and o.get("correct") and not v.ok
        ):
            print(f"  [{','.join(verdict.defects)}] {outcome['question'][:64]}")
            if verdict.problem:
                print(f"      {verdict.problem[:90]}")

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(
                {
                    "source": str(args.results),
                    "judge_model": args.judge_model,
                    "answer_quality": report,
                    "verdicts": [
                        None
                        if v is None
                        else {
                            "question_id": o.get("question_id"),
                            "defects": list(v.defects),
                            "problem": v.problem,
                        }
                        for o, v in zip(outcomes, verdicts, strict=True)
                    ],
                },
                indent=2,
            )
        )
        print(f"\nWrote {args.out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
