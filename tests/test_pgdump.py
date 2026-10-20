import sqlite3
from pathlib import Path

import pytest

from sqlagent.ingest import IngestError, ingest_file

DUMP = r"""--
-- PostgreSQL database dump
--
SET statement_timeout = 0;
SELECT pg_catalog.set_config('search_path', '', false);

CREATE FUNCTION public.noop() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  RETURN NEW;
END;
$$;

CREATE TABLE public.customers (
    id integer NOT NULL,
    name character varying(80) NOT NULL,
    active boolean,
    note text
);

CREATE TABLE public.orders (
    id integer NOT NULL,
    customer_id integer,
    total numeric(10,2),
    placed timestamp without time zone,
    tags text[]
);

COPY public.customers (id, name, active, note) FROM stdin;
1	Ada	t	line one\nline two
2	Bo	f	\N
\.

COPY public.orders (id, customer_id, total, placed, tags) FROM stdin;
10	1	12.50	2024-01-02 03:04:05	{a,b}
11	2	\N	\N	\N
\.

INSERT INTO public.customers (id, name, active, note) VALUES (3, 'Cy''s', true, NULL);

ALTER TABLE ONLY public.customers
    ADD CONSTRAINT customers_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT orders_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT orders_customer_fk FOREIGN KEY (customer_id) REFERENCES public.customers(id);
"""


def _load(tmp_path: Path, text: str = DUMP):
    dump = tmp_path / "shop.sql"
    dump.write_text(text)
    return ingest_file(dump, data_dir=tmp_path / "data", original_name="shop.sql")


def test_plain_dump_becomes_sqlite_without_a_server(tmp_path):
    dataset = _load(tmp_path)
    assert dataset.kind == "pg_dump"
    assert dataset.tables == ["customers", "orders"]
    assert dataset.row_counts == {"customers": 3, "orders": 2}

    db = sqlite3.connect(dataset.database_url.removeprefix("sqlite:///"))
    assert db.execute("select name, active, note from customers order by id").fetchall() == [
        ("Ada", 1, "line one\nline two"),
        ("Bo", 0, None),
        ("Cy's", 1, None),
    ]
    assert db.execute("select total, placed from orders where id = 10").fetchone() == (
        12.5,
        "2024-01-02 03:04:05",
    )


def test_keys_survive_so_the_schema_graph_can_join(tmp_path):
    dataset = _load(tmp_path)
    db = sqlite3.connect(dataset.database_url.removeprefix("sqlite:///"))
    fks = db.execute("pragma foreign_key_list(orders)").fetchall()
    assert [(row[2], row[3], row[4]) for row in fks] == [("customers", "customer_id", "id")]
    assert [r[1] for r in db.execute("pragma table_info(customers)") if r[5]] == ["id"]


def test_a_file_with_no_tables_is_refused(tmp_path):
    with pytest.raises(IngestError, match="No CREATE TABLE"):
        _load(tmp_path, "SELECT 1;\n")


def test_custom_format_dump_is_refused_with_advice(tmp_path):
    dump = tmp_path / "x.dump"
    dump.write_bytes(b"PGDMP" + b"\x00" * 20)
    with pytest.raises(IngestError, match="plain SQL"):
        ingest_file(dump, data_dir=tmp_path / "data", original_name="x.dump")
