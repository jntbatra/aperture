"""Tests for the HTTP API.

FastAPI's ``TestClient`` runs the application in-process — no server to start,
no port to bind — while still exercising real routing, validation and
serialisation.

The agent is replaced with a stub via FastAPI's dependency override, so these
tests cover the HTTP layer only: status codes, response shapes, validation,
and the Server-Sent Events framing. Whether the agent gives good answers is
what ``test_pipeline.py`` and the benchmark are for.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import sqlagent.api.app as api
from sqlagent.api.app import app, get_agent
from sqlagent.db.execute import QueryResult
from sqlagent.pipeline import AgentResult, Attempt, Trace


class StubSnapshot:
    def __init__(self, tables: dict):
        self.tables = tables
        self.version = "testversion0001"

    def __len__(self) -> int:
        return len(self.tables)


class StubTable:
    def __init__(self, name, columns, pk, fks):
        self.name = name
        self.columns = columns
        self.primary_key = pk
        self.foreign_keys = fks


class StubColumn:
    def __init__(self, name, type_):
        self.name = name
        self.type = type_


class StubForeignKey:
    def __init__(self, target):
        self.target_table = target


class StubAgent:
    """Stands in for SqlAgent. Returns a fixed answer and records progress."""

    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.snapshot = StubSnapshot(
            {
                "customers": StubTable(
                    "customers",
                    [StubColumn("id", "INTEGER"), StubColumn("name", "TEXT")],
                    ["id"],
                    [],
                ),
                "orders": StubTable(
                    "orders",
                    [StubColumn("id", "INTEGER"), StubColumn("customer_id", "INTEGER")],
                    ["id"],
                    [StubForeignKey("customers")],
                ),
            }
        )

    def ask(
        self, question: str, *, history=(), options=None, on_progress=None
    ) -> AgentResult:
        self.seen_options = options
        # Recorded so a test can assert the API loaded the thread's turns and
        # passed them down, which is the whole mechanism behind follow-ups.
        self.seen_history = list(history)
        if on_progress:
            on_progress("seeds", {"message": "looking"})
            on_progress("generating", {"tables": ["customers"]})
            on_progress("executing", {"sql": "SELECT count(*) FROM customers"})

        trace = Trace(question=question)
        trace.candidate_tables = ["customers"]
        trace.input_tokens = 100
        trace.output_tokens = 20
        trace.model_calls = 2
        trace.seconds = 1.25

        if self.fail:
            trace.attempts = [Attempt("bad sql", False, "boom", "syntax")]
            return AgentResult(
                question=question,
                answer="I could not produce a working query.",
                sql=None,
                result=None,
                trace=trace,
                error="syntax error",
            )

        trace.attempts = [Attempt("SELECT count(*) FROM customers", True)]
        return AgentResult(
            question=question,
            answer="There are 3 customers.",
            sql="SELECT count(*) FROM customers",
            result=QueryResult(
                columns=("count",), rows=((3,),), seconds=0.01, truncated=False
            ),
            trace=trace,
        )


def _install(monkeypatch, stub: StubAgent, tmp_path):
    """Swap in a stub agent and an isolated store.

    Two mechanisms are needed. The read-only routes take the agent through
    FastAPI's dependency system, so `dependency_overrides` reaches them. The
    ask routes resolve it themselves — they have to, because the dataset comes
    from the request body — so those are patched at the module level.

    The store is redirected to a temporary file so tests never append to a real
    history database.
    """
    app.dependency_overrides[get_agent] = lambda: stub
    monkeypatch.setattr(api, "get_agent", lambda: stub)
    monkeypatch.setattr(api, "resolve_agent", lambda dataset_id=None: stub)

    from sqlagent.store import Store

    store = Store(tmp_path / "test-store.db")
    monkeypatch.setattr(api, "get_store", lambda: store)
    return store


@pytest.fixture
def client(monkeypatch, tmp_path):
    _install(monkeypatch, StubAgent(), tmp_path)
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def client_and_agent(monkeypatch, tmp_path):
    """The client plus the stub it talks to.

    Conversation tests have to assert on what reached the *agent* — that the
    thread's turns were loaded and passed down — which the HTTP response alone
    does not reveal.
    """
    stub = StubAgent()
    _install(monkeypatch, stub, tmp_path)
    with TestClient(app) as test_client:
        yield test_client, stub
    app.dependency_overrides.clear()


@pytest.fixture
def failing_client(monkeypatch, tmp_path):
    _install(monkeypatch, StubAgent(fail=True), tmp_path)
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------
# Health and schema
# --------------------------------------------------------------------------


def test_health_reports_table_count_and_models(client):
    response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["tables"] == 2
    assert body["schema_version"] == "testversion0001"
    assert body["light_model"]


def test_schema_lists_tables_with_columns(client):
    response = client.get("/api/schema")

    assert response.status_code == 200
    body = response.json()
    assert body["table_count"] == 2
    names = [table["name"] for table in body["tables"]]
    assert names == ["customers", "orders"]


def test_schema_reports_primary_keys_and_references(client):
    tables = {t["name"]: t for t in client.get("/api/schema").json()["tables"]}

    assert tables["customers"]["primary_key"] == ["id"]
    assert tables["orders"]["references"] == ["customers"]


def test_schema_renders_column_types(client):
    tables = {t["name"]: t for t in client.get("/api/schema").json()["tables"]}
    assert "id INTEGER" in tables["customers"]["columns"]


# --------------------------------------------------------------------------
# Asking a question
# --------------------------------------------------------------------------


def test_ask_returns_answer_sql_and_rows(client):
    response = client.post("/api/ask", json={"question": "How many customers?"})

    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "There are 3 customers."
    assert body["sql"] == "SELECT count(*) FROM customers"
    assert body["columns"] == ["count"]
    assert body["rows"] == [[3]]
    assert body["ok"] is True


def test_ask_includes_diagnostics(client):
    """The UI shows these, and evaluation depends on them."""
    body = client.post("/api/ask", json={"question": "How many?"}).json()

    assert body["model_calls"] == 2
    assert body["input_tokens"] == 100
    assert body["output_tokens"] == 20
    assert body["seconds"] == 1.25
    assert body["tables_considered"] == ["customers"]


def test_failed_question_is_a_200_with_ok_false(client, failing_client):
    """A question that cannot be answered is a normal outcome, not an HTTP error.

    Returning 500 would make ordinary "I could not work that out" responses
    indistinguishable from the server being broken, both for the UI and for
    monitoring.
    """
    response = failing_client.post("/api/ask", json={"question": "impossible"})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["error"]
    assert body["sql"] is None


# --------------------------------------------------------------------------
# Request validation
# --------------------------------------------------------------------------


def test_missing_question_is_rejected(client):
    assert client.post("/api/ask", json={}).status_code == 422


def test_empty_question_is_rejected(client):
    assert client.post("/api/ask", json={"question": ""}).status_code == 422


def test_absurdly_long_question_is_rejected(client):
    """An unbounded field is a cheap way to run up someone else's token bill."""
    response = client.post("/api/ask", json={"question": "x" * 5000})
    assert response.status_code == 422


