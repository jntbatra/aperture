"""Domain facts the schema cannot tell you.

The failure this exists for
---------------------------
On a real database, revenue was reported as "₹1,234,500". The true figure was
**₹12,345** (figures illustrative; the 100× ratio is real).
``order_items.price`` stores paise — ``12500`` means ₹125.00 — and
nothing in the schema says so. The column's type is ``integer`` and its name is
``price``. Elsewhere in the same session the same kind of figure was labelled
``$``, for an Indian business.

No prompt fixes this, because it is not a reasoning failure. The information is
genuinely absent. A heuristic ("large integers in money-shaped columns are
probably minor units") would be wrong on every database that stores rupees
directly, and being confidently wrong about money is the whole problem.

The same gap produces subtler errors. ``cart_items`` and ``order_items`` are
both plausible readings of "ordered", and they give different answers — 300
against 78 on the same question. Only someone who knows the product knows which
is meant.

What a glossary is
------------------
A short list of declared facts about one database, written by someone who knows
it, injected into the prompt. Three kinds:

* **Units** — ``order_items.price is in paise; divide by 100 for rupees``.
  Attached to a column, so it can also be surfaced next to that column in the
  schema listing.
* **Terms** — ``"ordered" means order_items, never cart_items``.
* **Metrics** — ``revenue = SUM(order_items.price * order_items.quantity) / 100``.
  A named expression, so "revenue by month" stops being re-derived (differently)
  on every question.

Why declared rather than inferred
---------------------------------
Because a wrong declaration is visible and a wrong inference is not. A glossary
entry sits in a file someone can read and correct. An inferred unit conversion
is a silent multiplication buried in generated SQL, and the first sign it was
wrong is a business decision made on a number that was off by 100×.

Where it goes in the prompt
---------------------------
After the schema, before the conversation history. The schema says what exists;
the glossary says what it means; the history says what we were talking about.
A model reading in that order has the definitions in hand before it reaches the
question.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ColumnNote:
    """A fact about one column — most often its unit."""

    table: str
    column: str
    note: str

    @property
    def qualified(self) -> str:
        return f"{self.table}.{self.column}"

    def render(self) -> str:
        return f"{self.qualified}: {self.note}"


@dataclass(frozen=True, slots=True)
class Term:
    """What a word means in this business, when the schema is ambiguous."""

    word: str
    meaning: str

    def render(self) -> str:
        return f'"{self.word}" means {self.meaning}'


@dataclass(frozen=True, slots=True)
class Metric:
    """A named calculation, so it is not re-derived differently each time."""

    name: str
    expression: str
    note: str = ""

    def render(self) -> str:
        rendered = f"{self.name} = {self.expression}"
        return f"{rendered} ({self.note})" if self.note else rendered


@dataclass(frozen=True, slots=True)
class Glossary:
    """Everything declared about one database.

    Empty is the normal case and must cost nothing: with no entries this
    renders to an empty string, so prompts for an undeclared database are
    byte-identical to what they were before glossaries existed — which keeps
    every benchmark number comparable.
    """

    columns: tuple[ColumnNote, ...] = ()
    terms: tuple[Term, ...] = ()
    metrics: tuple[Metric, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.columns or self.terms or self.metrics)

    def for_tables(self, tables: set[str]) -> Glossary:
        """Narrow to the tables actually in this prompt.

        A fifty-column glossary on a question about two tables is forty-eight
        lines of noise competing with the schema for the model's attention.
        Terms and metrics are kept whole — they are few, and a metric that
        mentions a table not in context is itself a useful signal that the
        retrieval may have missed something.
        """
        if not tables:
            return self
        return Glossary(
            columns=tuple(note for note in self.columns if note.table in tables),
            terms=self.terms,
            metrics=self.metrics,
        )

    def render(self) -> str:
        """As prompt text, or an empty string when nothing is declared."""
        sections: list[str] = []

        if self.columns:
            sections.append(
                "Column meanings and units (apply these — the raw stored value "
                "is not the value to report):"
            )
            sections.extend(f"  {note.render()}" for note in self.columns)

        if self.terms:
            if sections:
                sections.append("")
            sections.append("What these words mean in this business:")
            sections.extend(f"  {term.render()}" for term in self.terms)

        if self.metrics:
            if sections:
                sections.append("")
            sections.append("Defined metrics (use these exact expressions):")
            sections.extend(f"  {metric.render()}" for metric in self.metrics)

        if not sections:
            return ""

        return "Domain notes for this database:\n" + "\n".join(sections)

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    @classmethod
    def from_dict(cls, payload: dict) -> Glossary:
        """Build from parsed JSON, ignoring entries that are not well formed.

        Lenient by design. A glossary is hand-written, and a typo in one entry
        should not take down the agent — the alternative is a server that
        refuses to start because someone misspelled a key. Skipped entries are
        logged so the mistake is findable.
        """
        columns: list[ColumnNote] = []
        for entry in payload.get("columns", []):
            try:
                table, column = str(entry["column"]).split(".", 1)
                columns.append(ColumnNote(table, column, str(entry["note"])))
            except (KeyError, TypeError, ValueError):
                logger.warning("skipping malformed glossary column entry: %r", entry)

        terms: list[Term] = []
        for entry in payload.get("terms", []):
            try:
                terms.append(Term(str(entry["word"]), str(entry["meaning"])))
            except (KeyError, TypeError):
                logger.warning("skipping malformed glossary term: %r", entry)

        metrics: list[Metric] = []
        for entry in payload.get("metrics", []):
            try:
                metrics.append(
                    Metric(
                        str(entry["name"]),
                        str(entry["expression"]),
                        str(entry.get("note", "")),
                    )
                )
            except (KeyError, TypeError):
                logger.warning("skipping malformed glossary metric: %r", entry)

        return cls(tuple(columns), tuple(terms), tuple(metrics))

    @classmethod
    def load(cls, path: str | Path | None) -> Glossary:
        """Read a glossary file, or return an empty one.

        No path is not an error — most databases have no glossary, and the
        agent works without one. A path that names a missing or malformed file
        is logged as an error: someone configured it intending it to take
        effect. A renamed glossary once failed silently here, and money was
        reported 100x too large because the "stored in paise" note never
        reached the model.
        """
        if not path:
            return cls()

        file = Path(path)
        if not file.exists():
            logger.error(
                "glossary %s is configured but does not exist; answering without it",
                file,
            )
            return cls()

        try:
            return cls.from_dict(json.loads(file.read_text()))
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("could not read glossary %s: %s", file, exc)
            return cls()
