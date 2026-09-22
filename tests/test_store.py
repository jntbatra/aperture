"""Tests for history and dataset persistence."""

from __future__ import annotations

import pytest

from sqlagent.store import Store


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "store.db")


def add(store: Store, question: str, **kwargs) -> int:
    defaults = {
        "answer": "an answer",
        "sql": "SELECT 1",
        "ok": True,
        "row_count": 1,
        "seconds": 1.0,
        "model_calls": 2,
        "tokens": 100,
        "repairs": 0,
    }
    return store.record_question(question=question, **{**defaults, **kwargs})


# --------------------------------------------------------------------------
# Recording and reading back
#
# Turns are read back through the conversation they belong to. There is no flat
# "every question ever asked" list any more: half its entries were fragments
# like "and for April?" that have no subject outside their thread.
# --------------------------------------------------------------------------


def test_records_and_returns_a_question(store: Store):
    thread = store.create_conversation()
    add(store, "How many customers?", conversation_id=thread)

    entries = store.conversation_entries(thread)

    assert len(entries) == 1
    assert entries[0].question == "How many customers?"
    assert entries[0].sql == "SELECT 1"
    assert entries[0].ok is True


def test_failures_are_recorded_too(store: Store):
    """A question that could not be answered is often the most useful entry."""
    thread = store.create_conversation()
    add(store, "impossible", conversation_id=thread, ok=False,
        error="no such table", sql=None, answer=None)

    entry = store.conversation_entries(thread)[0]

    assert entry.ok is False
    assert entry.error == "no such table"


def test_diagnostics_survive_the_round_trip(store: Store):
    thread = store.create_conversation()
    add(store, "q", conversation_id=thread, seconds=2.5, model_calls=3,
        tokens=1234, repairs=1)

    entry = store.conversation_entries(thread)[0]

    assert entry.seconds == 2.5
    assert entry.model_calls == 3
    assert entry.tokens == 1234
    assert entry.repairs == 1


def test_entries_are_timestamped(store: Store):
    thread = store.create_conversation()
    add(store, "q", conversation_id=thread)

    assert store.conversation_entries(thread)[0].asked_at


def test_a_small_result_is_kept_for_the_next_turn(store: Store):
    """The bounded exception to "no result rows": without it, a follow-up
    saying "that ID" has nothing to refer to."""
    thread = store.create_conversation()
    add(store, "which item?", conversation_id=thread,
        result_preview={"columns": ["id"], "rows": [["abc-123"]]})

    assert store.conversation_entries(thread)[0].result_preview == {
        "columns": ["id"],
        "rows": [["abc-123"]],
    }


# --------------------------------------------------------------------------
# Search
#
# The one thing the removed history view did well: find the query you wrote
# last week.
# --------------------------------------------------------------------------


def test_search_matches_a_substring(store: Store):
    add(store, "How many customers are there?")
    add(store, "Total revenue by region")

    assert [e.question for e in store.search_turns("revenue")] == [
        "Total revenue by region"
    ]


def test_search_is_case_insensitive_via_like(store: Store):
    """SQLite LIKE is case-insensitive for ASCII, which is what a user expects."""
    add(store, "Total Revenue By Region")

    assert len(store.search_turns("revenue")) == 1


def test_search_with_no_match_is_empty(store: Store):
    add(store, "How many customers?")
    assert store.search_turns("zzzz") == []


def test_search_reports_which_conversation_a_turn_came_from(store: Store):
    """A fragment is unreadable without the thread that gave it a subject."""
    thread = store.create_conversation()
    add(store, "and for April?", conversation_id=thread)

    assert store.search_turns("April")[0].conversation_id == thread


def test_search_is_newest_first(store: Store):
    add(store, "revenue, asked first")
    add(store, "revenue, asked second")

    assert [e.question for e in store.search_turns("revenue")] == [
        "revenue, asked second",
        "revenue, asked first",
    ]


def test_search_respects_its_limit(store: Store):
    for index in range(10):
        add(store, f"revenue question {index}")

    assert len(store.search_turns("revenue", limit=3)) == 3


# --------------------------------------------------------------------------
# Aggregate numbers
# --------------------------------------------------------------------------


def test_stats_count_every_question(store: Store):
    add(store, "one")
    add(store, "two", ok=False)

    numbers = store.stats()

    assert numbers["total"] == 2
    assert numbers["successful"] == 1
    assert numbers["success_rate"] == 0.5


