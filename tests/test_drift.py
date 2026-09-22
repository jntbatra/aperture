"""Tests for drift detection.

The claim this module is allowed to make is narrow: one set of questions was
answered measurably differently from another, with a p-value. It does not
measure whether an answer was right — there is no label — and these tests exist
partly to keep it from drifting into pretending otherwise.
"""

from __future__ import annotations

from sqlagent.drift import (
    ALPHA,
    MIN_CHANGE,
    Sample,
    Shift,
    detect_drift,
    percentile,
    sample_from_entry,
    split_history,
    two_proportion_p,
)


def samples(count: int, **kwargs) -> list[Sample]:
    base = {"ok": True, "repaired": False, "empty": False, "seconds": 1.0, "tokens": 1000}
    return [Sample(**{**base, **kwargs}) for _ in range(count)]


def mixed(good: int, bad: int, **bad_kwargs) -> list[Sample]:
    return samples(good) + samples(bad, **bad_kwargs)


# --------------------------------------------------------------------------
# The test itself
# --------------------------------------------------------------------------


def test_identical_rates_give_no_evidence():
    assert two_proportion_p(10, 100, 10, 100) == 1.0


def test_a_large_difference_is_significant():
    """10% against 30% at n=100 each is z = 3.54, two-sided p ~ 0.0004."""
    p = two_proportion_p(10, 100, 30, 100)

    assert 0.0003 < p < 0.0005


def test_the_same_difference_at_small_n_is_not():
    """3 against 1 out of 10 is the same twenty-point gap and proves nothing."""
    assert two_proportion_p(1, 10, 3, 10) > 0.05


def test_an_empty_side_is_not_evidence_of_anything():
    assert two_proportion_p(0, 0, 5, 50) == 1.0


def test_two_zero_rates_do_not_divide_by_zero():
    """Pooled variance is zero and the z-score undefined. A degenerate metric
    must not take the whole report down."""
    assert two_proportion_p(0, 50, 0, 50) == 1.0


def test_two_perfect_rates_do_not_divide_by_zero():
    assert two_proportion_p(50, 50, 50, 50) == 1.0


def test_the_test_is_two_sided():
    """An improvement is drift too, and must be detectable."""
    assert two_proportion_p(30, 100, 10, 100) == two_proportion_p(10, 100, 30, 100)


# --------------------------------------------------------------------------
# Percentiles
# --------------------------------------------------------------------------


def test_the_ninetieth_percentile_of_ten_values_is_the_ninth():
    assert percentile([float(i) for i in range(1, 11)], 0.9) == 9.0


def test_an_empty_sample_has_no_percentile():
    assert percentile([], 0.9) == 0.0


def test_a_single_value_is_its_own_percentile():
    assert percentile([4.0], 0.9) == 4.0


# --------------------------------------------------------------------------
# Not enough data is not "no drift"
# --------------------------------------------------------------------------


def test_too_little_history_says_so_rather_than_reporting_calm():
    report = detect_drift(samples(5), samples(5))

    assert not report.enough_data
    assert report.shifts == ()


def test_a_short_recent_period_is_also_not_enough():
    report = detect_drift(samples(100), samples(3))

    assert not report.enough_data


def test_not_enough_data_renders_as_such():
    assert "Not enough history" in detect_drift(samples(2), samples(2)).render()


def test_enough_data_and_nothing_moved_is_a_different_message():
    text = detect_drift(samples(100), samples(100)).render()

    assert "No significant change" in text


# --------------------------------------------------------------------------
# Detecting a real change
# --------------------------------------------------------------------------


def test_a_jump_in_failures_is_caught():
    report = detect_drift(mixed(95, 5, ok=False), mixed(60, 40, ok=False))

    assert report.drifted
    assert [s.metric for s in report.shifts] == ["failure_rate"]


def test_the_shift_carries_both_rates_and_the_sample_sizes():
    report = detect_drift(mixed(95, 5, ok=False), mixed(60, 40, ok=False))
    shift = report.shifts[0]

    assert shift.baseline == 0.05
    assert shift.recent == 0.4
    assert shift.baseline_n == 100
    assert shift.recent_n == 100


def test_a_jump_in_repairs_is_caught():
    report = detect_drift(mixed(95, 5, repaired=True), mixed(50, 50, repaired=True))

    assert [s.metric for s in report.shifts] == ["repair_rate"]


def test_a_jump_in_empty_results_is_caught():
    """A query that runs and returns nothing is usually a misunderstood
    question, not data that does not exist."""
    report = detect_drift(mixed(95, 5, empty=True), mixed(50, 50, empty=True))

    assert [s.metric for s in report.shifts] == ["empty_rate"]


