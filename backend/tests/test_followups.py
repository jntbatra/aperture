import datetime as dt

from aperture.followups import suggest


def _suggest(**kwargs):
    base = dict(
        question="total revenue by region",
        columns=["region", "revenue"],
        rows=[["North", 10], ["South", 20]],
        linked_tables=["orders"],
        status="answered",
        has_caveat=False,
    )
    base.update(kwargs)
    return suggest(**base)


def test_offers_a_breakdown_by_a_real_low_cardinality_column(snapshot, profile):
    texts = [s.text for s in _suggest(snapshot=snapshot, profile=profile)]
    assert any("status" in t for t in texts)


def test_never_offers_a_column_that_does_not_exist(snapshot, profile):
    reasons = " ".join(s.reason for s in _suggest(snapshot=snapshot, profile=profile))
    available = {c.name for t in snapshot.tables.values() for c in t.columns}
    for token in reasons.split():
        if "." in token:
            assert token.split(".")[1].rstrip(",") in available


def test_offers_a_time_breakdown_when_a_date_column_exists(snapshot, profile):
    texts = [s.text for s in _suggest(snapshot=snapshot, profile=profile)]
    assert any("over time" in t for t in texts)


def test_time_series_result_gets_a_change_question(snapshot, profile):
    texts = [
        s.text
        for s in _suggest(
            columns=["month", "orders"],
            rows=[[dt.datetime(2026, 5, 1), 5], [dt.datetime(2026, 6, 1), 9]],
            snapshot=snapshot,
            profile=profile,
        )
    ]
    assert any("biggest change" in t for t in texts)


def test_caveat_suggests_recalculating(snapshot, profile):
    texts = [s.text for s in _suggest(has_caveat=True, snapshot=snapshot, profile=profile)]
    assert any("each row only once" in t for t in texts)


def test_empty_result_asks_about_coverage(snapshot, profile):
    texts = [s.text for s in _suggest(status="empty", rows=[], snapshot=snapshot, profile=profile)]
    assert any("date range" in t for t in texts)


def test_suggestions_are_capped_and_unique(snapshot, profile):
    suggestions = _suggest(has_caveat=True, snapshot=snapshot, profile=profile)
    texts = [s.text for s in suggestions]
    assert len(texts) <= 4
    assert len(texts) == len(set(texts))
