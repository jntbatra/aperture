"""Tests for the SQL cache.

The property that matters most is what is *not* cached. Caching results would be
faster and wrong almost immediately — the database is live, and "how many orders
today" served from a cache is a stale number presented as a current one.
"""

from __future__ import annotations

from sqlagent.cache import SqlCache, normalise


def key(cache: SqlCache, question: str, **kwargs) -> str:
    kwargs.setdefault("schema_version", "v1")
    return cache.key(question, **kwargs)


# --------------------------------------------------------------------------
# Normalising a question
# --------------------------------------------------------------------------


def test_case_and_punctuation_do_not_make_a_new_question():
    assert normalise("How many orders?") == normalise("how many orders")


def test_whitespace_is_collapsed():
    assert normalise("how   many\n orders") == "how many orders"


def test_different_questions_stay_different():
    """Deliberately not fuzzy. A near-miss returning the wrong cached SQL is far
    worse than a miss costing one model call."""
    assert normalise("revenue last month") != normalise("revenue last week")


# --------------------------------------------------------------------------
# The key
# --------------------------------------------------------------------------


def test_the_same_question_maps_to_the_same_key():
    cache = SqlCache()

    assert key(cache, "How many orders?") == key(cache, "how many orders")


def test_a_schema_change_invalidates_everything():
    """A column rename makes cached statements potentially invalid, and the
    version hash changes on exactly that event."""
    cache = SqlCache()

    assert key(cache, "q", schema_version="v1") != key(cache, "q", schema_version="v2")


def test_editing_the_glossary_invalidates_cached_sql():
    """Changing "price is in paise" changes what the right SQL is. Without this,
    editing a glossary would appear to do nothing for questions already asked."""
    cache = SqlCache()

    assert key(cache, "revenue?", glossary="") != key(
        cache, "revenue?", glossary="price is in paise"
    )


# --------------------------------------------------------------------------
# Storing and reading
# --------------------------------------------------------------------------


def test_a_stored_statement_comes_back():
    cache = SqlCache()
    k = key(cache, "How many orders?")

    cache.put(k, "SELECT count(*) FROM orders")

    assert cache.get(k) == "SELECT count(*) FROM orders"


def test_an_unknown_question_misses():
    assert SqlCache().get("nothing here") is None


def test_hits_and_misses_are_counted():
    cache = SqlCache()
    k = key(cache, "q")
    cache.put(k, "SELECT 1")

    cache.get(k)
    cache.get("absent")

    assert cache.stats.hits == 1
    assert cache.stats.misses == 1
    assert cache.stats.hit_rate == 0.5


def test_hit_rate_of_an_untouched_cache_is_zero_not_an_error():
    assert SqlCache().stats.hit_rate == 0.0


# --------------------------------------------------------------------------
# Bounding and eviction
# --------------------------------------------------------------------------


def test_the_cache_is_bounded():
    cache = SqlCache(max_entries=3)

    for index in range(10):
        cache.put(f"key{index}", f"SELECT {index}")

    assert len(cache) == 3


def test_eviction_drops_the_least_recently_used():
    """Not the oldest — the one nobody has asked for in a while."""
    cache = SqlCache(max_entries=2)
    cache.put("a", "SELECT a")
    cache.put("b", "SELECT b")

    cache.get("a")  # 'a' is now the most recently used
    cache.put("c", "SELECT c")

    assert cache.get("a") == "SELECT a"
    assert cache.get("b") is None


# --------------------------------------------------------------------------
# Invalidation
# --------------------------------------------------------------------------


def test_a_failed_statement_can_be_dropped():
    """Serving it again would repeat the failure, and the repair path would keep
    starting from a statement known to be broken."""
    cache = SqlCache()
    cache.put("k", "SELECT broken")

    cache.invalidate("k")

    assert cache.get("k") is None


def test_invalidating_something_absent_is_harmless():
    SqlCache().invalidate("never stored")


def test_clear_empties_the_cache():
    cache = SqlCache()
    cache.put("k", "SELECT 1")

    cache.clear()

    assert len(cache) == 0
