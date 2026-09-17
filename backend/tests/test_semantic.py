from aperture.semantic import SemanticLayer
from aperture.semantic.layer import Metric

LAYER = SemanticLayer(
    metrics=[
        Metric(name="revenue", tables=["orders"], synonyms=["revenue", "sales"]),
        Metric(name="rider_payout", tables=["rider_settlements"], synonyms=["payout"]),
    ],
    conventions=["Money is stored in paise; divide by 100."],
)


def test_metrics_match_on_synonyms():
    assert [m.name for m in LAYER.match("what were sales last month")] == ["revenue"]


def test_definitions_apply_only_to_a_matching_schema():
    scoped = LAYER.for_tables({"orders"})
    assert [m.name for m in scoped.metrics] == ["revenue"]


def test_foreign_schema_gets_no_definitions_and_no_conventions():
    # A paise convention applied to an unrelated CSV divides every amount by 100.
    scoped = LAYER.for_tables({"sales"})
    assert scoped.metrics == []
    assert scoped.conventions == []


def test_conventions_survive_when_any_metric_applies():
    assert LAYER.for_tables({"orders"}).conventions


def test_prompt_section_is_empty_without_a_match():
    assert LAYER.for_tables({"sales"}).prompt_section("revenue") == ""
