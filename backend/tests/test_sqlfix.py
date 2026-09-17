from aperture.sqlfix import repair_identifiers


def test_quotes_bare_camel_case(snapshot):
    fix = repair_identifiers('select createdAt from orders', snapshot, dialect="postgres")
    assert fix.applied
    assert '"createdAt"' in fix.sql


def test_corrects_lower_cased_identifier(snapshot):
    fix = repair_identifiers("select createdat from orders", snapshot, dialect="postgres")
    assert '"createdAt"' in fix.sql


def test_leaves_correct_sql_untouched(snapshot):
    fix = repair_identifiers('select "createdAt", id from orders', snapshot, dialect="postgres")
    assert not fix.applied


def test_lowercase_only_identifiers_are_not_quoted(snapshot):
    fix = repair_identifiers("select id, status from orders", snapshot, dialect="postgres")
    assert not fix.applied


def test_sqlite_is_left_alone(snapshot):
    fix = repair_identifiers("select createdat from orders", snapshot, dialect="sqlite")
    assert not fix.applied
