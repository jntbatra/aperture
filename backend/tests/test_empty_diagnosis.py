from aperture.graph.empty import diagnose_empty

PG = {"dialect": "postgres"}


def test_empty_table_is_not_retryable(snapshot, profile):
    finding = diagnose_empty("select * from refunds", ["refunds"], snapshot, profile, **PG)
    assert finding and not finding.retryable
    assert "no data at all" in finding.explanation


def test_unknown_literal_lists_observed_values(snapshot, profile):
    finding = diagnose_empty(
        "select count(*) from orders where status = 'COMPLETED'", ["orders"], snapshot, profile, **PG
    )
    assert finding.retryable
    assert "DELIVERED" in finding.explanation


def test_out_of_range_date_reports_real_coverage(snapshot, profile):
    finding = diagnose_empty(
        "select count(*) from orders where \"createdAt\" >= '2019-01-01'",
        ["orders"],
        snapshot,
        profile,
        **PG,
    )
    assert finding.retryable
    assert "2026-05-09" in finding.explanation


def test_date_diagnosis_names_the_filtered_column(snapshot, profile):
    finding = diagnose_empty(
        "select count(*) from orders where \"createdAt\" < '2019-01-01'",
        ["orders"],
        snapshot,
        profile,
        **PG,
    )
    assert "orders.createdAt" in finding.explanation


def test_valid_in_range_query_gets_no_false_diagnosis(snapshot, profile):
    finding = diagnose_empty(
        "select count(*) from orders where status = 'DELIVERED'", ["orders"], snapshot, profile, **PG
    )
    assert not finding
