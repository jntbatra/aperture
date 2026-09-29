#!/usr/bin/env python3
"""Multi-seed analysis on corrected gold. Zero model calls.

Lives in the repository rather than a scratch directory: the scratchpad that
held the first version of this was cleared twice in one day, and an analysis
you cannot re-run is not a measurement.
"""
import json
import math
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "benchmarks"))
from bird_helpers import database_url, result_signature, run_gold  # noqa: E402

DATA = Path("/home/jntbatra/Projects/aperture/benchmarks/minidev/MINIDEV")
GOLD = Path("/home/jntbatra/Projects/aperture/benchmarks/corrected-gold/data/arcwise_plat_sql.json")
corrected = {(c["db_id"], " ".join(c["question"].split())): c for c in json.loads(GOLD.read_text())}

_cache = {}
def ok(o, fix):
    if not o["predicted_sql"]:
        return False
    key = (o["db_id"], o["predicted_sql"], fix["SQL"])
    if key not in _cache:
        ordered = "order by" in fix["SQL"].lower()
        url = database_url(DATA, o["db_id"])
        try:
            _cache[key] = result_signature(run_gold(url, o["predicted_sql"]), ordered=ordered) == \
                          result_signature(run_gold(url, fix["SQL"]), ordered=ordered)
        except Exception:
            _cache[key] = None
    return _cache[key]

MAX_UNANSWERED = 0.05
"""Above this share of questions with no SQL at all, the run is an outage.

sweep-exem-2 produced no SQL for 339 of 500 questions and 338 "Mantle call
failed after 4 attempts" errors — connection resets, SSL EOF, DNS failure —
over 3,498 seconds against roughly 400 for its siblings. Averaged in, it
dragged that arm's mean from 66.6% to 50.9% and turned a real -5.8pp effect
into a meaningless -89 net. A failed network is not a measurement, and a
sweep that silently averages one is worse than no sweep.
"""


def usable(name):
    """Whether a run actually ran. Prints its reason when it did not."""
    outcomes = json.loads((ROOT / "benchmarks/results" / name).read_text())["outcomes"]
    unanswered = sum(1 for o in outcomes if not o["predicted_sql"])
    if unanswered > MAX_UNANSWERED * len(outcomes):
        print(f"  EXCLUDED {name}: {unanswered}/{len(outcomes)} questions produced no SQL")
        return False
    return True


def score(name):
    out = {}
    for o in json.loads((ROOT / "benchmarks/results" / name).read_text())["outcomes"]:
        k = (o["db_id"], " ".join(o["question"].split()))
        fix = corrected.get(k)
        if fix is None:
            continue
        v = ok(o, fix)
        if v is not None:
            out[k] = v
    return out

def mcnemar(r, w):
    n = r + w
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(r, w) + 1))
    return min(1.0, 2 * tail / 2**n)

ARMS = {
    "baseline":    [f"sweep-base-{i}.json" for i in (1, 2, 3, 4, 5)],
    "column-docs": [f"sweep-docs-{i}.json" for i in (1, 2, 3)],
    "exemplars":   [f"sweep-exem-{i}.json" for i in (1, 2, 3)],
}
scored = {
    a: [score(f) for f in fs
        if (ROOT / "benchmarks/results" / f).exists() and usable(f)]
    for a, fs in ARMS.items()
}
scored = {a: v for a, v in scored.items() if v}
keys = sorted(set.intersection(*[set(s) for ss in scored.values() for s in ss]))
print(f"questions scorable in every run: {len(keys)}\n")

print(f"{'arm':14} {'n':>2}  {'accuracy per run':<28} {'mean':>7} {'sd':>6}")
for arm, runs in scored.items():
    accs = [sum(s[k] for k in keys) / len(keys) * 100 for s in runs]
    sd = st.stdev(accs) if len(accs) > 1 else 0.0
    shown = " ".join(f"{a:.1f}" for a in accs)
    print(f"{arm:14} {len(runs):>2}  {shown:<28} {st.mean(accs):>6.1f}% {sd:>5.2f}")

base = scored["baseline"]
pairs = [(i, j) for i in range(len(base)) for j in range(i + 1, len(base))]
disc, nets = [], []
for i, j in pairs:
    r = sum(1 for k in keys if base[j][k] and not base[i][k])
    w = sum(1 for k in keys if base[i][k] and not base[j][k])
    disc.append(r + w)
    nets.append(r - w)
print(f"\nNULL distribution — {len(pairs)} baseline-vs-baseline pairs")
print(f"  discordant : mean {st.mean(disc):.1f}, range {min(disc)}-{max(disc)}")
print(f"  net        : {sorted(nets)}")
print(f"               mean {st.mean(nets):+.1f}, sd {st.stdev(nets):.1f}, "
      f"largest |net| {max(abs(x) for x in nets)}")

for arm in ("column-docs", "exemplars"):
    if arm not in scored:
        continue
    ns, ps = [], []
    for t in scored[arm]:
        for b in base:
            r = sum(1 for k in keys if t[k] and not b[k])
            w = sum(1 for k in keys if b[k] and not t[k])
            ns.append(r - w)
            ps.append(mcnemar(r, w))
    print(f"\n{arm} vs every baseline run ({len(ns)} comparisons)")
    print(f"  net : mean {st.mean(ns):+.1f}, sd {st.stdev(ns):.1f}, "
          f"range {min(ns):+d}..{max(ns):+d}")
    hits = sum(1 for p in ps if p < 0.05)
    print(f"  p   : median {st.median(ps):.4f}, below 0.05 in {hits}/{len(ps)}")
