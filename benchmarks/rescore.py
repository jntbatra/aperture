#!/usr/bin/env python3
"""Re-score a finished run against corrected gold annotations.

Why this exists
---------------
BIRD's gold SQL is not ground truth. "Pervasive Annotation Errors Break
Text-to-SQL Benchmarks and Leaderboards" (VLDB 2026, uiuc-kang-lab) reports a
**52.8% error rate on BIRD Mini-Dev** and, re-evaluating 16 published agents
against a corrected set, finds execution accuracy moving by −7% to +31% and
leaderboard positions moving by up to nine places.

That makes every number measured against the original gold — ours and every
published one — a measurement against a noisy target. A system can be marked
wrong for producing the *right* answer.

No model calls
--------------
Each outcome already stores `predicted_sql`, so this re-executes the stored
statement and the corrected gold and compares result sets. Re-running the
agent would cost a full run and change two variables at once; this changes
exactly one, which is the point.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bird_helpers import (  # noqa: E402
    ScoringTimeout,
    database_url,
    result_signature,
    run_gold,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("results", type=Path)
    parser.add_argument("--corrected", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    args = parser.parse_args()

    # The corrected gold is the basis of every number this prints, and it is
    # not vendored — it is fetched from a repository that has no licence file.
    # An earlier corrected set was kept in a temporary directory, was deleted
    # mid-session, and took a day of "corrected" numbers with it: they could
    # not be reproduced, and when the canonical file was fetched it scored
    # 1.4 points differently. So every run says which file it used.
    digest = hashlib.sha256(args.corrected.read_bytes()).hexdigest()

    run = json.loads(args.results.read_text())
    # Matched on (database, question text), not on question_id. The corrected
    # release renumbers from 100 and BIRD's own ids run to 1533, so they do not
    # overlap at all — joining on the id silently matches nothing and reports a
    # confident zero.
    corrected = {
        (c["db_id"], " ".join(c["question"].split())): c
        for c in json.loads(args.corrected.read_text())
    }

    scored = {"same": 0, "now_right": 0, "now_wrong": 0, "unchanged_wrong": 0,
              "not_in_corrected": 0, "gold_failed": 0}
    gold_changed = 0

    for o in run["outcomes"]:
        fix = corrected.get((o["db_id"], " ".join(o["question"].split())))
        if fix is None:
            scored["not_in_corrected"] += 1
            continue

        if " ".join(fix["SQL"].split()) != " ".join(o["gold_sql"].split()):
            gold_changed += 1

        if not o["predicted_sql"]:
            scored["unchanged_wrong"] += 1
            continue

        url = database_url(args.data, o["db_id"])
        try:
            ordered = "order by" in fix["SQL"].lower()
            gold_rows = run_gold(url, fix["SQL"], timeout=0)
            try:
                mine = run_gold(url, o["predicted_sql"])
                now = result_signature(mine, ordered=ordered) == result_signature(
                    gold_rows, ordered=ordered
                )
            except ScoringTimeout:
                now = False  # the agent's own timeout would have failed it too
        except Exception:
            scored["gold_failed"] += 1
            continue

        was = o["correct"]
        if now and was:
            scored["same"] += 1
        elif now and not was:
            scored["now_right"] += 1
        elif was and not now:
            scored["now_wrong"] += 1
        else:
            scored["unchanged_wrong"] += 1

    judged = scored["same"] + scored["now_right"] + scored["now_wrong"] + scored["unchanged_wrong"]
    correct_now = scored["same"] + scored["now_right"]
    correct_before = scored["same"] + scored["now_wrong"]

    print(f"{args.results.name}")
    print(f"  corrected gold                       : {args.corrected.name}")
    print(f"  sha256                               : {digest[:16]}...")
    print(f"  gold SQL that the correction changed : {gold_changed}/{len(run['outcomes'])}")
    print(f"  comparable questions                 : {judged}")
    print(f"  accuracy on ORIGINAL gold            : {correct_before/judged:.1%} "
          f"({correct_before}/{judged})")
    print(f"  accuracy on CORRECTED gold           : {correct_now/judged:.1%} "
          f"({correct_now}/{judged})")
    print(f"    marked wrong before, right now     : {scored['now_right']}")
    print(f"    marked right before, wrong now     : {scored['now_wrong']}")
    print(f"  gold or prediction failed to run     : {scored['gold_failed']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
