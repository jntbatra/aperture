"""Tests for turning uploaded files into queryable databases."""

from __future__ import annotations

import pandas as pd
import pytest
from sqlalchemy import create_engine, text

from sqlagent.ingest import IngestError, add_relationship, ingest_file, safe_table_name
from sqlagent.schema.introspect import reflect_schema


def query(dataset, sql: str):
    engine = create_engine(dataset.database_url)
    try:
        with engine.connect() as connection:
            return connection.execute(text(sql)).fetchall()
    finally:
        engine.dispose()


# --------------------------------------------------------------------------
# Table naming
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("customers.csv", "customers"),
        ("Q1 Sales (final).csv", "q1_sales_final"),
        ("weird---name!!.csv", "weird_name"),
        ("2024_report.csv", "t_2024_report"),   # cannot start with a digit
        ("...csv", "data"),                     # nothing usable left
    ],
)
def test_table_names_are_made_legal(raw: str, expected: str):
    assert safe_table_name(raw) == expected


# --------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------


def test_csv_becomes_a_queryable_table(tmp_path):
    source = tmp_path / "customers.csv"
    source.write_text("id,name,city\n1,Ada,London\n2,Alan,Berlin\n")

    dataset = ingest_file(source, data_dir=tmp_path / "data")

    assert dataset.kind == "csv"
    assert dataset.tables == ["customers"]
    assert dataset.row_counts == {"customers": 2}
    assert query(dataset, "SELECT count(*) FROM customers")[0][0] == 2


def test_csv_values_are_preserved(tmp_path):
    source = tmp_path / "c.csv"
    source.write_text("id,name\n1,Ada\n2,Alan\n")

    dataset = ingest_file(source, data_dir=tmp_path / "data")

    assert query(dataset, "SELECT name FROM c ORDER BY id")[0][0] == "Ada"


def test_awkward_column_names_are_normalised(tmp_path):
    """A column called 'Total (£)' cannot be written in SQL without quoting."""
    source = tmp_path / "sales.csv"
    source.write_text("Order ID,Total (£),% Margin\n1,100,0.2\n")

    dataset = ingest_file(source, data_dir=tmp_path / "data")
    columns = [c.name for c in reflect_schema(create_engine(dataset.database_url))["sales"].columns]

    assert columns == ["order_id", "total", "margin"]


def test_duplicate_column_names_are_made_unique(tmp_path):
    source = tmp_path / "d.csv"
    source.write_text("Name,name\na,b\n")

    dataset = ingest_file(source, data_dir=tmp_path / "data")
    columns = [c.name for c in reflect_schema(create_engine(dataset.database_url))["d"].columns]

    assert len(columns) == len(set(columns))


def test_semicolon_separated_csv_is_detected(tmp_path):
    """European spreadsheet exports use semicolons; sniffing handles it."""
    source = tmp_path / "e.csv"
    source.write_text("id;name\n1;Ada\n")

    dataset = ingest_file(source, data_dir=tmp_path / "data")

    assert dataset.row_counts["e"] == 1


def test_csv_note_mentions_inferred_types(tmp_path):
    """A CSV has no types, so the user should know they were guessed."""
    source = tmp_path / "c.csv"
    source.write_text("id,name\n1,Ada\n")

    assert "inferred" in ingest_file(source, data_dir=tmp_path / "data").note


# --------------------------------------------------------------------------
# Excel
# --------------------------------------------------------------------------


def test_each_worksheet_becomes_a_table(tmp_path):
    source = tmp_path / "book.xlsx"
    with pd.ExcelWriter(source) as writer:
        pd.DataFrame({"id": [1, 2], "name": ["Ada", "Alan"]}).to_excel(
            writer, sheet_name="customers", index=False
        )
        pd.DataFrame({"id": [1], "total": [100]}).to_excel(
            writer, sheet_name="orders", index=False
        )

    dataset = ingest_file(source, data_dir=tmp_path / "data")

    assert dataset.kind == "excel"
    assert dataset.tables == ["customers", "orders"]
    assert dataset.row_counts["customers"] == 2


