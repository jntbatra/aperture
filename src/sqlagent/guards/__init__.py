"""Safety layers applied to every generated statement before it runs.

The validator proves a statement is a single read-only SELECT, bounded by a row
limit, touching only tables the model was actually shown.
"""

from sqlagent.guards.validator import ValidationError, ValidationResult, validate

__all__ = ["ValidationError", "ValidationResult", "validate"]
