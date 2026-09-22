"""sqlagent — a conversational analyst that answers questions over a SQL database.

Package layout (built feature by feature, each one complete before the next):

* ``sqlagent.schema`` — read the database's structure, model it as a graph, and
  retrieve the small subset of tables relevant to a given question.
"""

__version__ = "0.1.0"