def test_multi_table_upload_warns_that_joins_are_unavailable(tmp_path):
    """A workbook carries no foreign keys, so the tables are islands."""
    source = tmp_path / "book.xlsx"
    with pd.ExcelWriter(source) as writer:
        pd.DataFrame({"id": [1]}).to_excel(writer, sheet_name="a", index=False)
        pd.DataFrame({"id": [1]}).to_excel(writer, sheet_name="b", index=False)

    assert "foreign keys" in ingest_file(source, data_dir=tmp_path / "data").note


def test_empty_sheets_are_skipped(tmp_path):
    source = tmp_path / "book.xlsx"
    with pd.ExcelWriter(source) as writer:
        pd.DataFrame({"id": [1]}).to_excel(writer, sheet_name="real", index=False)
        pd.DataFrame().to_excel(writer, sheet_name="blank", index=False)

    assert ingest_file(source, data_dir=tmp_path / "data").tables == ["real"]


# --------------------------------------------------------------------------
# Rejection
# --------------------------------------------------------------------------


def test_unsupported_type_is_refused_with_a_useful_message(tmp_path):
    source = tmp_path / "photo.png"
    source.write_bytes(b"\x89PNG not a spreadsheet")

    with pytest.raises(IngestError, match="Unsupported file type"):
        ingest_file(source, data_dir=tmp_path / "data")


def test_empty_file_is_refused(tmp_path):
    source = tmp_path / "empty.csv"
    source.write_text("")

    with pytest.raises(IngestError, match="empty"):
        ingest_file(source, data_dir=tmp_path / "data")


def test_malformed_csv_is_refused_not_crashed(tmp_path):
    source = tmp_path / "bad.csv"
    source.write_bytes(b"\xff\xfe\x00\x00 not text at all \xff")

    with pytest.raises(IngestError):
        ingest_file(source, data_dir=tmp_path / "data")


def test_dump_without_a_server_is_refused_clearly(tmp_path):
    source = tmp_path / "backup.sql"
    source.write_text("CREATE TABLE t (id int);")

    with pytest.raises(IngestError, match="needs a server"):
        ingest_file(source, data_dir=tmp_path / "data", postgres_admin_url=None)


# --------------------------------------------------------------------------
# Declared relationships
# --------------------------------------------------------------------------


def test_relationship_can_be_declared_on_an_upload(tmp_path):
    """Nothing is inferred from column names: a wrong join returns wrong rows
    without erroring, which is the worst failure this system has."""
    source = tmp_path / "book.xlsx"
    with pd.ExcelWriter(source) as writer:
        pd.DataFrame({"id": [1, 2]}).to_excel(writer, sheet_name="customers", index=False)
        pd.DataFrame({"id": [1], "customer_id": [1]}).to_excel(
            writer, sheet_name="orders", index=False
        )

    dataset = ingest_file(source, data_dir=tmp_path / "data")
    add_relationship(
        dataset,
        source_table="orders",
        source_column="customer_id",
        target_table="customers",
        target_column="id",
    )

    recorded = query(dataset, "SELECT * FROM _sqlagent_relationships")

    assert recorded == [("orders", "customer_id", "customers", "id")]


# --------------------------------------------------------------------------
# The result is a database the agent can actually read
# --------------------------------------------------------------------------


def test_uploaded_dataset_reflects_like_any_other_database(tmp_path):
    """The whole point: an upload is indistinguishable from a real database
    to everything downstream."""
    source = tmp_path / "customers.csv"
    source.write_text("id,name\n1,Ada\n")

    dataset = ingest_file(source, data_dir=tmp_path / "data")
    snapshot = reflect_schema(create_engine(dataset.database_url))

    assert "customers" in snapshot
    assert snapshot.version
