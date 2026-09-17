import pytest

from aperture.extract import NoSQLFound, extract_sql


def test_takes_first_fence_not_last():
    text = "First:\n```sql\nSELECT a FROM t;\n```\nOr:\n```sql\nSELECT b FROM t;\n```"
    assert extract_sql(text, dialect="postgres") == "SELECT a FROM t"


def test_prose_containing_with_is_not_sql():
    with pytest.raises(NoSQLFound):
        extract_sql("I cannot help with that.", dialect="postgres")


def test_bare_statement_is_recovered():
    assert extract_sql("here: SELECT 1 FROM t", dialect="postgres").startswith("SELECT 1")


def test_broken_sql_in_fence_is_returned_for_the_validator_to_explain():
    assert extract_sql("```sql\nSELCT oops FROM\n```", dialect="postgres") == "SELCT oops FROM"


def test_empty_response_raises():
    with pytest.raises(NoSQLFound):
        extract_sql("   ", dialect="postgres")
