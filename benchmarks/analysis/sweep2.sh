#!/bin/bash
# Second sweep: the arms the first one did not cover.
#
# Interleaved, not blocked. The first sweep ran five baselines and then every
# other arm, and its baseline-vs-baseline nets averaged +2.8 rather than 0 with
# accuracy rising through the hour — so anything running later was flattered by
# whatever that drift is. Here each cycle runs one of every arm.
set -u
cd /home/jntbatra/Projects/sql-agent
LOG=benchmarks/analysis/sweep2.log
EX_DOCS="--column-docs"
run () {
  local name=$1; shift
  [ -f "benchmarks/results/sweep2-$name.json" ] && { echo "skip $name (exists)"; return; }
  echo "=== $name  $(date +%H:%M:%S)"
  env PYTHONPATH= .venv/bin/python benchmarks/bird.py --full --flex --workers 8 \
      "$@" --out "benchmarks/results/sweep2-$name.json" >> $LOG 2>&1
  grep "^Accuracy" $LOG | tail -1
}
for i in 1 2 3; do
  run "base-$i"
  run "intent-$i"      --intent
  run "intdocs-$i"     --intent $EX_DOCS
  [ "$i" -lt 3 ] && run "rebind-$i"     --rebind
  [ "$i" -lt 3 ] && run "docsrebind-$i" $EX_DOCS --rebind
done
echo "SWEEP2 COMPLETE $(date +%H:%M:%S)"
