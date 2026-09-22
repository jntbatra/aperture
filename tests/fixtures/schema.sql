-- Test schema, deliberately built around the cases that break naive
-- schema-graph code. Every table here exists to exercise one hazard.
--
-- Shape:
--
--   regions <- customers <- orders -> invoices
--                              |  ^      |
--                              |  +------+   (mutual reference: a cycle)
--                              |
--                              +-> addresses  (TWICE: billing + shipping)
--
--   employees -> employees        (self-reference)
--   order_items -> orders         (composite foreign key)
--   audit_log                     (isolated: no foreign keys at all)

DROP TABLE IF EXISTS order_items, invoices, orders, addresses, customers,
    regions, employees, audit_log CASCADE;

-- A plain parent table. Nothing references it except customers.
CREATE TABLE regions (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    iso_code    VARCHAR(2)
);

-- Standard child -> parent relationship.
CREATE TABLE customers (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    email       TEXT,
    region_id   INTEGER REFERENCES regions (id),
    created_at  TIMESTAMP
);

-- Referenced TWICE from orders. A DiGraph stores one edge per node pair, so
-- this proves both foreign keys survive on the single orders->addresses edge
-- instead of one overwriting the other.
CREATE TABLE addresses (
    id          INTEGER PRIMARY KEY,
    line1       TEXT,
    city        TEXT,
    postcode    TEXT
);

CREATE TABLE orders (
    id                  INTEGER PRIMARY KEY,
    customer_id         INTEGER NOT NULL REFERENCES customers (id),
    billing_address_id  INTEGER REFERENCES addresses (id),
    shipping_address_id INTEGER REFERENCES addresses (id),
    -- Points at invoices, while invoices points back at orders. Nullable so
    -- rows can actually be inserted despite the circular dependency.
    invoice_id          INTEGER,
    total               NUMERIC(12, 2),
    placed_at           DATE
);

CREATE TABLE invoices (
    id          INTEGER PRIMARY KEY,
    order_id    INTEGER NOT NULL REFERENCES orders (id),
    issued_at   DATE,
    amount      NUMERIC(12, 2)
);

-- Completes the cycle: orders <-> invoices.
ALTER TABLE orders
    ADD CONSTRAINT orders_invoice_fk FOREIGN KEY (invoice_id) REFERENCES invoices (id);

-- Composite primary key AND a composite foreign key. Splitting this into two
-- single-column links would let a generated query join on half the key, which
-- returns wrong rows without raising any database error.
CREATE TABLE order_items (
    order_id    INTEGER NOT NULL,
    line_no     INTEGER NOT NULL,
    sku         TEXT NOT NULL,
    quantity    INTEGER NOT NULL,
    PRIMARY KEY (order_id, line_no),
    CONSTRAINT order_items_order_fk
        FOREIGN KEY (order_id) REFERENCES orders (id)
);

-- Self-reference: a table that is its own parent. Traversal must not report
-- this table as its own neighbour, and must not loop.
CREATE TABLE employees (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    manager_id  INTEGER REFERENCES employees (id)
);

-- Completely isolated. Must still appear as a node: plenty of real questions
-- concern a single standalone table.
CREATE TABLE audit_log (
    id          INTEGER PRIMARY KEY,
    action      TEXT,
    occurred_at TIMESTAMP
);