def test_slowing_down_is_caught():
    """Latency is continuous; it enters the test as "how often is it worse than
    this deployment's own 90th percentile"."""
    report = detect_drift(samples(100, seconds=2.0), samples(100, seconds=30.0))

    assert "slow_rate" in [s.metric for s in report.shifts]


def test_getting_more_expensive_is_caught():
    report = detect_drift(samples(100, tokens=1000), samples(100, tokens=9000))

    assert "costly_rate" in [s.metric for s in report.shifts]


def test_several_metrics_can_move_at_once():
    report = detect_drift(
        mixed(95, 5, ok=False),
        mixed(50, 50, ok=False, repaired=True),
    )

    assert {"failure_rate", "repair_rate"} <= {s.metric for s in report.shifts}


def test_the_largest_movement_is_reported_first():
    """A report read top-down should start with the thing most worth looking
    at, not with whichever metric was declared first."""
    report = detect_drift(
        samples(200),
        samples(120) + samples(80, ok=False, repaired=True),
    )

    changes = [abs(s.change) for s in report.shifts]
    assert changes == sorted(changes, reverse=True)


# --------------------------------------------------------------------------
# Not crying wolf
# --------------------------------------------------------------------------


def test_an_improvement_is_reported_but_not_as_worse():
    report = detect_drift(mixed(50, 50, ok=False), mixed(95, 5, ok=False))

    assert report.drifted
    assert not report.shifts[0].worse
    assert "down" in report.shifts[0].render()


def test_a_significant_but_tiny_change_is_not_reported():
    """At a few thousand questions, 2% against 4% is significant and not worth
    anyone's attention. An alert that fires on it gets switched off."""
    baseline = mixed(1960, 40, ok=False)
    recent = mixed(1920, 80, ok=False)

    report = detect_drift(baseline, recent)

    assert two_proportion_p(40, 2000, 80, 2000) < ALPHA
    assert report.shifts == ()


def test_a_large_but_unproven_change_is_not_reported():
    """Thirty questions is enough to look at and not enough to conclude from."""
    report = detect_drift(mixed(27, 3, ok=False), mixed(22, 8, ok=False))

    assert abs(8 / 30 - 3 / 30) > MIN_CHANGE
    assert report.shifts == ()


def test_identical_periods_report_nothing():
    assert not detect_drift(mixed(80, 20, ok=False), mixed(80, 20, ok=False)).drifted


def test_metrics_that_held_are_still_reported():
    """"Latency is up but the failure rate held" is a different situation from
    "both moved", and only one of them is a correctness concern."""
    report = detect_drift(samples(100, seconds=2.0), samples(100, seconds=30.0))

    assert report.rates["failure_rate"] == (0.0, 0.0)
    assert set(report.rates) == {
        "failure_rate",
        "repair_rate",
        "empty_rate",
        "slow_rate",
        "costly_rate",
    }


# --------------------------------------------------------------------------
# Reading the history table
# --------------------------------------------------------------------------


class Row:
    def __init__(self, **kwargs):
        defaults = {"ok": 1, "repairs": 0, "row_count": 3, "seconds": 1.0, "tokens": 500}
        for key, value in {**defaults, **kwargs}.items():
            setattr(self, key, value)


def test_a_stored_row_becomes_a_sample():
    sample = sample_from_entry(Row(ok=1, repairs=2, row_count=0, seconds=4.0, tokens=900))

    assert sample == Sample(ok=True, repaired=True, empty=True, seconds=4.0, tokens=900)


def test_a_failed_question_is_not_also_counted_as_empty():
    """It has no rows for a reason failure_rate already captures. Counting it
    twice makes one incident move two metrics."""
    assert not sample_from_entry(Row(ok=0, row_count=0)).empty


def test_missing_columns_read_as_benign():
    """Rows written before a column existed must not register as a regression."""
    sample = sample_from_entry(Row(seconds=None, tokens=None, repairs=None))

    assert sample.seconds == 0.0
    assert sample.tokens == 0
    assert not sample.repaired


def test_history_splits_newest_first():
    """The store returns newest first. Getting this backwards compares the
    baseline against itself and reports calm, which looks like good news."""
    rows = [Row(ok=0) for _ in range(10)] + [Row(ok=1) for _ in range(20)]

    baseline, recent = split_history(rows, recent_n=10)

    assert all(s.ok for s in baseline)
    assert not any(s.ok for s in recent)


def test_splitting_more_than_there_is_leaves_an_empty_baseline():
    baseline, recent = split_history([Row() for _ in range(5)], recent_n=10)

    assert baseline == []
    assert len(recent) == 5


def test_a_shift_renders_its_evidence():
    text = Shift("failure_rate", 0.05, 0.4, 100, 100, 0.0001).render()

    for fragment in ("failure_rate", "up", "5.0%", "40.0%", "n=100", "p="):
        assert fragment in text
