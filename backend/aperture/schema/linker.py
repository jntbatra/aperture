"""Schema linking: choose the subschema a question actually needs.

Two problems, and they fail differently:

* **Schema linking** picks the tables. Getting it wrong means the model writes
  correct SQL against the wrong tables.
* **Value linking** picks the literals. Getting it wrong means correct SQL that
  returns nothing -- `status = 'completed'` against a database whose enum says
  `DELIVERED`. This is the dominant failure mode in practice, so observed
  values are matched against the question directly and surfaced as hints.

The lexical scorer here is deliberately simple and dependency-free; an
embedding index can replace the scoring step behind the same interface without
touching anything downstream.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import settings
from ..db.introspect import SchemaSnapshot
from ..db.profile import DatabaseProfile
from .graph import SchemaGraph

_WORD = re.compile(r"[a-z0-9]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

# Words that appear in every schema and carry no signal.
STOPWORDS = {
    "the", "a", "an", "of", "in", "on", "by", "for", "to", "and", "or", "is", "are",
    "was", "were", "how", "many", "much", "what", "which", "show", "me", "list",
    "get", "give", "count", "total", "per", "each", "all", "from", "with", "that",
    "this", "id", "at", "created", "updated", "table", "data",
}


def tokenize(text: str) -> set[str]:
    """Split identifiers and prose into comparable tokens.

    `createdAt` and `order_items` both have to reduce to the same vocabulary as
    the English in the question, so camelCase is split before lowercasing.
    """
    spaced = _CAMEL.sub(" ", text)
    tokens = set()
    for word in _WORD.findall(spaced.lower()):
        if word in STOPWORDS or len(word) < 2:
            continue
        tokens.add(word)
        if len(word) > 3 and word.endswith("s"):
            tokens.add(word[:-1])
    return tokens


@dataclass
class LinkedSchema:
    tables: list[str]
    ddl: str
    join_hints: list[str] = field(default_factory=list)
    fan_out_warnings: list[str] = field(default_factory=list)
    value_hints: list[str] = field(default_factory=list)
    empty_tables: list[str] = field(default_factory=list)
    seeds: list[str] = field(default_factory=list)
    # Names of every table, so an unlisted one is visibly not an option.
    inventory: list[str] = field(default_factory=list)

    def as_prompt_section(self) -> str:
        parts = []
        if self.inventory:
            parts.append(
                "EVERY TABLE IN THIS DATABASE (use only these names)\n  "
                + ", ".join(self.inventory)
            )
        parts.append(f"SCHEMA OF THE RELEVANT TABLES\n{self.ddl}")
        if self.join_hints:
            parts.append("JOINS\n" + "\n".join(f"  {h}" for h in self.join_hints))
        if self.value_hints:
            parts.append("VALUE HINTS\n" + "\n".join(f"  {v}" for v in self.value_hints))
        if self.fan_out_warnings:
            parts.append("CAUTION\n" + "\n".join(f"  {w}" for w in self.fan_out_warnings))
        if self.empty_tables:
            parts.append(
                "EMPTY TABLES (contain no rows; a query against them returns nothing)\n  "
                + ", ".join(self.empty_tables)
            )
        return "\n\n".join(parts)


@dataclass
class TableDoc:
    name: str
    tokens: set[str]
    rows: int


class SchemaLinker:
    """Scores tables against a question, then expands over foreign keys."""

    def __init__(
        self,
        snapshot: SchemaSnapshot,
        profile: DatabaseProfile,
        graph: SchemaGraph | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.profile = profile
        self.graph = graph or SchemaGraph.build(snapshot, profile)
        self.docs = [self._describe(name) for name in snapshot.tables]

    def _describe(self, name: str) -> TableDoc:
        table = self.snapshot.tables[name]
        text = [name, table.comment or ""]
        text.extend(column.name for column in table.columns)
        text.extend(column.comment or "" for column in table.columns)
        rows = self.profile.tables[name].exact_rows if name in self.profile.tables else 0
        return TableDoc(name=name, tokens=tokenize(" ".join(text)), rows=rows)

    def score_tables(self, question: str) -> list[tuple[str, float]]:
        asked = tokenize(question)
        scored: list[tuple[str, float]] = []
        for doc in self.docs:
            overlap = asked & doc.tokens
            if not overlap:
                continue
            # Table-name matches are worth more than a column buried in a wide
            # table, and an empty table is almost never the intended answer.
            name_tokens = tokenize(doc.name)
            score = len(overlap) + 2.0 * len(asked & name_tokens)
            if doc.rows == 0:
                score *= 0.35
            scored.append((doc.name, score))
        return sorted(scored, key=lambda pair: (-pair[1], pair[0]))

    def match_values(self, question: str) -> tuple[list[str], list[str]]:
        """Find observed column values mentioned in the question.

        Returns (hints, tables) -- a question saying "delivered" should both
        pin the literal to `DELIVERED` and seed the table it lives in.
        """
        asked = tokenize(question)
        hints: list[str] = []
        tables: list[str] = []
        for table_name, table_profile in self.profile.tables.items():
            for column_name, column in table_profile.columns.items():
                if column.sensitive or not column.common_values:
                    continue
                for value in column.common_values:
                    text = str(value)
                    if len(text) > 40:
                        continue
                    value_tokens = tokenize(text)
                    # Every token must appear, or "coupon usage by customer"
                    # matches CONFUSED_CUSTOMER and pins a wrong literal.
                    if value_tokens and value_tokens <= asked:
                        hints.append(f"{table_name}.{column_name} = '{text}'")
                        tables.append(table_name)
        return hints[:12], list(dict.fromkeys(tables))

    def link(
        self,
        question: str,
        *,
        top_k: int | None = None,
        extra_seeds: list[str] | None = None,
    ) -> LinkedSchema:
        """Retrieve the subschema for `question`.

        `extra_seeds` carries tables named by a matched metric definition.
        Those are authoritative -- "revenue" names no table, so lexical scoring
        alone drifts to whatever shares a word with the question.
        """
        cfg = settings()
        top_k = top_k or cfg.link_top_k_tables

        scored = self.score_tables(question)
        value_hints, value_tables = self.match_values(question)

        known = [t for t in (extra_seeds or []) if t in self.snapshot.tables]
        seeds = list(dict.fromkeys(known + value_tables + [name for name, _ in scored[:top_k]]))
        if not seeds:
            # Nothing matched: fall back to the busiest tables, which is where
            # an unguided question about "the data" almost always points.
            seeds = [
                name
                for name, _ in sorted(
                    ((d.name, d.rows) for d in self.docs), key=lambda p: -p[1]
                )[:top_k]
            ]

        tables = self.graph.expand(seeds, hops=cfg.link_hops, budget=cfg.link_table_budget)
        empty = [t for t in tables if t in self.profile.tables and self.profile.tables[t].is_empty]

        return LinkedSchema(
            inventory=sorted(self.snapshot.tables),
            tables=tables,
            ddl=self.snapshot.ddl_for(tables, profile=self.profile),
            join_hints=self.graph.join_hints(tables),
            fan_out_warnings=self.graph.fan_out_warnings(tables),
            value_hints=value_hints,
            empty_tables=empty,
            seeds=seeds,
        )
