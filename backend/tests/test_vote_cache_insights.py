import datetime as dt

from aperture.cache import key_for, normalise
from aperture.insights import find_insights
from aperture.vote import Candidate, choose, fingerprint


def _candidate(sql, rows, columns=("x",)):
    return Candidate(
        sql=sql,
        ok=True,
        columns=list(columns),
        rows=rows,
        row_count=len(rows),
        fingerprint=fingerprint(list(columns), rows),
    )


def test_majority_result_wins():
    vote = choose([_candidate("a", [[1]]), _candidate("b", [[1]]), _candidate("c", [[2]])])
    assert vote.winner.sql == "a"
    assert vote.agreement == 2


def test_agreement_is_on_results_not_query_text():
    # Different SQL, same rows: that is agreement, and it is the useful signal.
    left = _candidate("SELECT count(*) FROM t", [[5]])
    right = _candidate("SELECT COUNT(1) FROM t", [[5]])
    assert choose([left, right]).agreement == 2


def test_row_order_does_not_split_the_vote():
    assert fingerprint(["x"], [[1], [2]]) == fingerprint(["x"], [[2], [1]])


def test_empty_result_loses_to_a_populated_one():
    assert choose([_candidate("empty", []), _candidate("rows", [[7]])]).winner.sql == "rows"


def test_no_runnable_candidate_yields_no_winner():
    assert choose([Candidate(sql="broken", error="boom")]).winner is None


def test_cache_key_ignores_casing_and_punctuation():
    assert key_for("What is Revenue?", "db", "v1") == key_for("what is revenue", "db", "v1")


def test_cache_key_changes_with_the_schema():
    assert key_for("q", "db", "v1") != key_for("q", "db", "v2")


def test_cache_key_is_per_dataset():
    assert key_for("q", "db-a", "v1") != key_for("q", "db-b", "v1")


def test_normalise_strips_trailing_punctuation():
    assert normalise("  How many ORDERS?? ") == "how many orders"


def test_concentration_is_reported():
    found = find_insights(["region", "revenue"], [["West", 900], ["East", 60], ["North", 40]])
    assert any(i.kind == "concentration" for i in found)


def test_consistent_trend_is_reported():
    found = find_insights(
        ["month", "orders"],
        [[dt.date(2026, 5, 1), 100], [dt.date(2026, 6, 1), 160], [dt.date(2026, 7, 1), 240]],
    )
    assert any(i.kind == "trend" for i in found)


def test_flat_or_tiny_results_produce_nothing():
    assert find_insights(["x", "y"], [["a", 10], ["b", 10]]) == []
    assert find_insights(["x", "y"], [["a", 10]]) == []


def test_results_without_a_measure_produce_nothing():
    assert find_insights(["name", "city"], [["a", "Delhi"], ["b", "Mumbai"]]) == []
