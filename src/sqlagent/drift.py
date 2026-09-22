"""Notice when the agent has started behaving differently.

Why this is hard to do honestly
--------------------------------
Nothing here measures whether an answer was *right*. There is no label. What
there is, in the history table the app already writes, is a record of what
every question cost and how it went: did it succeed, did it need a repair, how
long it took, how many tokens, did the query come back empty.

Those are proxies. A rise in the failure rate is not proof the agent got worse
— the questions may have got harder, or one user may have spent an afternoon on
an unfamiliar corner of the schema. The honest claim this module makes is
narrow: **this set of questions was answered measurably differently from that
set**, with a p-value, and a human decides what it means.

The alternative — a dashboard of rates with no test attached — is worse. Every
metric moves between any two samples, so a chart without significance produces
a false alarm on a slow Tuesday and trains everyone to ignore it.

Everything is a proportion
--------------------------
Five signals, all expressed as rates, all tested the same way with a
two-proportion z-test:

* **failure_rate** — the question was not answered at all
* **repair_rate** — the first query was wrong and the loop rescued it
* **empty_rate** — the query ran and returned nothing, which is usually a
  question that was misunderstood rather than data that does not exist
* **slow_rate** — slower than the baseline's own 90th percentile
* **costly_rate** — more tokens than the baseline's own 90th percentile

Latency and cost are continuous, and comparing their means properly needs a
rank test or an assumption about their distribution that response times do not
satisfy. Turning each into "how often is it worse than this period used to be"
converts it into a proportion, where the same exact test applies and the
threshold is set by the data rather than by someone's guess.

Two gates, not one
------------------
A shift is reported only when it is both statistically significant (``alpha``)
and large enough to act on (``min_change``). Significance alone, at a few
thousand questions, fires on a change from 4.0% to 4.4% — real, and not worth
anyone's attention. An effect size alone fires constantly at small n.

Not enough data is not "no drift"
---------------------------------
Below ``min_samples`` per side the report says so explicitly rather than
returning an empty list of shifts, because those two states look identical to a
caller and mean opposite things.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

MIN_SAMPLES = 30
"""Questions needed on each side before anything is tested.

Below this the z-test's normal approximation is not trustworthy and, more to
the point, neither is the sample: ten questions is one afternoon's work on one
topic, and a rate computed from it describes the afternoon.
"""

ALPHA = 0.01
"""Significance threshold. Deliberately stricter than the usual 0.05.

Five metrics are tested at once, so at 0.05 roughly one report in four would
contain a spurious shift, and an alert that is wrong a quarter of the time gets
switched off. 0.01 across five tests keeps the family-wise false-alarm rate
near 5%.
"""

MIN_CHANGE = 0.05
"""Smallest change in a rate worth reporting: five percentage points.

