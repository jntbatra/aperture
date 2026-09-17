import sqlite3

import pytest

from aperture.ingest import infer_type, load_csv, plan_columns, safe_identifier

CSV = """Order ID,Order Date,Region,Units,Unit Price,Refunded,Notes
SO-1,2025-01-05,North,3,19.99,false,
SO-2,2025-02-11,South,12,4.50,true,bulk
SO-3,2025-03-02,North,7,101.25,false,
"""


@pytest.fixture
def csv_file(tmp_path):
    path = tmp_path / "sales.csv"
    path.write_text(CSV)
    return path


def test_header_becomes_safe_identifiers():
    assert safe_identifier("Order ID", fallback="c") == "Order_ID"
    assert safe_identifier("2024 total", fallback="c") == "c_2024_total"
    assert safe_identifier("", fallback="column_3") == "column_3"


@pytest.mark.parametrize(
    "values,expected",
    [
        (["1", "2", "3"], "INTEGER"),
        (["1.5", "2"], "REAL"),
        (["2025-01-05", "2025-02-11"], "DATE"),
        (["2025-01-05 10:00:00"], "TIMESTAMP"),
        (["true", "false"], "BOOLEAN"),
        (["north", "south"], "TEXT"),
        ([], "TEXT"),
        (["1", "n/a", "3"], "INTEGER"),
    ],
)
def test_type_inference(values, expected):
    assert infer_type(values) == expected


def test_mixed_column_falls_back_to_text():
    assert infer_type(["1", "two", "3"]) == "TEXT"


def test_load_csv_creates_a_typed_table(csv_file, monkeypatch, tmp_path):
    monkeypatch.setenv("APERTURE_HOME_DIR", str(tmp_path / "home"))
    from aperture.config import settings

    settings.cache_clear()

    result = load_csv(csv_file, dataset="sales_test")
    assert result.rows == 3
    assert result.database_url.startswith("sqlite:///")

    types = {c.name: c.sql_type for c in result.columns}
    assert types["Order_Date"] == "DATE"
    assert types["Units"] == "INTEGER"
    assert types["Unit_Price"] == "REAL"
    assert types["Refunded"] == "BOOLEAN"

    connection = sqlite3.connect(str(result.path))
    total = connection.execute('SELECT SUM("Units" * "Unit_Price") FROM "sales"').fetchone()[0]
    connection.close()
    assert round(total, 2) == round(3 * 19.99 + 12 * 4.50 + 7 * 101.25, 2)
    settings.cache_clear()


def test_empty_cells_become_null(csv_file, monkeypatch, tmp_path):
    monkeypatch.setenv("APERTURE_HOME_DIR", str(tmp_path / "home"))
    from aperture.config import settings

    settings.cache_clear()
    result = load_csv(csv_file, dataset="nulls_test")
    connection = sqlite3.connect(str(result.path))
    nulls = connection.execute('SELECT count(*) FROM "sales" WHERE "Notes" IS NULL').fetchone()[0]
    connection.close()
    assert nulls == 2
    settings.cache_clear()


def test_duplicate_headers_are_disambiguated():
    plans = plan_columns(["name", "name"], [["a", "b"]])
    assert plans[0].name != plans[1].name
