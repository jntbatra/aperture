from aperture.semantic import SemanticLayer
from aperture.semantic.layer import Metric

LAYER = SemanticLayer(
    metrics=[
        Metric(
            name="revenue",
            tables=["orders"],
            expression='SUM(orders."totalAmount") / 100.0',
            filter="orders.status = 'DELIVERED'",
            synonyms=["revenue", "sales"],
        ),
        Metric(
            name="rider_payout",
            tables=["rider_settlements"],
            expression='SUM(rider_settlements."netPaise") / 100.0',
            synonyms=["payout"],
        ),
    ],
    conventions=["Money is stored in paise; divide by 100."],
)

TIFFINWALA = {
    "orders": {"id", "totalAmount", "status", "createdAt"},
    "rider_settlements": {"id", "netPaise"},
}
# A different database that happens to have a table called "orders".
WORKBOOK = {"orders": {"Order_ID", "Amount", "Paid"}, "customers": {"Customer", "City"}}


def test_metrics_match_on_synonyms():
    assert [m.name for m in LAYER.match("what were sales last month")] == ["revenue"]


def test_definitions_apply_to_a_matching_schema():
    scoped = LAYER.for_schema(TIFFINWALA)
    assert {m.name for m in scoped.metrics} == {"revenue", "rider_payout"}


def test_table_name_alone_is_not_enough():
    # An uploaded workbook with an "orders" sheet must not inherit definitions
    # written for a different database -- a paise rule would divide it by 100.
    scoped = LAYER.for_schema(WORKBOOK)
    assert scoped.metrics == []
    assert scoped.conventions == []


def test_numeric_literals_are_not_mistaken_for_columns():
    # "/ 100.0" must not parse as a reference to column "0" of table "100".
    assert "100.0" not in " ".join(LAYER.metrics[0].referenced_columns)
    assert LAYER.metrics[0].referenced_columns == {"orders.totalAmount", "orders.status"}


def test_conventions_survive_when_any_metric_applies():
    assert LAYER.for_schema(TIFFINWALA).conventions


def test_prompt_section_is_empty_without_a_match():
    assert LAYER.for_schema(WORKBOOK).prompt_section("revenue") == ""
