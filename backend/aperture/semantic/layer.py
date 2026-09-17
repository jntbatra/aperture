"""The semantic layer.

A schema says `orders.status` exists. It does not say that "revenue" means the
sum of `totalAmount` over delivered orders only, in paise, excluding cancelled
ones. Left to infer that, a model invents a definition per question and the
same question answered twice gives two numbers.

Definitions live in YAML so they are reviewable by whoever owns the metric,
and only the definitions a question actually touches are put in the prompt.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..schema.linker import tokenize

log = logging.getLogger(__name__)


@dataclass
class Metric:
    name: str
    description: str = ""
    expression: str = ""
    filter: str = ""
    tables: list[str] = field(default_factory=list)
    synonyms: list[str] = field(default_factory=list)
    notes: str = ""

    @property
    def vocabulary(self) -> set[str]:
        words = tokenize(self.name) | tokenize(" ".join(self.synonyms))
        return words

    def render(self) -> str:
        lines = [f"- {self.name}: {self.description}".rstrip(": ")]
        if self.expression:
            lines.append(f"    expression: {self.expression}")
        if self.filter:
            lines.append(f"    filter: {self.filter}")
        if self.notes:
            lines.append(f"    note: {self.notes}")
        return "\n".join(lines)


@dataclass
class SemanticLayer:
    metrics: list[Metric] = field(default_factory=list)
    conventions: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> SemanticLayer:
        path = Path(path)
        if not path.exists():
            return cls()
        data = yaml.safe_load(path.read_text()) or {}
        metrics = [Metric(name=name, **(body or {})) for name, body in (data.get("metrics") or {}).items()]
        return cls(metrics=metrics, conventions=list(data.get("conventions") or []))

    @classmethod
    def default(cls) -> SemanticLayer:
        return cls.load(Path(__file__).parent / "semantic.yaml")

    def match(self, question: str, *, limit: int = 4) -> list[Metric]:
        """Metrics whose name or synonyms appear in the question."""
        asked = tokenize(question)
        scored = []
        for metric in self.metrics:
            overlap = asked & metric.vocabulary
            if overlap:
                scored.append((len(overlap), metric))
        scored.sort(key=lambda pair: -pair[0])
        return [metric for _, metric in scored[:limit]]

    def prompt_section(self, question: str) -> str:
        matched = self.match(question)
        if not matched and not self.conventions:
            return ""
        parts = []
        if matched:
            parts.append(
                "METRIC DEFINITIONS (use these exact definitions; do not invent your own)\n"
                + "\n".join(metric.render() for metric in matched)
            )
        if self.conventions:
            parts.append("CONVENTIONS\n" + "\n".join(f"  - {c}" for c in self.conventions))
        return "\n\n".join(parts)
