"""Tests for the Bedrock Mantle client.

Unit tests use ``httpx.MockTransport``, which intercepts requests inside httpx
and returns canned responses. That exercises the real client code — payload
construction, retry logic, error classification, response parsing — without a
network call or AWS credentials.

The live test at the bottom is marked ``integration`` and skips itself when
credentials are absent.
"""

from __future__ import annotations

import json

import httpx
import pytest

from sqlagent.config import Settings
from sqlagent.llm.mantle import (
    MantleClient,
    MantleError,
    ModelUnavailableError,
    extract_sql,
    json_from_reply,
)


def make_client(handler, *, max_retries: int = 4) -> MantleClient:
    """Build a client whose HTTP layer is a stub."""
    config = Settings(max_retries=max_retries, mantle_base_url="https://mantle.test/v1")
    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(base_url=config.base_url, transport=transport)
    return MantleClient(config, client=http_client)


def completion_payload(text: str = "SELECT 1", *, prompt_tokens=10, completion_tokens=5) -> dict:
    """A minimal OpenAI-shaped chat completion response."""
    return {
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


# --------------------------------------------------------------------------
# Request construction
# --------------------------------------------------------------------------


def test_sends_prompt_as_user_message():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=completion_payload())

    make_client(handler).complete("How many customers?")

    assert captured["messages"] == [{"role": "user", "content": "How many customers?"}]


def test_system_message_precedes_user_message():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=completion_payload())

    make_client(handler).complete("question", system="You write SQL.")

    assert [m["role"] for m in captured["messages"]] == ["system", "user"]


def test_uses_max_completion_tokens_not_max_tokens():
    """Mantle follows the current OpenAI schema.

    Some models on this endpoint reject the legacy ``max_tokens`` name, so
    sending the wrong one fails at runtime for a subset of models only — the
    kind of bug that hides until a model swap.
    """
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=completion_payload())

    make_client(handler).complete("hi", max_tokens=42)

    assert captured["max_completion_tokens"] == 42
    assert "max_tokens" not in captured


def test_defaults_to_light_model():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=completion_payload())

    client = make_client(handler)
    client.complete("hi")

    assert captured["model"] == Settings().light_model


def test_temperature_defaults_to_zero_for_determinism():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=completion_payload())

    make_client(handler).complete("hi")

    assert captured["temperature"] == 0.0


# --------------------------------------------------------------------------
# Response handling
# --------------------------------------------------------------------------


def test_returns_text_and_token_counts():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=completion_payload("SELECT 1", prompt_tokens=120, completion_tokens=8)
        )

    result = make_client(handler).complete("hi")

    assert result.text == "SELECT 1"
    assert result.input_tokens == 120
    assert result.output_tokens == 8
    assert result.total_tokens == 128
    assert result.seconds >= 0


def test_strips_surrounding_whitespace():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=completion_payload("\n\n  SELECT 1  \n"))

    assert make_client(handler).complete("hi").text == "SELECT 1"


def test_missing_usage_block_defaults_to_zero():
    """Not every model returns usage; accounting must not crash the request."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    result = make_client(handler).complete("hi")
    assert result.input_tokens == 0


def test_null_content_becomes_empty_string():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": None}}]})

    assert make_client(handler).complete("hi").text == ""


def test_unexpected_response_shape_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    with pytest.raises(MantleError, match="Unexpected response shape"):
        make_client(handler).complete("hi")


# --------------------------------------------------------------------------
# Errors and retries
# --------------------------------------------------------------------------


def test_model_not_entitled_raises_specific_error():
    """Distinct from a generic failure: retrying cannot fix entitlement."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={
                "error": {
                    "message": "anthropic.claude-sonnet-5 is not available for this account"
                }
            },
        )

    with pytest.raises(ModelUnavailableError, match="not available for this AWS account"):
        make_client(handler).complete("hi", model="anthropic.claude-sonnet-5")


def test_client_error_is_not_retried():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, json={"error": {"message": "bad request"}})

    with pytest.raises(MantleError, match="HTTP 400"):
        make_client(handler).complete("hi")

    assert calls == 1