def test_wrong_type_is_rejected(client):
    assert client.post("/api/ask", json={"question": 42}).status_code == 422


# --------------------------------------------------------------------------
# Streaming
# --------------------------------------------------------------------------


def test_stream_emits_stage_events_then_a_result(client):
    with client.stream(
        "GET", "/api/ask/stream", params={"question": "How many customers?"}
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(response.iter_text())

    # Stage events arrive before the final result.
    assert "event: seeds" in body
    assert "event: generating" in body
    assert "event: executing" in body
    assert "event: result" in body
    assert body.index("event: seeds") < body.index("event: result")


def test_stream_frames_are_correctly_terminated(client):
    """Each SSE frame ends with a blank line, or the browser never dispatches it."""
    with client.stream("GET", "/api/ask/stream", params={"question": "test"}) as response:
        body = "".join(response.iter_text())

    assert "\n\n" in body
    for frame in filter(None, body.split("\n\n")):
        assert frame.startswith("event: ")
        assert "\ndata: " in frame


def test_stream_result_frame_carries_the_full_payload(client):
    import json

    with client.stream("GET", "/api/ask/stream", params={"question": "test"}) as response:
        body = "".join(response.iter_text())

    result_frame = [f for f in body.split("\n\n") if f.startswith("event: result")][0]
    payload = json.loads(result_frame.split("data: ", 1)[1])

    assert payload["answer"] == "There are 3 customers."
    assert payload["sql"] == "SELECT count(*) FROM customers"


def test_stream_rejects_an_empty_question(client):
    response = client.get("/api/ask/stream", params={"question": "   "})
    assert response.status_code == 400


def test_stream_sets_headers_that_prevent_proxy_buffering(client):
    """Without this a reverse proxy holds every event until the request ends,
    which defeats streaming entirely."""
    with client.stream("GET", "/api/ask/stream", params={"question": "test"}) as response:
        assert response.headers.get("x-accel-buffering") == "no"
        assert response.headers.get("cache-control") == "no-cache"


# --------------------------------------------------------------------------
# Conversations
#
# The point of these: a question like "and for April?" is not a question about
# a database at all until the previous turn is attached to it. These check that
# the attaching happens, over HTTP, end to end.
# --------------------------------------------------------------------------


def test_asking_without_a_thread_creates_one(client):
    response = client.post("/api/ask", json={"question": "How many customers?"})

    assert response.status_code == 200
    assert response.json()["conversation_id"]


def test_the_first_question_in_a_thread_gets_no_history(client_and_agent):
    """A first question must reach the agent exactly as it did before."""
    client, stub = client_and_agent

    client.post("/api/ask", json={"question": "How many customers?"})

    assert stub.seen_history == []


def test_a_follow_up_receives_the_previous_turn(client_and_agent):
    client, stub = client_and_agent

    first = client.post("/api/ask", json={"question": "How many orders in March?"})
    thread = first.json()["conversation_id"]

    client.post(
        "/api/ask",
        json={"question": "and for April?", "conversation_id": thread},
    )

    assert [turn.question for turn in stub.seen_history] == ["How many orders in March?"]
    assert stub.seen_history[0].sql  # the SQL is what makes the follow-up answerable


def test_turns_reach_the_agent_oldest_first(client_and_agent):
    client, stub = client_and_agent

    thread = client.post("/api/ask", json={"question": "one"}).json()["conversation_id"]
    client.post("/api/ask", json={"question": "two", "conversation_id": thread})
    client.post("/api/ask", json={"question": "three", "conversation_id": thread})

    assert [turn.question for turn in stub.seen_history] == ["one", "two"]


def test_separate_threads_do_not_see_each_other(client_and_agent):
    client, stub = client_and_agent

    other = client.post("/api/ask", json={"question": "about refunds"}).json()
    client.post("/api/ask", json={"question": "about shipping"})

    # A second question in the *refunds* thread must not see the shipping one.
    client.post(
        "/api/ask",
        json={"question": "and last month?", "conversation_id": other["conversation_id"]},
    )

    assert [turn.question for turn in stub.seen_history] == ["about refunds"]


def test_an_unknown_thread_id_starts_a_new_one_rather_than_404ing(client):
    """A stale id in a browser tab should cost the user nothing."""
    response = client.post(
        "/api/ask", json={"question": "hello", "conversation_id": "does-not-exist"}
    )

    assert response.status_code == 200
    assert response.json()["conversation_id"] != "does-not-exist"


def test_the_stream_threads_a_conversation_too(client_and_agent):
    client, stub = client_and_agent

    thread = client.post("/api/ask", json={"question": "How many orders?"}).json()[
        "conversation_id"
    ]

    with client.stream(
        "GET", f"/api/ask/stream?question=and%20for%20April%3F&conversation_id={thread}"
    ) as response:
        body = "".join(response.iter_text())

    assert thread in body
    assert [turn.question for turn in stub.seen_history] == ["How many orders?"]


def test_a_thread_can_be_read_back_as_a_transcript(client):
    """What the UI calls after a reload, so the thread survives a refresh."""
    thread = client.post("/api/ask", json={"question": "first"}).json()["conversation_id"]
    client.post("/api/ask", json={"question": "second", "conversation_id": thread})

    response = client.get(f"/api/conversations/{thread}")

    assert response.status_code == 200
    payload = response.json()
    assert [entry["question"] for entry in payload["entries"]] == ["first", "second"]
    assert payload["conversation"]["turns"] == 2


def test_a_thread_is_titled_by_its_first_question(client):
    thread = client.post("/api/ask", json={"question": "How many refunds?"}).json()[
        "conversation_id"
    ]
    client.post("/api/ask", json={"question": "and for April?", "conversation_id": thread})

    title = client.get(f"/api/conversations/{thread}").json()["conversation"]["title"]

    assert title == "How many refunds?"


def test_threads_are_listed_most_recent_first(client):
    client.post("/api/ask", json={"question": "older"})
    client.post("/api/ask", json={"question": "newer"})

    listed = client.get("/api/conversations").json()

    assert [item["title"] for item in listed] == ["newer", "older"]


def test_an_empty_thread_can_be_created_for_the_new_chat_button(client):
    response = client.post("/api/conversations", json={})

    assert response.status_code == 200
    assert response.json()["turns"] == 0


def test_deleting_a_thread_removes_it_and_its_turns(client):
    thread = client.post("/api/ask", json={"question": "delete me"}).json()[
        "conversation_id"
    ]

    assert client.delete(f"/api/conversations/{thread}").status_code == 200
    assert client.get(f"/api/conversations/{thread}").status_code == 404
    assert client.get("/api/search?q=delete").json() == []


def test_deleting_a_missing_thread_is_a_404(client):
    assert client.delete("/api/conversations/nope").status_code == 404


def test_reading_a_missing_thread_is_a_404(client):
    assert client.get("/api/conversations/nope").status_code == 404


def test_search_finds_a_past_question_and_names_its_thread(client):
    """The one part of the removed history view worth keeping."""
    thread = client.post("/api/ask", json={"question": "Total revenue by region"}).json()[
        "conversation_id"
    ]

    hits = client.get("/api/search?q=revenue").json()

    assert len(hits) == 1
    assert hits[0]["conversation_id"] == thread
    assert hits[0]["conversation_title"] == "Total revenue by region"


def test_search_with_no_match_is_empty(client):
    client.post("/api/ask", json={"question": "anything"})

    assert client.get("/api/search?q=zzzz").json() == []


def test_search_rejects_an_empty_term(client):
    assert client.get("/api/search?q=").status_code == 422


def test_stats_are_exposed_separately(client):
    client.post("/api/ask", json={"question": "one"})

    body = client.get("/api/stats").json()

    assert body["total"] == 1
    assert body["success_rate"] == 1.0


def test_the_flat_history_endpoint_is_gone(client):
    """Removed deliberately: half its entries were fragments with no subject."""
    assert client.get("/api/history").status_code == 404


# --------------------------------------------------------------------------
# Per-question options
# --------------------------------------------------------------------------


def test_options_reach_the_agent(client_and_agent):
    client, stub = client_and_agent

    client.post(
        "/api/ask",
        json={"question": "q", "options": {"quality_tier": "thorough"}},
    )

    assert stub.seen_options == {"quality_tier": "thorough"}


def test_unset_options_are_not_sent(client_and_agent):
    """None means "use the server's setting". Sending it as an override would
    make every client have to know the full configuration."""
    client, stub = client_and_agent

    client.post("/api/ask", json={"question": "q", "options": {"use_critic": True}})

    assert stub.seen_options == {"use_critic": True}


def test_no_options_means_none(client_and_agent):
    client, stub = client_and_agent

    client.post("/api/ask", json={"question": "q"})

    assert stub.seen_options is None


def test_an_invalid_option_value_is_rejected(client):
    response = client.post(
        "/api/ask", json={"question": "q", "options": {"quality_tier": "extremely"}}
    )

    assert response.status_code == 422


def test_vote_samples_are_bounded(client):
    response = client.post(
        "/api/ask", json={"question": "q", "options": {"vote_samples": 99}}
    )

    assert response.status_code == 422


def test_a_setting_outside_the_allow_list_is_not_accepted(client_and_agent):
    """`row_limit` is not an AskOptions field, so it never reaches the agent."""
    client, stub = client_and_agent

    client.post("/api/ask", json={"question": "q", "options": {"row_limit": 999999}})

    assert "row_limit" not in (stub.seen_options or {})


def test_the_stream_accepts_options_as_json(client_and_agent):
    client, stub = client_and_agent

    with client.stream(
        "GET", '/api/ask/stream?question=q&options={"use_critic":true}'
    ) as response:
        "".join(response.iter_text())

    assert stub.seen_options == {"use_critic": True}


def test_the_stream_rejects_malformed_options(client):
    response = client.get("/api/ask/stream?question=q&options=not-json")

    assert response.status_code == 400


def test_the_options_endpoint_describes_every_toggle(client):
    body = client.get("/api/options").json()

    names = {toggle["name"] for toggle in body["toggles"]}
    assert "quality_tier" in names
    # `use_critic` was asserted here and has been removed from the page on
    # purpose: `quality_tier` decides it, and showing both contradicted the
    # engine. See `test_no_toggle_contradicts_the_tier`.
    assert "use_critic" not in names
    assert set(body["defaults"]) >= names


def test_every_toggle_states_its_cost(client):
    """A toggle offered without one invites switching everything on and
    concluding the tool is slow."""
    for toggle in client.get("/api/options").json()["toggles"]:
        assert toggle["cost"]
        assert len(toggle["help"]) > 40


def test_the_options_endpoint_reports_the_server_defaults(client):
    body = client.get("/api/options").json()

    assert body["defaults"]["quality_tier"] == "fast"
    assert body["defaults"]["use_critic"] is False


def test_warnings_reach_the_client(client_and_agent, monkeypatch):
    """A guard whose finding never reaches the person acting on the figure is
    not a guard. The fan-out check fired, logged, and told nobody."""
    _, stub = client_and_agent
    original = stub.ask

    def with_warning(*args, **kwargs):
        result = original(*args, **kwargs)
        result.trace.inflation_warnings = ["SUM(o.total) may be inflated"]
        return result

    monkeypatch.setattr(stub, "ask", with_warning)

    body = client_and_agent[0].post("/api/ask", json={"question": "q"}).json()

    assert body["warnings"] == ["SUM(o.total) may be inflated"]


def test_an_unverified_answer_is_flagged_to_the_client(client_and_agent, monkeypatch):
    _, stub = client_and_agent
    original = stub.ask

    def unverified(*args, **kwargs):
        result = original(*args, **kwargs)
        result.trace.answer_unverified = True
        return result

    monkeypatch.setattr(stub, "ask", unverified)

    body = client_and_agent[0].post("/api/ask", json={"question": "q"}).json()

    assert any("could not be reconciled" in w for w in body["warnings"])


def test_a_clean_answer_carries_no_warnings(client):
    assert client.post("/api/ask", json={"question": "q"}).json()["warnings"] == []


# --------------------------------------------------------------------------
# The toggle list is declared three times
#
# `config.OVERRIDABLE` is the allow-list, `AskOptions` is the request schema,
# and `TOGGLE_DESCRIPTIONS` is what the page draws. Adding a setting means
# editing all three by hand, and forgetting one fails silently in a different
# way each time: not overridable, rejected as an unknown field, or simply never
# shown to anyone.
# --------------------------------------------------------------------------


def test_every_overridable_setting_is_a_request_field():
    from sqlagent.api.app import AskOptions
    from sqlagent.config import OVERRIDABLE

    missing = OVERRIDABLE - set(AskOptions.model_fields)
    assert not missing, f"overridable but not sendable: {sorted(missing)}"


def test_every_request_field_is_overridable():
    """The allow-list is enforced again in `apply_overrides`, so a field here
    that is not there is silently dropped — a control that appears to work."""
    from sqlagent.api.app import AskOptions
    from sqlagent.config import OVERRIDABLE

    extra = set(AskOptions.model_fields) - OVERRIDABLE
    assert not extra, f"sendable but silently ignored: {sorted(extra)}"


DERIVED = {
    # Derived from the hop choice. Offering "start at 2, stop at 1" as a
    # reachable state would be offering a setting with no meaning.
    "max_hops",
    # Decided by `quality_tier`. Shown alongside it, these did not duplicate
    # the control, they contradicted it: with Care on *detailed* the panel
    # rendered the critic off, voting at 1 and decomposition off while the
    # engine ran all three. See `test_no_toggle_contradicts_the_tier`.
    "use_critic",
    "vote_samples",
    "decompose_questions",
}
"""Settings that are overridable over the API but deliberately not on the page.

Every entry needs a reason, and the reason is either "it has no independent
meaning" or "something else already decides it".
"""


def test_no_toggle_contradicts_the_tier():
    """No control may be shown whose value the tier overrides.

    This is the general form of a real bug. `quality_tier="thorough"` forces
    the critic, voting and decomposition on, and all three had their own
    controls in the panel. Left at their defaults they rendered as *off* while
    the engine ran them — a control displaying the opposite of what is
    happening, which is worse than no control at all.
    """
    from sqlagent.api.app import TOGGLE_DESCRIPTIONS
    from sqlagent.config import (
        Settings,
        critic_enabled,
        decompose_enabled,
        vote_samples,
    )

    shown = {toggle.name for toggle in TOGGLE_DESCRIPTIONS}
    lying = []

    for tier in ("fast", "medium", "thorough"):
        config = Settings(_env_file=None, database_url="sqlite://", quality_tier=tier)
        effective = {
            "use_critic": critic_enabled(config),
            "vote_samples": vote_samples(config),
            "decompose_questions": decompose_enabled(config),
        }
        for name, running in effective.items():
            if name in shown and getattr(config, name) != running:
                lying.append(
                    f"{name} shows {getattr(config, name)!r} "
                    f"but runs {running!r} at tier={tier}"
                )

    assert not lying, "controls contradicting the tier: " + "; ".join(lying)


def test_every_overridable_setting_is_described_to_the_user():
    from sqlagent.api.app import TOGGLE_DESCRIPTIONS
    from sqlagent.config import OVERRIDABLE

    described = {toggle.name for toggle in TOGGLE_DESCRIPTIONS}
    missing = OVERRIDABLE - described - DERIVED
    assert not missing, f"overridable but never shown: {sorted(missing)}"


def test_every_described_toggle_is_a_real_setting():
    from sqlagent.api.app import TOGGLE_DESCRIPTIONS
    from sqlagent.config import Settings

    for toggle in TOGGLE_DESCRIPTIONS:
        assert toggle.name in Settings.model_fields, toggle.name


def test_a_choice_toggle_offers_the_values_the_setting_accepts():
    """A choice the server rejects is a 422 the user cannot act on."""
    from sqlagent.api.app import TOGGLE_DESCRIPTIONS, AskOptions

    for toggle in TOGGLE_DESCRIPTIONS:
        if toggle.kind != "choice":
            continue
        for choice in toggle.choices:
            field = AskOptions.model_fields[toggle.name]
            value = int(choice) if field.annotation in (int | None,) else choice
            AskOptions(**{toggle.name: value})


def test_numeric_choice_toggles_are_declared_numeric():
    """The client sends what the server says the type is. Before this the
    client kept its own list of numeric names, and a new one sent `"4"` where
    the schema wanted `4` — a 422 with no way for the user to act on it."""
    from sqlagent.api.app import TOGGLE_DESCRIPTIONS, AskOptions

    for toggle in TOGGLE_DESCRIPTIONS:
        if toggle.kind != "choice":
            continue
        wants_int = AskOptions.model_fields[toggle.name].annotation == (int | None)
        assert toggle.numeric == wants_int, toggle.name


# --------------------------------------------------------------------------
# Drift
# --------------------------------------------------------------------------


@pytest.fixture
def client_and_store(monkeypatch, tmp_path):
    """The client plus the history database behind it.

    Drift is computed from stored questions, so a test has to be able to write
    a history before asking what changed.
    """
    store = _install(monkeypatch, StubAgent(), tmp_path)
    with TestClient(app) as test_client:
        yield test_client, store
    app.dependency_overrides.clear()


def record(store, count: int, *, ok: bool = True, seconds: float = 1.0):
    for index in range(count):
        store.record_question(
            question=f"q{index}",
            answer="a",
            sql="SELECT 1",
            ok=ok,
            error=None if ok else "boom",
            row_count=1,
            seconds=seconds,
            model_calls=2,
            tokens=500,
            repairs=0,
        )


def test_drift_on_an_empty_history_says_there_is_not_enough(client_and_store):
    """Not "no drift". A fresh install reporting calm is a claim nobody made."""
    test_client, _ = client_and_store

    payload = test_client.get("/api/drift").json()

    assert payload["enough_data"] is False
    assert payload["drifted"] is False
    assert "Not enough history" in payload["summary"]


def test_a_steady_history_reports_no_change(client_and_store):
    test_client, store = client_and_store
    record(store, 120)

    payload = test_client.get("/api/drift?recent=50&baseline=60").json()

    assert payload["enough_data"] is True
    assert payload["drifted"] is False
    assert payload["rates"]["failure_rate"] == [0.0, 0.0]


def test_a_surge_of_failures_is_reported(client_and_store):
    test_client, store = client_and_store
    record(store, 60)                 # the baseline, written first
    record(store, 50, ok=False)       # the recent period

    payload = test_client.get("/api/drift?recent=50&baseline=60").json()

    assert payload["drifted"] is True
    shift = next(s for s in payload["shifts"] if s["metric"] == "failure_rate")
    assert shift["worse"] is True
    assert shift["baseline"] == 0.0
    assert shift["recent"] == 1.0


def test_the_recent_period_is_the_newest_questions(client_and_store):
    """Reversed, this compares the baseline against itself and reports calm —
    which reads as good news."""
    test_client, store = client_and_store
    record(store, 60, ok=False)       # older
    record(store, 50)                 # newer

    payload = test_client.get("/api/drift?recent=50&baseline=60").json()

    shift = next(s for s in payload["shifts"] if s["metric"] == "failure_rate")
    assert shift["baseline"] == 1.0
    assert shift["recent"] == 0.0
    assert shift["worse"] is False


def test_an_absurd_window_is_rejected_rather_than_scanning_everything(client_and_store):
    test_client, _ = client_and_store

    assert test_client.get("/api/drift?recent=100000").status_code == 422
