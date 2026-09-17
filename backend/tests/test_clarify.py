import pytest

from aperture.clarify import needs_clarification
from aperture.db.introspect import ColumnInfo, SchemaSnapshot, TableInfo
from aperture.db.profile import ColumnProfile, DatabaseProfile, TableProfile
from aperture.schema.linker import LinkedSchema


@pytest.fixture
def shop():
    snap = SchemaSnapshot(dialect="postgresql")
    snap.tables["orders"] = TableInfo(
        name="orders",
        columns=[
            ColumnInfo(name="id", data_type="text", is_pk=True),
            ColumnInfo(name="createdAt", data_type="timestamp"),
            ColumnInfo(name="deliveredAt", data_type="timestamp"),
            ColumnInfo(name="deletedAt", data_type="timestamp"),
            ColumnInfo(name="totalAmount", data_type="integer"),
            ColumnInfo(name="deliveryFee", data_type="integer"),
        ],
    )
    profile = DatabaseProfile()
    profile.tables["orders"] = TableProfile(table="orders", exact_rows=100)
    profile.tables["orders"].columns["deletedAt"] = ColumnProfile(
        table="orders", column="deletedAt", null_fraction=0.99
    )
    linked = LinkedSchema(tables=["orders"], ddl="", seeds=["orders"])
    return snap, profile, linked


def test_relative_date_with_two_event_dates_is_ambiguous(shop):
    snap, profile, linked = shop
    finding = needs_clarification("how many orders last month?", linked, snap, profile)
    assert finding is not None
    assert set(finding.options) == {"createdAt", "deliveredAt"}


def test_attribute_timestamps_are_not_offered(shop):
    snap, profile, linked = shop
    finding = needs_clarification("how many orders last month?", linked, snap, profile)
    assert "deletedAt" not in finding.options


def test_naming_the_date_removes_the_ambiguity(shop):
    snap, profile, linked = shop
    assert needs_clarification("orders by createdAt last month", linked, snap, profile) is None


def test_absolute_dates_are_not_ambiguous(shop):
    snap, profile, linked = shop
    assert needs_clarification("orders between 2026-01-01 and 2026-02-01", linked, snap, profile) is None


def test_ranking_without_a_measure_is_ambiguous(shop):
    snap, profile, linked = shop
    finding = needs_clarification("which kitchen is best?", linked, snap, profile)
    assert finding is not None
    assert "number of rows" in finding.options


def test_ranking_that_names_its_measure_is_not(shop):
    snap, profile, linked = shop
    assert needs_clarification("kitchen with the highest revenue", linked, snap, profile) is None


def test_a_matching_metric_definition_settles_it(shop):
    snap, profile, linked = shop
    finding = needs_clarification(
        "which kitchen is best?", linked, snap, profile, metric_names={"revenue"}
    )
    assert finding is None


def test_plain_questions_are_left_alone(shop):
    snap, profile, linked = shop
    assert needs_clarification("how many orders are there?", linked, snap, profile) is None
