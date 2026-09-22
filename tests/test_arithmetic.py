"""Tests for the join fan-out check.

The failure being caught produces no error at all. Joining orders to
order_items and summing the order total counts each total once per line item —
valid SQL, real foreign keys, a query that runs, and a number that is wrong.
"""

from __future__ import annotations

from sqlagent.guards.arithmetic import find_inflated_aggregates, one_to_many_map
from sqlagent.schema.introspect import Column, ForeignKey, SchemaSnapshot, Table

# customers <- orders <- order_items. Each arrow is "many of these per one of
# those", which is exactly the duplication the check looks for.
ONE_TO_MANY = {
    "customers": {"orders"},
    "orders": {"order_items"},
}


def find(sql: str, mapping: dict | None = None):
    return find_inflated_aggregates(
        sql, dialect="postgres", one_to_many=mapping or ONE_TO_MANY
    )


# --------------------------------------------------------------------------
# The failure
# --------------------------------------------------------------------------


def test_a_sum_multiplied_by_a_child_join_is_flagged():
    """The canonical fan-out: each order total counted once per line item."""
    findings = find(
        "SELECT c.name, SUM(o.total) FROM customers c "
        "JOIN orders o ON o.customer_id = c.id "
        "JOIN order_items i ON i.order_id = o.id "
        "GROUP BY c.name"
    )

    assert len(findings) == 1
    assert findings[0].table == "orders"
    assert findings[0].multiplied_by == "order_items"


def test_the_finding_explains_itself():
    """It is shown to a user, so it has to say what went wrong, not just that
    something did."""
    findings = find(
        "SELECT SUM(o.total) FROM orders o JOIN order_items i ON i.order_id = o.id"
    )

    rendered = findings[0].render()
    assert "orders" in rendered
    assert "order_items" in rendered


def test_avg_is_flagged_too():
    """An average over duplicated rows is weighted by the duplication."""
    assert find(
        "SELECT AVG(o.total) FROM orders o JOIN order_items i ON i.order_id = o.id"
    )


def test_an_alias_is_resolved_to_its_table():
    """`SUM(o.total)` has to be traced back to `orders` for the check to work."""
    findings = find(
        "SELECT SUM(anything.total) FROM orders AS anything "
        "JOIN order_items AS lines ON lines.order_id = anything.id"
    )

    assert findings[0].table == "orders"


def test_a_two_hop_fan_out_is_flagged():
    """Summing a customer column while joining through to orders."""
    findings = find(
        "SELECT SUM(c.credit) FROM customers c JOIN orders o ON o.customer_id = c.id"
    )

    assert findings[0].multiplied_by == "orders"


# --------------------------------------------------------------------------
# Not crying wolf
# --------------------------------------------------------------------------


def test_summing_the_child_itself_is_fine():
    """`SUM(i.price)` over order_items is the correct shape, not a fan-out."""
    assert find(
        "SELECT SUM(i.price) FROM orders o JOIN order_items i ON i.order_id = o.id"
    ) == []


def test_a_single_table_aggregate_is_fine():
    assert find("SELECT SUM(o.total) FROM orders o") == []


def test_a_join_that_does_not_fan_out_is_fine():
    """Joining a parent adds no rows — many orders per customer, not the reverse."""
    assert find(
        "SELECT SUM(o.total) FROM orders o JOIN customers c ON o.customer_id = c.id"
    ) == []


def test_count_is_not_flagged():
    """COUNT(*) over a fan-out is usually the intended question, and
    COUNT(DISTINCT x) cannot be inflated at all."""
    assert find(
        "SELECT COUNT(*) FROM orders o JOIN order_items i ON i.order_id = o.id"
    ) == []


def test_min_and_max_are_not_flagged():
    """Idempotent under duplication."""
    assert find(
        "SELECT MAX(o.total) FROM orders o JOIN order_items i ON i.order_id = o.id"
    ) == []


def test_an_unqualified_column_is_skipped_rather_than_guessed():
    """Attributing a bare column to a table needs proper resolution; guessing
    would produce warnings on correct queries."""
    assert find("SELECT SUM(total) FROM orders o JOIN order_items i ON i.order_id = o.id") == []


def test_unparseable_sql_yields_no_finding():
    """A guard that can break a working query is worse than one that misses."""
    assert find("this is not sql at all") == []


def test_the_same_aggregate_is_reported_once():
    findings = find(
        "SELECT SUM(o.total) FROM orders o "
        "JOIN order_items i ON i.order_id = o.id "
        "WHERE o.id IN (SELECT order_id FROM order_items)"
    )

    assert len(findings) == 1


# --------------------------------------------------------------------------
# Deriving the map from a real schema
# --------------------------------------------------------------------------


def test_the_map_is_built_from_foreign_keys():
    """The child declares the constraint, so the child is the "many" side."""
    snapshot = SchemaSnapshot(
        tables={
            "customers": Table("customers", (Column("id", "INT", False, True),), ()),
            "orders": Table(
                "orders",
                (Column("id", "INT", False, True),),
                (ForeignKey("orders", ("customer_id",), "customers", ("id",)),),
            ),
        },
        version="v1",
    )

    assert one_to_many_map(snapshot) == {"customers": {"orders"}}