def test_stats_on_an_empty_store_do_not_divide_by_zero(store: Store):
    assert store.stats()["success_rate"] == 0.0


# --------------------------------------------------------------------------
# Datasets
# --------------------------------------------------------------------------


class FakeDataset:
    id = "ds1"
    name = "sales.csv"
    kind = "csv"
    database_url = "sqlite:///tmp/ds1.sqlite"
    tables = ["sales"]
    row_counts = {"sales": 42}
    created_at = "2026-01-01T00:00:00+00:00"
    note = "types inferred"


def test_dataset_round_trips(store: Store):
    store.save_dataset(FakeDataset())

    datasets = store.list_datasets()

    assert len(datasets) == 1
    assert datasets[0]["name"] == "sales.csv"
    assert datasets[0]["tables"] == ["sales"]
    assert datasets[0]["row_counts"] == {"sales": 42}


def test_get_dataset_by_id(store: Store):
    store.save_dataset(FakeDataset())

    assert store.get_dataset("ds1")["kind"] == "csv"
    assert store.get_dataset("nope") is None


def test_saving_the_same_id_twice_replaces_it(store: Store):
    store.save_dataset(FakeDataset())
    store.save_dataset(FakeDataset())

    assert len(store.list_datasets()) == 1


def test_deleting_a_dataset_removes_its_sqlite_file(store: Store, tmp_path):
    """The file is the data; leaving it behind would leak disk indefinitely."""
    file = tmp_path / "ds1.sqlite"
    file.write_text("not really a database")

    dataset = FakeDataset()
    dataset.database_url = f"sqlite:///{file}"
    store.save_dataset(dataset)

    assert store.delete_dataset("ds1") is True
    assert not file.exists()
    assert store.list_datasets() == []


def test_deleting_a_missing_dataset_reports_false(store: Store):
    assert store.delete_dataset("nope") is False


def test_deleting_a_postgres_dataset_leaves_the_database_alone(store: Store):
    """Dropping someone's restored database from a UI click is not this code's call."""
    dataset = FakeDataset()
    dataset.id = "pg1"
    dataset.database_url = "postgresql+psycopg://u:p@localhost:5432/upload_pg1"
    store.save_dataset(dataset)

    assert store.delete_dataset("pg1") is True
    assert store.get_dataset("pg1") is None


# --------------------------------------------------------------------------
# Durability
# --------------------------------------------------------------------------


def test_data_survives_reopening(tmp_path):
    """The point of persistence: a restart must not lose history."""
    path = tmp_path / "store.db"

    first = Store(path)
    thread = first.create_conversation()
    add(first, "remembered", conversation_id=thread)
    first.close()

    assert [e.question for e in Store(path).conversation_entries(thread)] == [
        "remembered"
    ]


# --------------------------------------------------------------------------
# Conversations
# --------------------------------------------------------------------------


def test_a_new_conversation_starts_empty(store: Store):
    conversation_id = store.create_conversation()

    assert store.get_conversation(conversation_id) is not None
    assert store.conversation_entries(conversation_id) == []


def test_turns_come_back_oldest_first(store: Store):
    """Opposite of the history list, and deliberately so.

    A transcript reads forwards, and "the previous question" only means
    something if the turns are in the order they were asked.
    """
    conversation_id = store.create_conversation()
    add(store, "first", conversation_id=conversation_id)
    add(store, "second", conversation_id=conversation_id)

    entries = store.conversation_entries(conversation_id)

    assert [e.question for e in entries] == ["first", "second"]


def test_a_thread_holds_only_its_own_turns(store: Store):
    one = store.create_conversation()
    two = store.create_conversation()
    add(store, "in one", conversation_id=one)
    add(store, "in two", conversation_id=two)

    assert [e.question for e in store.conversation_entries(one)] == ["in one"]


def test_questions_asked_outside_a_thread_still_record(store: Store):
    """conversation_id is optional; the CLI and benchmark never set one."""
    add(store, "standalone")

    assert store.search_turns("standalone")[0].conversation_id is None


def test_the_first_question_names_the_thread(store: Store):
    conversation_id = store.create_conversation()
    store.touch_conversation(conversation_id, title="How many refunds?")

    assert store.get_conversation(conversation_id)["title"] == "How many refunds?"


def test_later_questions_do_not_rename_the_thread(store: Store):
    """A label that moves as the thread grows cannot be found again."""
    conversation_id = store.create_conversation()
    store.touch_conversation(conversation_id, title="How many refunds?")
    store.touch_conversation(conversation_id, title="and for April?")

    assert store.get_conversation(conversation_id)["title"] == "How many refunds?"


