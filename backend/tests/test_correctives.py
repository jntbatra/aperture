from aperture.graph.nodes import corrective_for


def test_missing_from_entry_names_the_available_tables():
    note = corrective_for("42P01", attempts=0, max_attempts=3, tables=["orders", "users"])
    assert "FROM or JOIN" in note
    assert "orders, users" in note


def test_undefined_column_asks_for_exact_spelling():
    note = corrective_for("42703", attempts=0, max_attempts=3)
    assert "camelCase" in note


def test_bad_enum_literal_points_at_value_hints():
    assert "VALUE HINTS" in corrective_for("22P02", attempts=0, max_attempts=3)


def test_timeout_asks_to_narrow_rather_than_rewrite():
    assert "Narrow it" in corrective_for("57014", attempts=0, max_attempts=3)


def test_last_attempt_asks_for_a_simpler_query():
    early = corrective_for("42703", attempts=0, max_attempts=3)
    final = corrective_for("42703", attempts=2, max_attempts=3)
    assert "simplest formulation" not in early
    assert "simplest formulation" in final


def test_unknown_failure_class_adds_nothing_early():
    assert corrective_for("mystery", attempts=0, max_attempts=3) == ""
