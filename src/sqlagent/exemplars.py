"""Retrieve worked examples for the question being asked.

Why this is the largest lever available
---------------------------------------
In the only fine-grained ablation published on BIRD mini-dev — the same 500
questions this project scores on — removing dynamic few-shot exemplars costs
more than removing anything else:

    w/o few-shot          -6.2 at generation, -4.6 final
    w/o schema extraction -4.2 / -3.2
    w/o value retrieval   -1.4 / -1.4

Our pipeline had none. Every question was answered zero-shot.

Which examples, and the reason it matters here more than usual
--------------------------------------------------------------
The obvious source is BIRD's own train split, and it is a trap: BIRD's
annotations carry a **52.8% error rate**, so exemplars drawn from it teach the
model wrong SQL more often than right. ReViSQL measured exactly this on the
training side — fine-tuning on the original BIRD train scored **7 points below
not training at all**, and on an expert-verified subset it gained 7.2. Their
conclusion was that annotation quality, not method, was the binding
constraint.

So the examples here come from **BIRD-Verified** (ReViSQL's release): 2,064
pairs where a human expert corrected the question, the evidence and the SQL.
Verified against mini-dev before use — zero overlapping questions, zero
overlapping SQL, and the 69 training databases are disjoint from the 11
evaluation ones.

Cross-database, and deliberately
--------------------------------
An exemplar from another database cannot leak an answer and cannot be copied
verbatim. What transfers is *form*: how a ratio is expressed, when a CASE
belongs inside a SUM, that "top 5" needs ORDER BY with LIMIT. DAIL-SQL's
question-to-SQL pairs without schema work the same way and are the setting
OpenSearch-SQL measured.

BM25, not embeddings
--------------------
One network round trip per question to an embedding endpoint would cost more
latency than the generation call it is helping. BM25 over question tokens
runs in about a millisecond against 2,064 documents, needs no model and no
new dependency, and CodeS used Lucene BM25 for the same job.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

K1 = 1.5
B = 0.75
"""Standard BM25 parameters. Not tuned — tuning them on the evaluation set is
how a retrieval component starts reporting its own test score."""

MAX_SQL_CHARS = 600
"""An exemplar longer than this is teaching length, not form."""

_TOKEN = re.compile(r"[a-z0-9]+")

STOPWORDS = frozenset(
    """
    a an the of for from in on at by to and or not with without is are was were
    be been being how many much what which who whom whose when where why me my
    our us we you your show list give tell find get all every each per
    """.split()  # noqa: SIM905 - a block of words reads as a block of words
)


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in STOPWORDS]


@dataclass(frozen=True, slots=True)
class Exemplar:
    """One worked example: what was asked, and the SQL that answered it."""

    question: str
    sql: str
    evidence: str = ""

    def render(self) -> str:
        parts = [f"Question: {self.question}"]
        if self.evidence:
            parts.append(f"Context: {self.evidence}")
        parts.append(f"SQL: {self.sql}")
        return "\n".join(parts)


class ExemplarIndex:
    """BM25 over example questions. Built once, queried per request."""

    def __init__(self, exemplars: list[Exemplar]):
        self.exemplars = exemplars
        self._docs = [_tokens(e.question) for e in exemplars]
        self._len = [len(d) for d in self._docs]
        self._avg = (sum(self._len) / len(self._len)) if self._len else 0.0
        self._freq = [Counter(d) for d in self._docs]

        document_frequency: Counter[str] = Counter()
        for doc in self._docs:
            document_frequency.update(set(doc))
        total = max(len(self._docs), 1)
        self._idf = {
            term: math.log(1 + (total - count + 0.5) / (count + 0.5))
            for term, count in document_frequency.items()
        }

    def __len__(self) -> int:
        return len(self.exemplars)

    def retrieve(self, question: str, k: int = 3) -> list[Exemplar]:
        """The k most similar example questions, best first."""
        if k <= 0 or not self.exemplars:
            return []
        query = _tokens(question)
        if not query:
            return []

        scored: list[tuple[float, int]] = []
        for index, freq in enumerate(self._freq):
            length = self._len[index] or 1
            score = 0.0
            for term in query:
                count = freq.get(term)
                if not count:
                    continue
                denominator = count + K1 * (1 - B + B * length / (self._avg or 1))
                score += self._idf.get(term, 0.0) * count * (K1 + 1) / denominator
            if score > 0:
                scored.append((score, index))

        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        return [self.exemplars[index] for _, index in scored[:k]]


def load(path: Path | str) -> ExemplarIndex:
    """Read a BIRD-shaped JSON list into an index. Never raises.

    Accepts both the original BIRD fields and BIRD-Verified's, which carries
    the corrected question and SQL under the same names and keeps the
    originals under ``original_*``. The corrected ones are what we want.
    """
    try:
        raw = json.loads(Path(path).read_text())
    except Exception as exc:  # noqa: BLE001 - a missing example file is not fatal
        logger.warning("exemplars: cannot load %s: %s", path, exc)
        return ExemplarIndex([])

    exemplars: list[Exemplar] = []
    for entry in raw if isinstance(raw, list) else []:
        question = str(entry.get("question") or "").strip()
        sql = " ".join(str(entry.get("SQL") or "").split())
        if not question or not sql or len(sql) > MAX_SQL_CHARS:
            continue
        exemplars.append(
            Exemplar(
                question=question,
                sql=sql,
                evidence=str(entry.get("evidence") or "").strip(),
            )
        )

    logger.info("loaded %d exemplars from %s", len(exemplars), path)
    return ExemplarIndex(exemplars)


def render(exemplars: list[Exemplar]) -> str:
    """The prompt block. Empty when there is nothing to show."""
    if not exemplars:
        return ""
    body = "\n\n".join(e.render() for e in exemplars)
    return (
        "Worked examples from other databases. They show how questions of this "
        "shape are expressed in SQL — the tables and columns are not from the "
        "schema above and must not be used:\n\n" + body
    )
