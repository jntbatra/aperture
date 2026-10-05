"""Langfuse tracing: off without keys, never sends rows, redacts PII."""

from __future__ import annotations

import pytest

from sqlagent import observability
from sqlagent.config import Settings
from sqlagent.db.execute import QueryResult


@pytest.fixture(autouse=True)
def no_langfuse_keys(monkeypatch):
    """Every test starts with tracing unconfigured, whatever the developer's shell has."""
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.setattr(observability, "_dotenv_loaded", True)
    monkeypatch.setattr(observability, "_client", None)
    monkeypatch.setattr(observability, "_client_failed", False)


def config(**overrides) -> Settings:
    return Settings(_env_file=None, database_url="sqlite://", **overrides)


def test_without_keys_tracing_is_off_and_creates_no_client():
    assert not observability.enabled(config())
    with observability.observation(config(), "x") as span:
        span.update(output="ignored")
    assert observability._client is None


def test_tracing_enabled_false_wins_over_keys(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    assert observability.enabled(config())
    assert not observability.enabled(config(tracing_enabled=False))


def test_an_untraced_node_is_the_original_function():
    """Off means off: no wrapper, no per-step cost."""

    def node(state):
        return {"x": 1}

    assert observability.traced_node(config(), "n", "span", node) is node


def test_result_rows_are_replaced_by_their_shape():
    result = QueryResult(
        columns=("name", "email"),
        rows=(("Ada", "ada@example.com"), ("Bo", "bo@example.com")),
        seconds=0.1,
        truncated=False,
    )
    summary = observability.summarise_state({"result": result, "validated_sql": "SELECT 1"})

    assert summary["result"] == {"columns": ["name", "email"], "row_count": 2, "truncated": False}
    assert "ada@example.com" not in repr(summary)
    assert summary["validated_sql"] == "SELECT 1"


def test_the_trace_object_is_never_serialised():
    assert "trace" not in observability.summarise_state({"trace": object(), "sql": "x"})


def test_long_text_is_clipped():
    clipped = observability.clip("a" * (observability.MAX_TEXT + 500))
    assert len(clipped) < observability.MAX_TEXT + 100
    assert "500 more characters" in clipped


@pytest.mark.parametrize(
    "text, expected",
    [
        ("mail ada@example.com now", "mail [email] now"),
        ("call +91 98765 43210 today", "call [phone] today"),
        ("call 9876543210", "call [phone]"),
        ("SELECT count(*) FROM orders WHERE id = 42", "SELECT count(*) FROM orders WHERE id = 42"),
        ("revenue 12345.67 in 2026", "revenue 12345.67 in 2026"),
    ],
)
def test_redaction(text, expected):
    assert observability.redact(text) == expected


def test_the_masking_hook_patches_only_langfuse_io_attributes():
    from langfuse.types import MaskOtelSpansParams, OtelSpanData, OtelSpanIdentifier

    ident = OtelSpanIdentifier(trace_id="0" * 32, span_id="1" * 16)
    other = OtelSpanIdentifier(trace_id="0" * 32, span_id="2" * 16)

    def span(attributes):
        return OtelSpanData(
            trace_id="0" * 32,
            span_id="1" * 16,
            parent_span_id=None,
            name="s",
            instrumentation_scope_name="langfuse-sdk",
            instrumentation_scope_version=None,
            attributes=attributes,
            resource_attributes={},
        )

    params = MaskOtelSpansParams(
        spans={
            ident: span(
                {
                    "langfuse.observation.output": '{"customer": "ada@example.com"}',
                    "langfuse.observation.model.name": "ada@example.com",
                }
            ),
            other: span({"langfuse.observation.input": "nothing to hide"}),
        }
    )
    result = observability.mask_otel_spans(params=params)

    assert set(result.span_patches) == {ident}
    patch = result.span_patches[ident]
    assert patch.set_attributes == {"langfuse.observation.output": '{"customer": "[email]"}'}


# --------------------------------------------------------------------------
# With tracing on, exported to memory instead of Langfuse
# --------------------------------------------------------------------------


@pytest.fixture
def exported(monkeypatch):
    """A real Langfuse client whose spans land in a list, not on the network."""
    # A fresh public key per test: Langfuse keeps one client per key and would
    # otherwise hand back the previous test's, already shut down.
    import uuid

    from langfuse import Langfuse
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    public_key = f"pk-lf-memory-{uuid.uuid4().hex}"
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", public_key)
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-memory-test")
    memory = InMemorySpanExporter()
    client = Langfuse(
        public_key=public_key,
        secret_key="sk-lf-memory-test",
        base_url="http://127.0.0.1:9",
        span_exporter=memory,
        tracer_provider=TracerProvider(),
        mask_otel_spans=observability.mask_otel_spans,
    )
    monkeypatch.setattr(observability, "_client", client)
    yield memory, client
    client.shutdown()


def _spans(memory, client):
    client.flush()
    return {s.name: s for s in memory.get_finished_spans()}


def _kind(span):
    return span.attributes.get("langfuse.observation.type")


def test_a_question_becomes_one_trace_with_a_typed_step_per_node(exported, tmp_path):
    from sqlalchemy import create_engine, text
    from tests.test_toggles import ScriptedClient, build

    engine = create_engine(f"sqlite:///{tmp_path / 'o.db'}")
    with engine.begin() as c:
        c.execute(text("CREATE TABLE customers (id INTEGER PRIMARY KEY, email TEXT)"))
        c.execute(
            text("INSERT INTO customers VALUES (1, 'ada@example.com'), (2, 'bo@example.com')")
        )

    memory, client = exported
    agent = build(engine, ScriptedClient("SELECT email FROM customers", "Two customers."))
    agent.ask("List customer emails", session_id="thread-1", source="test")
    spans = _spans(memory, client)

    root = spans["answer-question"]
    assert _kind(root) == "agent"
    assert root.parent is None
    for name, kind in [
        ("screen-question", "guardrail"),
        ("select-tables", "retriever"),
        ("generate-sql", "chain"),
        ("execute-sql", "tool"),
        ("write-answer", "chain"),
    ]:
        assert name in spans, f"missing {name}: {sorted(spans)}"
        assert _kind(spans[name]) == kind
        assert spans[name].parent.span_id == root.context.span_id, name
        assert spans[name].context.trace_id == root.context.trace_id

    assert root.attributes.get("session.id") == "thread-1"
    assert "tier:cheap" in root.attributes.get("langfuse.trace.tags", ())

    # Rows never leave: execution reports shape, and the masking hook catches
    # the addresses in the answer prompt's sample.
    executed = spans["execute-sql"].attributes["langfuse.observation.output"]
    assert '"row_count": 2' in executed
    assert "ada@example.com" not in executed


def test_a_model_call_is_a_generation_with_model_messages_and_tokens(exported, monkeypatch):
    from sqlagent.llm.mantle import MantleClient

    memory, client = exported
    mantle = MantleClient(config(llm_base_url="http://127.0.0.1:9", llm_api_key="x"))
    monkeypatch.setattr(
        mantle,
        "_post",
        lambda path, payload: {
            "choices": [{"message": {"content": "SELECT 1"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 7},
        },
    )
    token = observability._current_step.set("generate-sql")
    try:
        mantle.complete("How many?", model="google.gemma-4-31b", system="Write SQL.")
    finally:
        observability._current_step.reset(token)

    found = _spans(memory, client)
    assert "generate-sql" in found, sorted(found)
    gen = found["generate-sql"]
    assert _kind(gen) == "generation"
    assert gen.attributes["langfuse.observation.model.name"] == "google.gemma-4-31b"
    usage = gen.attributes["langfuse.observation.usage_details"]
    assert '"input": 120' in usage and '"output": 7' in usage
    assert '"role": "system"' in gen.attributes["langfuse.observation.input"]