def test_listing_reports_how_many_turns_each_thread_has(store: Store):
    conversation_id = store.create_conversation()
    add(store, "one", conversation_id=conversation_id)
    add(store, "two", conversation_id=conversation_id)

    listed = store.list_conversations()

    assert listed[0]["id"] == conversation_id
    assert listed[0]["turns"] == 2


def test_deleting_a_thread_takes_its_questions_with_it(store: Store):
    """Half of them are fragments that mean nothing without the thread."""
    conversation_id = store.create_conversation()
    add(store, "and for April?", conversation_id=conversation_id)

    assert store.delete_conversation(conversation_id) is True
    assert store.get_conversation(conversation_id) is None
    assert store.conversation_entries(conversation_id) == []
    assert store.stats()["total"] == 0


def test_deleting_a_missing_thread_reports_false(store: Store):
    assert store.delete_conversation("nope") is False


def test_a_store_written_before_conversations_existed_is_migrated(tmp_path):
    """CREATE TABLE IF NOT EXISTS does nothing to an existing table.

    Without an explicit ALTER, an older store keeps a history table with no
    conversation_id column and every insert then fails.
    """
    from sqlalchemy import create_engine, text

    path = tmp_path / "old.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE history (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "asked_at TEXT NOT NULL, question TEXT NOT NULL, answer TEXT, "
                "sql TEXT, dataset_id TEXT, ok INTEGER NOT NULL, error TEXT, "
                "row_count INTEGER, seconds REAL, model_calls INTEGER, "
                "tokens INTEGER, repairs INTEGER)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO history (asked_at, question, ok) "
                "VALUES ('2026-01-01T00:00:00+00:00', 'asked before the upgrade', 1)"
            )
        )
    engine.dispose()

    store = Store(path)
    add(store, "asked after the upgrade")

    questions = [e.question for e in store.search_turns("asked")]
    assert "asked before the upgrade" in questions
    assert "asked after the upgrade" in questions


# --------------------------------------------------------------------------
# Clarifications are not failures
#
# A clarification is stored with ok = 0 — no answer was produced — and counting
# it against the success rate made the sidebar report 65% answered on a system
# where almost nothing had failed and a third of turns were the agent declining
# to guess.
# --------------------------------------------------------------------------


def test_a_clarification_does_not_lower_the_success_rate(tmp_path):
    from sqlagent.clarify import CLARIFICATION_ERROR
    from sqlagent.store import Store

    store = Store(tmp_path / "s.db")
    for _ in range(7):
        store.record_question(question="q", answer="a", sql="SELECT 1", ok=True)
    for _ in range(3):
        store.record_question(
            question="vague", answer="which one?", sql=None,
            ok=False, error=CLARIFICATION_ERROR,
        )

    stats = store.stats()

    assert stats["total"] == 10
    assert stats["clarified"] == 3
    assert stats["success_rate"] == 1.0


def test_a_real_failure_still_lowers_the_success_rate(tmp_path):
    """Excluding clarifications must not mask what the rate is for."""
    from sqlagent.store import Store

    store = Store(tmp_path / "s.db")
    for _ in range(3):
        store.record_question(question="q", answer="a", sql="SELECT 1", ok=True)
    store.record_question(
        question="q", answer=None, sql=None, ok=False, error="syntax error"
    )

    assert store.stats()["success_rate"] == 0.75


def test_a_history_of_nothing_but_clarifications_is_not_a_zero_percent_system(tmp_path):
    """Every turn excluded leaves nothing to divide by. Zero attempts is not
    zero successes."""
    from sqlagent.clarify import CLARIFICATION_ERROR
    from sqlagent.store import Store

    store = Store(tmp_path / "s.db")
    store.record_question(
        question="vague", answer="which?", sql=None, ok=False, error=CLARIFICATION_ERROR
    )

    stats = store.stats()

    assert stats["success_rate"] == 0.0
    assert stats["clarified"] == 1


def test_recent_turns_come_back_newest_first(tmp_path):
    """Drift splits this list by position. Reversed, it compares the baseline
    against itself and reports calm."""
    from sqlagent.store import Store

    store = Store(tmp_path / "s.db")
    for index in range(5):
        store.record_question(question=f"q{index}", answer="a", sql="SELECT 1", ok=True)

    assert [t.question for t in store.recent_turns(limit=3)] == ["q4", "q3", "q2"]
