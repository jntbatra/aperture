"""Tests for the SQL safety validator.

This is the security-critical layer, so the suite is adversarial: it tries the
things a confused or manipulated model might actually emit.
"""

from __future__ import annotations

import pytest

from sqlagent.guards.validator import ValidationError, validate

# --------------------------------------------------------------------------
# Statements that must be allowed
# --------------------------------------------------------------------------


def test_simple_select_passes():
    assert validate("SELECT 1").sql.startswith("SELECT")


def test_join_passes():
    sql = "SELECT c.name FROM customers c JOIN orders o ON o.customer_id = c.id"
    assert "JOIN" in validate(sql).sql


def test_aggregate_with_group_by_passes():
    sql = "SELECT country, SUM(total) FROM orders GROUP BY country"
    assert validate(sql).sql


def test_read_only_cte_passes():
    sql = "WITH recent AS (SELECT * FROM orders) SELECT count(*) FROM recent"
    assert validate(sql).sql


def test_union_passes():
    assert validate("SELECT 1 UNION SELECT 2").sql


def test_subquery_passes():
    sql = "SELECT * FROM (SELECT id FROM orders) AS o"
    assert validate(sql).sql


def test_window_function_passes():
    sql = "SELECT id, row_number() OVER (ORDER BY total DESC) FROM orders"
    assert validate(sql).sql


# --------------------------------------------------------------------------
# Write statements must be refused
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM orders",
        "UPDATE orders SET total = 0",
        "INSERT INTO orders (id) VALUES (1)",
        "DROP TABLE orders",
        "TRUNCATE orders",
        "ALTER TABLE orders ADD COLUMN x INT",
        "CREATE TABLE evil (id INT)",
        "GRANT ALL ON orders TO PUBLIC",
    ],
)
def test_write_statements_are_refused(sql: str):
    with pytest.raises(ValidationError, match="read-only SELECT"):
        validate(sql)


def test_error_message_names_the_statement_type():
    """The message is fed into the repair prompt, so it must be specific."""
    with pytest.raises(ValidationError, match="DELETE"):
        validate("DELETE FROM orders")


# --------------------------------------------------------------------------
# Statement smuggling
# --------------------------------------------------------------------------


def test_multiple_statements_are_refused():
    """Classic injection shape: a harmless query followed by a destructive one."""
    with pytest.raises(ValidationError, match="found 2"):
        validate("SELECT 1; DROP TABLE orders")


def test_write_inside_cte_is_refused():
    """The outer statement is a SELECT, but the query still deletes rows.

    Postgres genuinely supports this, so a top-level type check alone is not
    enough.
    """
    sql = "WITH gone AS (DELETE FROM orders RETURNING *) SELECT * FROM gone"
    with pytest.raises(ValidationError, match="WITH clause contains a DELETE"):
        validate(sql)


def test_update_inside_cte_is_refused():
    sql = "WITH bumped AS (UPDATE orders SET total = 1 RETURNING *) SELECT * FROM bumped"
    with pytest.raises(ValidationError, match="WITH clause"):
        validate(sql)


def test_the_word_drop_inside_a_string_is_allowed():
    """A regex-based guard would produce a false positive here."""
    assert validate("SELECT 'DROP TABLE orders' AS note").sql


def test_a_column_named_delete_is_allowed():
    assert validate('SELECT "delete" FROM audit_log').sql


# --------------------------------------------------------------------------
# Dangerous functions
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT pg_ls_dir('/')",
        "SELECT dblink('host=evil', 'SELECT 1')",
        "SELECT pg_sleep(60)",
        "SELECT lo_import('/etc/shadow')",
    ],
)
def test_forbidden_functions_are_refused(sql: str):
    with pytest.raises(ValidationError, match="not permitted"):
        validate(sql)


def test_ordinary_functions_are_allowed():
    sql = "SELECT upper(name), count(*), date_trunc('month', placed_at) FROM orders GROUP BY 1, 3"
    assert validate(sql).sql


# --------------------------------------------------------------------------
# Malformed input
# --------------------------------------------------------------------------


def test_empty_string_is_refused():
    with pytest.raises(ValidationError, match="Empty statement"):
        validate("")


def test_whitespace_only_is_refused():
    with pytest.raises(ValidationError, match="Empty statement"):
        validate("   \n  ")


def test_unparseable_sql_is_refused():
    with pytest.raises(ValidationError):
        validate("SELECT FROM WHERE ORDER BY GROUP")


# --------------------------------------------------------------------------
# Table extraction and allow-listing
# --------------------------------------------------------------------------


def test_referenced_tables_are_collected():
    sql = "SELECT * FROM customers c JOIN orders o ON o.customer_id = c.id"
    assert validate(sql).tables == {"customers", "orders"}


def test_cte_names_are_not_counted_as_tables():
    """A CTE alias looks like a table in the tree but is defined in the query."""
    sql = "WITH recent AS (SELECT * FROM orders) SELECT * FROM recent"
    assert validate(sql).tables == {"orders"}


