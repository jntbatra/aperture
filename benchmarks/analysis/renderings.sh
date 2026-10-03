#!/bin/bash
# High setting: intent + column docs + 3 schema renderings, three full runs.
# Compared per question against sweep2-intdocs-{1,2,3} (same flags minus --renderings).
set -u
cd /home/jntbatra/Projects/sql-agent
LOG=benchmarks/analysis/renderings.log
for i in 1 2 3; do
  OUT="benchmarks/results/renderings-intdocs-$i.json"
  [ -f "$OUT" ] && { echo "skip $i (exists)"; continue; }
  echo "=== run $i  $(date +%H:%M:%S)" | tee -a $LOG
  env PYTHONPATH= .venv/bin/python benchmarks/bird.py --full --flex --workers 8 \
      --intent --column-docs --renderings 3 --out "$OUT" >> $LOG 2>&1
  grep "^Accuracy" $LOG | tail -1
done
echo "RENDERINGS COMPLETE $(date +%H:%M:%S)"