An absolute difference, not a relative one. A failure rate going from 1% to 2%
is a doubling and is almost certainly noise at any sample size this system
will see; 20% to 25% is not.
"""


@dataclass(frozen=True, slots=True)
class Sample:
    """One question, reduced to the five things drift is measured on.

    A record of behaviour, not of correctness — there is no label saying the
    answer was right, and this module never pretends otherwise.
    """

    ok: bool
    repaired: bool
    empty: bool
    seconds: float
    tokens: int


@dataclass(frozen=True, slots=True)
class Shift:
    """One metric that moved, with the evidence for saying so."""

    metric: str
    baseline: float
    recent: float
    baseline_n: int
    recent_n: int
    p_value: float

    @property
    def change(self) -> float:
        return round(self.recent - self.baseline, 4)

    @property
    def worse(self) -> bool:
        """Every metric here is a rate of something undesirable, so up is bad.

        Stated as a property rather than assumed by the caller: a drop in the
        failure rate is still drift, still worth knowing about, and must not be
        rendered as an alarm.
        """
        return self.recent > self.baseline

    def render(self) -> str:
        direction = "up" if self.worse else "down"
        return (
            f"{self.metric} {direction} from {self.baseline:.1%} to "
            f"{self.recent:.1%} (n={self.baseline_n} then {self.recent_n}, "
            f"p={self.p_value:.4f})"
        )


@dataclass(frozen=True, slots=True)
class DriftReport:
    """What the comparison found, including having found nothing."""

    baseline_n: int
    recent_n: int
    shifts: tuple[Shift, ...] = ()
    enough_data: bool = True
    """False when either side was below ``min_samples``.

    Separate from an empty ``shifts`` on purpose: "nothing moved" and "there
    was not enough to look at" are indistinguishable to a caller otherwise, and
    they mean opposite things.
    """

    rates: dict[str, tuple[float, float]] = field(default_factory=dict)
    """Every metric's ``(baseline, recent)`` pair, shifted or not.

    Reported alongside the shifts because a metric that did *not* move is
    evidence too — "latency is up but the failure rate held" is a different
    situation from "both moved", and only one of them is a correctness
    concern.
    """

    @property
    def drifted(self) -> bool:
        return bool(self.shifts)

    def render(self) -> str:
        if not self.enough_data:
            return (
                f"Not enough history to compare: {self.baseline_n} baseline and "
                f"{self.recent_n} recent questions."
            )
        if not self.shifts:
            return (
                f"No significant change across {self.baseline_n} baseline and "
                f"{self.recent_n} recent questions."
            )
        return "\n".join(shift.render() for shift in self.shifts)


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile of an already-collected sample.

    Written out rather than pulled from a dependency: it is six lines, and the
    alternative is adding a numerical stack to a project that otherwise needs
    none.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def two_proportion_p(hits_a: int, n_a: int, hits_b: int, n_b: int) -> float:
    """Two-sided p-value for two rates being the same.

    Pooled two-proportion z-test. ``erfc`` gives the normal tail exactly, so
    there is no table and no approximation beyond the test's own.

    Returns 1.0 — "no evidence of a difference" — when the test cannot be run:
    an empty side, or two rates that are both 0% or both 100%, where the pooled
    standard error is zero and the z-score is undefined. Returning 1.0 rather
    than raising keeps a degenerate metric from taking the whole report down.
    """
    if n_a <= 0 or n_b <= 0:
        return 1.0

    p_a = hits_a / n_a
    p_b = hits_b / n_b
    pooled = (hits_a + hits_b) / (n_a + n_b)
    variance = pooled * (1.0 - pooled) * (1.0 / n_a + 1.0 / n_b)
    if variance <= 0.0:
        return 1.0

    z = (p_b - p_a) / math.sqrt(variance)
    return math.erfc(abs(z) / math.sqrt(2.0))


def detect_drift(
    baseline: list[Sample],
    recent: list[Sample],
    *,
    min_samples: int = MIN_SAMPLES,
    alpha: float = ALPHA,
    min_change: float = MIN_CHANGE,
) -> DriftReport:
    """Compare two periods and report the metrics that moved.

    Args:
        baseline: The earlier period — what "normal" means here.
        recent: The period being checked against it.

    The thresholds for ``slow_rate`` and ``costly_rate`` come from the
    *baseline's* own 90th percentile, so "slow" means slow for this deployment
    rather than slow against a number someone picked. A consequence worth
    knowing: the baseline's slow_rate is 10% by construction, and the test is
    on how far the recent period departs from it.
    """
    if len(baseline) < min_samples or len(recent) < min_samples:
        return DriftReport(
            baseline_n=len(baseline),
            recent_n=len(recent),
            enough_data=False,
        )

    slow_threshold = percentile([s.seconds for s in baseline], 0.9)
    costly_threshold = percentile([float(s.tokens) for s in baseline], 0.9)

    predicates = {
        "failure_rate": lambda s: not s.ok,
        "repair_rate": lambda s: s.repaired,
        "empty_rate": lambda s: s.empty,
        "slow_rate": lambda s: s.seconds > slow_threshold,
        "costly_rate": lambda s: s.tokens > costly_threshold,
    }

    shifts: list[Shift] = []
    rates: dict[str, tuple[float, float]] = {}

    for metric, hit in predicates.items():
        hits_a = sum(1 for s in baseline if hit(s))
        hits_b = sum(1 for s in recent if hit(s))
        rate_a = hits_a / len(baseline)
        rate_b = hits_b / len(recent)
        rates[metric] = (round(rate_a, 4), round(rate_b, 4))

        p_value = two_proportion_p(hits_a, len(baseline), hits_b, len(recent))

        # Both gates. Significance alone fires on 4.0% -> 4.4% at a few thousand
        # questions; effect size alone fires constantly at small n.
        if p_value < alpha and abs(rate_b - rate_a) >= min_change:
            shifts.append(
                Shift(
                    metric=metric,
                    baseline=round(rate_a, 4),
                    recent=round(rate_b, 4),
                    baseline_n=len(baseline),
                    recent_n=len(recent),
                    p_value=round(p_value, 6),
                )
            )

    # Largest movement first: a report read top-down should start with the thing
    # most worth looking at, not with whichever metric was declared first.
    shifts.sort(key=lambda s: abs(s.change), reverse=True)

    return DriftReport(
        baseline_n=len(baseline),
        recent_n=len(recent),
        shifts=tuple(shifts),
        rates=rates,
    )


def sample_from_entry(entry) -> Sample:
    """Reduce one stored history row to a :class:`Sample`.

    Missing values become the benign reading — a row with no recorded latency
    is not slow, a row with no token count is not costly. Old rows written
    before a column existed must not register as a regression.
    """
    return Sample(
        ok=bool(entry.ok),
        repaired=bool(entry.repairs or 0),
        # Only counted for questions that actually ran. A failed question has no
        # rows for a reason already captured by failure_rate, and counting it
        # here would make one incident move two metrics.
        empty=bool(entry.ok) and (entry.row_count or 0) == 0,
        seconds=float(entry.seconds or 0.0),
        tokens=int(entry.tokens or 0),
    )


def split_history(entries: list, *, recent_n: int) -> tuple[list[Sample], list[Sample]]:
    """Split newest-first history rows into ``(baseline, recent)`` samples.

    Takes the rows in the order the store returns them — newest first — because
    getting that backwards silently compares the baseline against itself and
    reports no drift, which looks exactly like good news.
    """
    samples = [sample_from_entry(entry) for entry in entries]
    return samples[recent_n:], samples[:recent_n]