def test_query_within_allowed_tables_passes():
    sql = "SELECT * FROM orders"
    assert validate(sql, allowed_tables={"orders", "customers"}).sql


def test_query_touching_an_unlisted_table_is_refused():
    """Catches a model inventing a plausible table it was never shown."""
    with pytest.raises(ValidationError, match="not provided in the schema"):
        validate("SELECT * FROM secret_salaries", allowed_tables={"orders"})


def test_unlisted_table_error_lists_what_is_available():
    with pytest.raises(ValidationError, match="Available tables: customers, orders"):
        validate("SELECT * FROM ghosts", allowed_tables={"orders", "customers"})


def test_cte_does_not_trip_the_allow_list():
    sql = "WITH t AS (SELECT * FROM orders) SELECT * FROM t"
    assert validate(sql, allowed_tables={"orders"}).sql


# --------------------------------------------------------------------------
# Row limits
# --------------------------------------------------------------------------


def test_limit_is_added_when_missing():
    result = validate("SELECT * FROM orders", row_limit=100)
    assert "LIMIT 100" in result.sql
    assert result.limit_added is True


def test_smaller_existing_limit_is_respected():
    """The model may have meant LIMIT 10; the cap is a ceiling, not a target."""
    result = validate("SELECT * FROM orders LIMIT 10", row_limit=100)
    assert "LIMIT 10" in result.sql
    assert result.limit_added is False


def test_larger_existing_limit_is_tightened():
    result = validate("SELECT * FROM orders LIMIT 50000", row_limit=100)
    assert "LIMIT 100" in result.sql
    assert result.limit_added is True


def test_no_limit_applied_when_row_limit_is_none():
    result = validate("SELECT * FROM orders")
    assert "LIMIT" not in result.sql.upper()
    assert result.limit_added is False


def test_union_is_wrapped_so_the_limit_covers_the_whole_result():
    """Attaching LIMIT to a UNION would bound only its final branch.

    That produces a wrong answer rather than an error, which is worse.
    """
    result = validate("SELECT 1 UNION SELECT 2", row_limit=5)
    assert result.sql.upper().count("LIMIT") == 1
    assert result.sql.upper().strip().startswith("SELECT")
    assert "LIMIT 5" in result.sql


def test_limit_survives_order_by():
    result = validate("SELECT * FROM orders ORDER BY total DESC", row_limit=10)
    assert "ORDER BY" in result.sql.upper()
    assert "LIMIT 10" in result.sql


# --------------------------------------------------------------------------
# Output usability
# --------------------------------------------------------------------------


def test_returned_sql_is_executable_text():
    result = validate("select  *   from orders", row_limit=10)
    assert result.sql.upper().startswith("SELECT")
    assert ";" not in result.sql


def test_validation_is_idempotent():
    """Re-validating already-validated SQL must not keep stacking limits."""
    once = validate("SELECT * FROM orders", row_limit=100)
    twice = validate(once.sql, row_limit=100)
    assert twice.sql.upper().count("LIMIT") == 1
    assert twice.limit_added is False


# --------------------------------------------------------------------------
# Identifier case
# --------------------------------------------------------------------------


def test_allow_list_is_case_insensitive():
    """SQL identifiers are case-insensitive unless quoted.

    A model writing `FROM player` against a table declared as `Player` has
    written a correct query, and the database resolves it without complaint.
    Comparing raw strings here rejected every such query, which broke whole
    databases that capitalise table names — a very common convention.
    """
    assert validate("SELECT * FROM player", allowed_tables={"Player"}).sql


def test_allow_list_matches_regardless_of_declared_case():
    sql = "SELECT * FROM Player p JOIN Match m ON m.home_player_1 = p.id"
    assert validate(sql, allowed_tables={"player", "match"}).sql


def test_allow_list_still_rejects_genuinely_unknown_tables():
    """Case-insensitivity must not become "accept anything"."""
    with pytest.raises(ValidationError, match="not provided in the schema"):
        validate("SELECT * FROM salaries", allowed_tables={"Player", "Match"})


# --------------------------------------------------------------------------
# Attacking the validator
#
# Written after someone asked whether a subquery could hide a DROP. The answer
# turned out to be no — `find_all(exp.CTE)` is a recursive walk, so nesting
# does not help — but the exercise found three real gaps, and these are the
# regression tests for them.
#
# The honest framing, which belongs beside the tests: this is the *second* of
# three layers. A denylist of function names protects against what somebody
# thought of. The read-only role is what actually holds.
# --------------------------------------------------------------------------

ATTACK_TABLES = {"orders", "users", "items", "gone", "d", "x", "t", "s"}


def refuses(sql: str) -> bool:
    from sqlagent.guards.validator import ValidationError, validate

    try:
        validate(sql, dialect="postgres", allowed_tables=ATTACK_TABLES, row_limit=100)
    except ValidationError:
        return True
    except Exception:
        return True
    return False


