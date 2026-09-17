import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "benchmarks"))

from bird import results_match  # noqa: E402


def test_identical_results_match():
    assert results_match([(1, "a")], [(1, "a")])


def test_row_order_is_ignored():
    assert results_match([(1,), (2,)], [(2,), (1,)])


def test_duplicate_counts_matter():
    assert not results_match([(1,), (1,)], [(1,)])


def test_different_values_do_not_match():
    assert not results_match([(1,)], [(2,)])


def test_numeric_and_string_forms_compare_equal():
    # SQLite returns ints where the gold query may return text; the benchmark
    # compares rendered values, matching BIRD's own comparison.
    assert results_match([(1,)], [("1",)])
