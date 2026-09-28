"""Per-column documentation: loading it, cleaning it, and putting it in a prompt.

A column called `EdOpsCode` holding `'TRAD'` is opaque, and neither its name
nor a sample value says what it means. BIRD ships a written description for
77% of its columns and this project referenced none of them; both systems
above us on the corrected leaderboard feed exactly this.
"""

from __future__ import annotations

import networkx as nx

from sqlagent.prompts import render_schema
from sqlagent.schema.docs import ColumnDoc, from_csv_directory
from sqlagent.schema.introspect import Column, SchemaSnapshot, Table, compute_version


def _write(tmp_path, table, rows):
    directory = tmp_path / "database_description"
    directory.mkdir(exist_ok=True)
    header = "original_column_name,column_name,column_description,value_description\n"
    (directory / f"{table}.csv").write_text(header + "".join(rows), encoding="utf-8")
    return directory


def test_a_description_and_its_value_list_are_both_kept(tmp_path):
    """The value list is the half a WHERE clause actually needs."""
    row = 'availability,,A list of printing types,"""arena"", ""paper"""\n'
    d = _write(tmp_path, "cards", [row])

    docs = from_csv_directory(d)

    doc = docs[("cards", "availability")]
    assert doc.description == "A list of printing types"
    assert '"arena", "paper"' in doc.values
    assert "—" in doc.render()


def test_a_description_that_just_repeats_the_column_name_is_dropped(tmp_path):
    """BIRD has many of these. Keeping them doubles the schema's token cost
    and tells the model nothing it cannot already see."""
    d = _write(tmp_path, "t", ["CDSCode,,CDSCode,\n", "County Code,,County  code,\n"])

    assert from_csv_directory(d) == {}


def test_a_column_with_only_a_value_description_still_counts(tmp_path):
    d = _write(tmp_path, "t", ["Charter,,Charter,0: N; 1: Y\n"])

    docs = from_csv_directory(d)

    assert docs[("t", "Charter")].description == ""
    assert docs[("t", "Charter")].render() == "0: N; 1: Y"


def test_the_commonsense_evidence_preamble_is_stripped(tmp_path):
    """It prefixes about a third of BIRD's value descriptions and says
    nothing the position of the text does not already say."""
    d = _write(tmp_path, "t", ["c,,desc,commonsense evidence: higher means better\n"])

    assert from_csv_directory(d)[("t", "c")].values == "higher means better"


def test_a_long_description_is_truncated_rather_than_dropped(tmp_path):
    d = _write(tmp_path, "t", [f"c,,{'word ' * 200},\n"])

    rendered = from_csv_directory(d)[("t", "c")].description

    assert len(rendered) <= 180
    assert rendered.endswith("…")


def test_a_byte_order_mark_does_not_break_the_first_column(tmp_path):
    """BIRD's CSVs carry a BOM. Read as plain utf-8 it becomes part of the
    first header name, every lookup misses, and the loader silently returns
    nothing at all."""
    directory = tmp_path / "database_description"
    directory.mkdir()
    (directory / "t.csv").write_text(
        "﻿original_column_name,column_name,column_description,value_description\n"
        "c,,what it means,\n",
        encoding="utf-8",
    )

    assert from_csv_directory(directory)[("t", "c")].description == "what it means"


def test_a_missing_directory_is_not_an_error(tmp_path):
    assert from_csv_directory(tmp_path / "nope") == {}


def test_a_malformed_file_does_not_lose_the_good_ones(tmp_path):
    d = _write(tmp_path, "good", ["c,,what it means,\n"])
    (d / "bad.csv").write_bytes(b"\xff\xfe not, valid; utf8 \x00\x00")

    assert from_csv_directory(d)[("good", "c")].description == "what it means"


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _snapshot():
    tables = {
        "orders": Table(
            name="orders",
            columns=(
                Column(name="id", type="INTEGER", nullable=False, primary_key=True),
                Column(name="status", type="TEXT", nullable=True, primary_key=False),
            ),
            foreign_keys=(),
        )
    }
    return SchemaSnapshot(tables=tables, version=compute_version(tables))


def test_a_description_sits_on_its_own_column_line():
    """Three sections away from the column it describes is a description the
    model has to join up for itself."""
    snapshot = _snapshot()
    text = render_schema(
        snapshot,
        nx.DiGraph(),
        ["orders"],
        docs={("orders", "status"): ColumnDoc(values="'new', 'shipped'")},
        dialect="sqlite",
    )

    line = next(ln for ln in text.splitlines() if "status" in ln)
    assert "'new', 'shipped'" in line
    assert "--" in line


def test_an_undocumented_schema_renders_exactly_as_before():
    """The dense single-line form is kept when there is nothing to attach, so
    turning this off costs nothing and the diff of turning it on is readable."""
    snapshot = _snapshot()

    without = render_schema(snapshot, nx.DiGraph(), ["orders"], dialect="sqlite")
    empty = render_schema(snapshot, nx.DiGraph(), ["orders"], docs={}, dialect="sqlite")

    assert without == empty
    assert "orders(id INTEGER PRIMARY KEY NOT NULL, status TEXT)" in without


def test_documenting_one_column_does_not_drop_the_others():
    snapshot = _snapshot()
    text = render_schema(
        snapshot,
        nx.DiGraph(),
        ["orders"],
        docs={("orders", "status"): ColumnDoc(description="how far along")},
        dialect="sqlite",
    )

    assert "id INTEGER PRIMARY KEY NOT NULL" in text
    assert "status TEXT" in text
    assert text.count("--") == 1
