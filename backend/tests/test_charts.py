import datetime as dt

from aperture.charts import build_spec, validate_spec


def test_time_series_becomes_a_line():
    spec = build_spec(["month", "orders"], [[dt.datetime(2026, 5, 1), 331], [dt.datetime(2026, 6, 1), 273]])
    assert spec["mark"]["type"] == "line"
    assert spec["encoding"]["x"]["type"] == "temporal"


def test_categories_become_a_bar():
    spec = build_spec(["item", "orders"], [["Momos", 300], ["Dal", 187]])
    assert spec["mark"] == "bar"


def test_single_number_becomes_a_metric_tile():
    spec = build_spec(["total"], [[2999]])
    assert spec["aperture"]["kind"] == "metric"
    assert spec["aperture"]["value"] == 2999


def test_two_measures_become_a_scatter():
    spec = build_spec(["a", "b"], [[1.0, 2.0], [3.0, 4.0]])
    assert spec["mark"]["type"] == "point"


def test_no_measure_means_no_chart():
    assert build_spec(["id", "name"], [["x", "y"]]) is None


def test_empty_result_means_no_chart():
    assert build_spec(["a"], []) is None


def test_datetimes_are_serialised():
    spec = build_spec(["month", "n"], [[dt.datetime(2026, 5, 1), 1]])
    assert spec["data"]["values"][0]["month"] == "2026-05-01T00:00:00"


def test_bar_charts_are_trimmed_to_a_readable_number():
    rows = [[f"item{i}", i] for i in range(40)]
    spec = build_spec(["item", "n"], rows)
    assert len(spec["data"]["values"]) == 12
    assert "top 12" in spec["title"]


def test_spec_referencing_unknown_column_is_rejected():
    ok, reason = validate_spec({"mark": "bar", "encoding": {"x": {"field": "nope"}}}, ["a"])
    assert not ok and "nope" in reason


def test_valid_spec_passes():
    ok, _ = validate_spec({"mark": "bar", "encoding": {"x": {"field": "a"}}}, ["a"])
    assert ok
