"""Remember the SQL a question produced, not the rows it returned.

What is cached, and what deliberately is not
-------------------------------------------
The **SQL**. Never the results.

Caching results would be faster still and wrong almost immediately: the database
is live, and "how many orders today" answered from a cache is a stale number
presented as a current one. Caching the SQL skips the expensive part — two or
three model calls, several seconds — while the query itself re-runs against real
data every time. A cache hit is fresh *and* fast.

It also keeps every safety property intact. A cached statement goes through the
validator, the cost gate and the read-only transaction exactly like a generated
one. The cache is an optimisation in front of the model, not a bypass around the
guards.

What the key has to include
---------------------------
* The **question**, normalised — whitespace collapsed, case folded, trailing
  punctuation dropped. "How many orders?" and "how many orders" are the same
  question and should not each pay for their own generation.
* The **schema version**. A column rename makes every cached statement
  potentially invalid, and the version hash already changes on exactly that
  event.
* The **dataset**, because the same question means something different against
  an uploaded CSV than against the production database.
* The **glossary**, because changing "price is in paise" changes what the right
  SQL is. Without this, editing a glossary would appear to do nothing for every
  question already asked.

Why conversational turns are never cached
-----------------------------------------
"And for April?" is not a question. Its meaning comes entirely from the turns
before it, and two threads can give the same fragment completely different
meanings. Keying on the rendered history would technically work and would
essentially never hit, so follow-ups skip the cache entirely — cheaper, and it
removes any chance of serving one conversation's answer into another.

Why in-process
--------------
A dictionary with an LRU bound. One process, one database, a working set of a
few hundred questions. Redis would add an operational dependency, a
serialisation format and a failure mode, to cache a few kilobytes of text.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_PUNCTUATION = re.compile(r"[?!.\s]+$")
_WHITESPACE = re.compile(r"\s+")


def normalise(question: str) -> str:
    """Reduce a question to its cache identity.

    Case folding and whitespace collapsing only. Deliberately not stemming,
    synonym expansion or embedding similarity: a near-miss that returns the
    wrong cached SQL is far worse than a miss that costs a model call, and
    "revenue last month" against "revenue last week" is exactly the kind of pair
    a fuzzy matcher would happily conflate.
    """
    collapsed = _WHITESPACE.sub(" ", question.strip()).casefold()
    return _PUNCTUATION.sub("", collapsed)


@dataclass(frozen=True, slots=True)
class CacheStats:
    hits: int = 0
    misses: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


class SqlCache:
    """Question -> SQL, bounded, thread-safe.

    Thread safety matters: the API runs the agent in a worker thread pool, so
    two requests can touch the cache at once. The lock is held only around
    dictionary operations, never across a model call.
    """

    def __init__(self, max_entries: int = 512) -> None:
        self.max_entries = max_entries
        self._entries: OrderedDict[str, str] = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    @staticmethod
    def key(
        question: str,
        *,
        schema_version: str,
        dataset_id: str | None = None,
        glossary: str = "",
    ) -> str:
        """Everything that changes what the right SQL is, hashed together."""
        material = "\x00".join(
            [
                normalise(question),
                schema_version,
                dataset_id or "",
                hashlib.sha256(glossary.encode("utf-8")).hexdigest()[:16],
            ]
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]

    def get(self, key: str) -> str | None:
        with self._lock:
            sql = self._entries.get(key)
            if sql is None:
                self._misses += 1
                return None
            # Least-recently-used: touching an entry moves it to the end, so
            # eviction drops the question nobody has asked in a while rather
            # than the one asked most often.
            self._entries.move_to_end(key)
            self._hits += 1
            return sql

    def put(self, key: str, sql: str) -> None:
        with self._lock:
            self._entries[key] = sql
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    def invalidate(self, key: str) -> None:
        """Drop one entry.

        Called when a cached statement fails: the schema may have drifted in a
        way the version hash has not caught yet, or the query was always
        fragile. Either way, serving it again would repeat the failure.
        """
        with self._lock:
            self._entries.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    @property
    def stats(self) -> CacheStats:
        with self._lock:
            return CacheStats(hits=self._hits, misses=self._misses)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
