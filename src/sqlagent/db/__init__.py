"""Database access: connecting, and running validated queries safely."""

from sqlagent.db.execute import ExecutionError, QueryResult, execute

__all__ = ["ExecutionError", "QueryResult", "execute"]