def test_a_string_that_merely_looks_dangerous_is_allowed():
    """The naive-guard failure in the other direction. A keyword denylist over
    the query *text* refuses this, and it is a completely harmless SELECT of a
    string literal."""
    assert not refuses("SELECT 'DROP TABLE orders' AS example")


def test_a_column_value_containing_sql_is_allowed():
    assert not refuses("SELECT * FROM orders WHERE status = 'delete from users'")


def test_a_column_named_after_a_keyword_is_allowed():
    assert not refuses('SELECT "delete" FROM orders')


# --- writes hidden in a WITH, at every depth -------------------------------


def test_a_deleting_cte_is_refused():
    assert refuses("WITH gone AS (DELETE FROM orders RETURNING *) SELECT * FROM gone")


def test_an_updating_cte_is_refused():
    assert refuses("WITH d AS (UPDATE orders SET total = 0 RETURNING *) SELECT * FROM d")


def test_an_inserting_cte_is_refused():
    assert refuses("WITH d AS (INSERT INTO orders VALUES (1) RETURNING *) SELECT * FROM d")


def test_a_deleting_cte_inside_a_subquery_is_refused():
    """The question that prompted all of this. `find_all` descends, so nesting
    buys an attacker nothing."""
    assert refuses(
        "SELECT * FROM (WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d) s"
    )


def test_a_deleting_cte_inside_an_in_clause_is_refused():
    assert refuses(
        "SELECT * FROM users WHERE id IN "
        "(WITH d AS (DELETE FROM orders RETURNING id) SELECT id FROM d)"
    )


def test_a_deleting_cte_in_a_scalar_subquery_is_refused():
    assert refuses(
        "SELECT (WITH d AS (DELETE FROM orders RETURNING 1) SELECT count(*) FROM d) AS n"
    )


def test_a_deleting_cte_two_levels_deep_is_refused():
    assert refuses(
        "WITH a AS (WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d) SELECT * FROM a"
    )


def test_a_deleting_cte_in_a_union_branch_is_refused():
    assert refuses(
        "SELECT 1 UNION ALL (WITH d AS (DELETE FROM orders RETURNING 1) SELECT 1 FROM d)"
    )


# --- SQL passed to a function as a string ----------------------------------
#
# The dangerous class and the one most easily missed: the statement really is a
# SELECT, its CTEs really do only read, and the DELETE is inside a string
# argument where no AST check can see it. Found by attacking this file; the
# read-only transaction caught it, which is why that layer exists — but a guard
# that relies on the next guard is not a guard.


def test_query_to_xml_executing_sql_is_refused():
    assert refuses("SELECT query_to_xml('DELETE FROM orders', true, true, '')")


def test_table_to_xml_is_refused():
    assert refuses("SELECT table_to_xml('orders', true, true, '')")


def test_query_to_xml_hidden_in_a_cte_is_refused():
    assert refuses(
        "WITH x AS (SELECT query_to_xml('DROP TABLE orders', true, true, '') AS v) "
        "SELECT * FROM x"
    )


# --- state that is not table data ------------------------------------------


def test_advancing_a_sequence_is_refused():
    """Not table data, and still a persistent change: a question must not be
    able to renumber anything."""
    assert refuses("SELECT nextval('s')")


def test_setting_a_sequence_is_refused():
    assert refuses("SELECT setval('s', 1)")


def test_reading_a_sequence_is_allowed():
    """`currval` only reads. Refusing it would be a denylist that had stopped
    distinguishing."""
    assert not refuses("SELECT currval('s')")


def test_changing_server_configuration_is_refused():
    assert refuses("SELECT pg_reload_conf()")


def test_changing_session_configuration_is_refused():
    assert refuses("SELECT set_config('work_mem', '1GB', false)")


def test_taking_an_advisory_lock_is_refused():
    assert refuses("SELECT pg_advisory_lock(1)")


# --- locking reads ---------------------------------------------------------


def test_a_locking_read_is_refused():
    """Structural, not a function name. A locking read returns rows and changes
    none, and blocks writers for the length of the transaction — a way for an
    analytics question to stall an application."""
    assert refuses("SELECT * FROM orders FOR UPDATE")


def test_for_share_is_refused_too():
    assert refuses("SELECT * FROM orders FOR SHARE")


# --- the rest --------------------------------------------------------------


def test_a_stacked_statement_is_refused():
    assert refuses("SELECT 1; DROP TABLE orders")


def test_a_statement_hidden_after_a_comment_is_refused():
    assert refuses("SELECT 1 -- \nDROP TABLE orders")


def test_select_into_is_refused():
    """It creates a table. The top-level node is still a Select, which is
    exactly the kind of thing a shallow type check waves through."""
    assert refuses("SELECT * INTO backup FROM orders")


def test_a_do_block_is_refused():
    assert refuses("DO $$ BEGIN DROP TABLE orders; END $$")


def test_copy_to_program_is_refused():
    assert refuses("COPY orders TO PROGRAM 'rm -rf /'")


def test_a_table_the_model_was_not_shown_is_refused():
    assert refuses("SELECT * FROM pg_shadow")