def test_throttling_is_retried_then_succeeds(monkeypatch):
    monkeypatch.setattr("sqlagent.llm.mantle.time.sleep", lambda _: None)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(429, json={"error": {"message": "slow down"}})
        return httpx.Response(200, json=completion_payload("SELECT 2"))

    assert make_client(handler).complete("hi").text == "SELECT 2"
    assert calls == 3


def test_server_error_is_retried(monkeypatch):
    monkeypatch.setattr("sqlagent.llm.mantle.time.sleep", lambda _: None)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, text="unavailable")

    with pytest.raises(MantleError, match="failed after 3 attempts"):
        make_client(handler, max_retries=3).complete("hi")

    assert calls == 3


def test_connection_error_is_retried(monkeypatch):
    monkeypatch.setattr("sqlagent.llm.mantle.time.sleep", lambda _: None)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("connection reset")

    with pytest.raises(MantleError, match="failed after 2 attempts"):
        make_client(handler, max_retries=2).complete("hi")

    assert calls == 2


def test_probe_model_reports_failure_reason():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"error": {"message": "model x is not available for this account"}}
        )

    ok, reason = make_client(handler).probe_model("model-x")

    assert ok is False
    assert "not available" in reason


def test_probe_model_reports_success():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=completion_payload("ok"))

    assert make_client(handler).probe_model("qwen.qwen3-coder-next") == (True, "")


def test_list_models_returns_data_array():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "a"}, {"id": "b"}]})

    assert [m["id"] for m in make_client(handler).list_models()] == ["a", "b"]


# --------------------------------------------------------------------------
# Reply parsing helpers
# --------------------------------------------------------------------------


def test_extract_sql_passes_through_bare_sql():
    assert extract_sql("SELECT 1") == "SELECT 1"


def test_extract_sql_strips_markdown_fences():
    """ministral-3-8b wraps output in fences regardless of instructions."""
    assert extract_sql("```sql\nSELECT 1\n```") == "SELECT 1"


def test_extract_sql_strips_plain_fences():
    assert extract_sql("```\nSELECT 1\n```") == "SELECT 1"


def test_extract_sql_ignores_prose_around_a_fenced_block():
    reply = "Here is the query:\n```sql\nSELECT count(*) FROM orders\n```\nHope that helps!"
    assert extract_sql(reply) == "SELECT count(*) FROM orders"


def test_extract_sql_removes_trailing_semicolon():
    """Statements are wrapped in subqueries later; a semicolon breaks that."""
    assert extract_sql("SELECT 1;") == "SELECT 1"


def test_extract_sql_preserves_internal_structure():
    sql = "SELECT a,\n       b\nFROM t\nWHERE x = 1"
    assert extract_sql(f"```sql\n{sql}\n```") == sql


def test_json_from_reply_parses_bare_object():
    assert json_from_reply('{"scope": "in_scope"}') == {"scope": "in_scope"}


def test_json_from_reply_strips_fences():
    assert json_from_reply('```json\n{"a": 1}\n```') == {"a": 1}


def test_json_from_reply_ignores_surrounding_prose():
    assert json_from_reply('Sure!\n{"a": 1}\nLet me know.') == {"a": 1}


def test_json_from_reply_without_object_raises():
    with pytest.raises(MantleError, match="No JSON object"):
        json_from_reply("I cannot answer that.")


def test_json_from_reply_with_malformed_json_raises():
    with pytest.raises(MantleError, match="Malformed JSON"):
        json_from_reply('{"a": }')


# --------------------------------------------------------------------------
# Integration: a real call to Mantle
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_live_call_against_mantle():
    """Proves SigV4 signing works end to end against the real endpoint.

    Skips when AWS credentials are unavailable, so CI without secrets stays
    green.
    """
    boto3 = pytest.importorskip("boto3")
    if boto3.Session().get_credentials() is None:
        pytest.skip("no AWS credentials configured")

    with MantleClient() as client:
        result = client.complete(
            "Reply with exactly: ok",
            max_tokens=16,
        )

    assert result.text
    assert result.input_tokens > 0