def test_a_schema_with_no_foreign_keys_yields_an_empty_map():
    snapshot = SchemaSnapshot(
        tables={"solo": Table("solo", (Column("id", "INT", False, True),), ())},
        version="v1",
    )

    assert one_to_many_map(snapshot) == {}


# --------------------------------------------------------------------------
# Self-joins
#
# Missed on a real query. A retention analysis joined orders to orders — to
# find each customer's next order — then summed line-item values across the
# result. Each order repeated once per later order by the same customer, and an
# average order value of Rs 271 was reported as Rs 1,076. No foreign key
# describes that duplication, because the relationship is a table to itself.
# --------------------------------------------------------------------------


def test_a_self_join_is_flagged():
    findings = find(
        'SELECT SUM(o.total) FROM orders o '
        'LEFT JOIN orders o2 ON o2.customer_id = o.customer_id AND o2.id > o.id'
    )

    assert findings
    assert findings[0].self_join
    assert findings[0].multiplied_by == "orders"


def test_a_self_join_inflates_a_different_table_too():
    """Summing order_items across a self-joined orders inflates just as surely
    — the duplication happens before the aggregate sees anything."""
    findings = find(
        'SELECT SUM(i.price * i.quantity) FROM orders o '
        'JOIN order_items i ON i.order_id = o.id '
        'LEFT JOIN orders o2 ON o2.customer_id = o.customer_id AND o2.id > o.id'
    )

    assert any(f.self_join for f in findings)


def test_the_self_join_finding_explains_itself():
    findings = find(
        'SELECT SUM(o.total) FROM orders o JOIN orders o2 ON o2.customer_id = o.customer_id'
    )

    assert "joined to itself" in findings[0].render()


def test_a_plain_join_is_not_reported_as_a_self_join():
    findings = find(
        'SELECT SUM(i.price) FROM orders o JOIN order_items i ON i.order_id = o.id'
    )

    assert findings == []


def test_one_reference_per_table_is_not_a_self_join():
    findings = find('SELECT SUM(o.total) FROM orders o')

    assert findings == []


def test_a_self_join_is_reported_once_per_aggregate():
    findings = find(
        'SELECT SUM(o.total) FROM orders o '
        'JOIN orders o2 ON o2.customer_id = o.customer_id '
        'JOIN orders o3 ON o3.customer_id = o.customer_id'
    )

    assert len([f for f in findings if f.self_join]) == 1


# --------------------------------------------------------------------------
# Re-aggregating an aggregate
#
# Reported from the running app. A CTE counted DISTINCT orders per
# (kitchen, item); the outer query then SUMmed that per kitchen. An order with
# three items counted three times, and 269 cancelled orders were reported as
# 393. No fan-out the table-level check could see: the aggregate reads a CTE
# column, and a CTE is not in the foreign-key graph.
# --------------------------------------------------------------------------

REAGG = """
WITH CancelledOrders AS (
    SELECT o.kitchen_id, i.item_id, COUNT(DISTINCT o.id) AS order_count
    FROM orders o JOIN order_items i ON i.order_id = o.id
    GROUP BY o.kitchen_id, i.item_id
)
SELECT c.item_id, SUM(c.order_count) AS order_count
FROM CancelledOrders c GROUP BY c.item_id
"""


def test_summing_an_aggregate_from_a_cte_is_flagged():
    findings = [f for f in find(REAGG) if f.reaggregated]

    assert findings
    assert "order_count" in findings[0].multiplied_by


def test_the_reaggregation_finding_explains_the_double_counting():
    findings = [f for f in find(REAGG) if f.reaggregated]

    rendered = findings[0].render()
    assert "re-aggregates" in rendered
    assert "once per group" in rendered


def test_summing_a_plain_cte_column_is_fine():
    """The CTE carries a raw value, not an aggregate. Nothing is doubled."""
    findings = find(
        "WITH t AS (SELECT id, price FROM order_items) SELECT SUM(t.price) FROM t"
    )

    assert findings == []


def test_a_cte_referenced_twice_is_not_called_a_self_join():
    """sqlglot represents a CTE *reference* as a Table, so a CTE used in two
    places looked exactly like a self-join — a confusing message about a table
    that does not exist."""
    findings = find(
        "WITH t AS (SELECT id, price FROM order_items) "
        "SELECT SUM(t.price) FROM t JOIN t AS t2 ON t2.id = t.id"
    )

    assert [f for f in findings if f.self_join] == []


def test_a_real_self_join_still_fires_alongside_ctes():
    findings = find(
        "WITH t AS (SELECT id FROM order_items) "
        "SELECT SUM(o.total) FROM orders o "
        "JOIN orders o2 ON o2.customer_id = o.customer_id "
        "JOIN t ON t.id = o.id"
    )

    assert any(f.self_join for f in findings)
