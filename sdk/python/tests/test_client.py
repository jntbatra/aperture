import json

import httpx
import pytest

from aperture_sql import Aperture, ApertureError

RAW = {
    "question": "How many orders?", "answer": "There are 3 orders.", "conversation_id": "c1",
    "sql": "SELECT count(*) FROM orders", "columns": ["count"], "rows": [[3]], "row_count": 1,
    "truncated": False, "ok": True, "error": None, "repairs": 0, "model_calls": 2,
    "input_tokens": 2900, "output_tokens": 110, "seconds": 3.1, "tables_considered": ["orders"],
    "clarification_asks": [], "warnings": [],
}


def client(status=200, payload=RAW, **kwargs):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, json=payload)

    return Aperture("http://agent:8000/", transport=httpx.MockTransport(handler), **kwargs), calls


def test_cheap_is_the_default():
    c, calls = client()
    c.ask("How many orders?")
    body = json.loads(calls[0].content)
    assert str(calls[0].url) == "http://agent:8000/api/ask"
    assert body["options"]["check_result_intent"] is False


def test_expensive_turns_on_the_intent_check():
    c, calls = client()
    answer = c.ask("q", tier="expensive")
    assert json.loads(calls[0].content)["options"]["check_result_intent"] is True
    assert answer.tier == "expensive"


def test_api_key_is_a_bearer_token():
    c, calls = client(api_key="k" * 24)
    c.ask("q")
    assert calls[0].headers["authorization"] == "Bearer " + "k" * 24


def test_no_key_sends_no_authorization():
    c, calls = client()
    c.ask("q")
    assert "authorization" not in calls[0].headers


def test_the_answer_is_mapped():
    c, _ = client()
    a = c.ask("How many orders?", dataset_id="d1", conversation_id="c1", ambiguity="guess")
    assert (a.answer, a.rows, a.conversation_id, a.ok) == ("There are 3 orders.", [[3]], "c1", True)


def test_query_reruns_a_statement():
    c, calls = client(payload={"columns": ["n"], "rows": [[3]], "row_count": 1, "truncated": False, "seconds": 0.01})
    r = c.query("SELECT 3 AS n", dataset_id="d1")
    assert json.loads(calls[0].content) == {"sql": "SELECT 3 AS n", "dataset_id": "d1"}
    assert r.rows == [[3]] and r.row_count == 1


def test_a_refused_statement_raises_with_the_reason():
    c, _ = client(400, {"detail": "Only SELECT is allowed"})
    with pytest.raises(ApertureError) as err:
        c.query("DELETE FROM t")
    assert err.value.status == 400 and "SELECT" in str(err.value)


def test_schema_sends_the_dataset_only_when_given():
    c, calls = client(payload={"tables": []})
    c.schema()
    c.schema(dataset_id="d1")
    assert "dataset_id" not in str(calls[0].url)
    assert str(calls[1].url).endswith("?dataset_id=d1")


def test_upload_sends_the_file(tmp_path):
    f = tmp_path / "t.csv"
    f.write_text("a,b\n1,2\n")
    c, calls = client(payload={"id": "d1"})
    assert c.upload_dataset(f) == {"id": "d1"}
    assert b"filename=\"t.csv\"" in calls[0].content


def test_unknown_tier_is_refused():
    with pytest.raises(ValueError):
        Aperture("http://x", tier="free")  # type: ignore[arg-type]
