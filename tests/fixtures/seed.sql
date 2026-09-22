-- Small, deterministic dataset for execution tests.
--
-- Values are chosen so assertions can be exact: three customers across two
-- regions, four orders with round totals, and one deliberately long text value
-- to exercise cell truncation.

TRUNCATE order_items, invoices, orders, addresses, customers, regions,
         employees, audit_log RESTART IDENTITY CASCADE;

INSERT INTO regions (id, name, iso_code) VALUES
    (1, 'United Kingdom', 'GB'),
    (2, 'Germany',        'DE');

INSERT INTO customers (id, name, email, region_id, created_at) VALUES
    (1, 'Ada Lovelace',    'ada@example.com',    1, '2025-01-15 10:00:00'),
    (2, 'Alan Turing',     'alan@example.com',   1, '2025-02-20 11:30:00'),
    (3, 'Emmy Noether',    'emmy@example.com',   2, '2025-03-05 09:15:00');

INSERT INTO addresses (id, line1, city, postcode) VALUES
    (1, '1 Analytical Way', 'London', 'E1 6AN'),
    (2, 'Hauptstrasse 4',   'Berlin', '10115');

-- Totals: UK = 100 + 250 + 50 = 400, Germany = 300.
INSERT INTO orders (id, customer_id, billing_address_id, shipping_address_id,
                    invoice_id, total, placed_at) VALUES
    (1, 1, 1, 1, NULL, 100.00, '2025-04-01'),
    (2, 1, 1, 1, NULL, 250.00, '2025-05-12'),
    (3, 2, 1, 1, NULL,  50.00, '2025-06-30'),
    (4, 3, 2, 2, NULL, 300.00, '2025-07-04');

INSERT INTO invoices (id, order_id, issued_at, amount) VALUES
    (1, 1, '2025-04-02', 100.00),
    (2, 2, '2025-05-13', 250.00);

UPDATE orders SET invoice_id = 1 WHERE id = 1;
UPDATE orders SET invoice_id = 2 WHERE id = 2;

-- Order 1 has two lines; used to prove a join can inflate an aggregate.
INSERT INTO order_items (order_id, line_no, sku, quantity) VALUES
    (1, 1, 'SKU-A', 2),
    (1, 2, 'SKU-B', 1),
    (2, 1, 'SKU-C', 5),
    (3, 1, 'SKU-A', 1),
    (4, 1, 'SKU-D', 3);

INSERT INTO employees (id, name, manager_id) VALUES
    (1, 'Grace Hopper', NULL),
    (2, 'Margaret Hamilton', 1),
    (3, 'Katherine Johnson', 1);

-- The long value exercises truncation in QueryResult.preview().
INSERT INTO audit_log (id, action, occurred_at) VALUES
    (1, repeat('x', 500), '2025-01-01 00:00:00'),
    (2, 'login',          '2025-01-02 00:00:00');
