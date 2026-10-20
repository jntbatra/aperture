"""Python client for an Aperture server.

    from aperture_sql import Aperture

    client = Aperture("http://127.0.0.1:8000", api_key="...")
    answer = client.ask("How many orders last month?")
    print(answer.answer, answer.sql, answer.rows)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

import httpx

__all__ = ["Aperture", "ApertureError", "Answer", "QueryResult", "TIER_OPTIONS"]
__version__ = "0.2.0"

Tier = Literal["cheap", "expensive"]

# What each tier sends. `expensive` adds the result-intent check: a model reads
# the rows and asks whether they answer the question, and rewrites once if not.
TIER_OPTIONS: dict[str, dict[str, Any]] = {
    "cheap": {"quality_tier": "fast", "check_result_intent": False},
    "expensive": {"quality_tier": "fast", "check_result_intent": True},
}


class ApertureError(Exception):
    """The request failed. ``status`` is the HTTP status, or 0 if no response arrived."""

    def __init__(self, message: str, status: int = 0, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass
class Answer:
    question: str
    answer: str
    conversation_id: Optional[str]
    sql: Optional[str]
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    ok: bool
    error: Optional[str]
    clarifications: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    tables_considered: list[str] = field(default_factory=list)
    tier: str = "cheap"
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    seconds: float


class Aperture:
    """A client for one Aperture server.

    Args:
        base_url: e.g. ``http://127.0.0.1:8000``. No trailing ``/api``.
        api_key: A key listed in the server's ``SQLAGENT_API_KEYS``. Not needed
            when the server runs with ``SQLAGENT_AUTH_REQUIRED=false``.
        tier: Default tier, ``"cheap"`` or ``"expensive"``.
        timeout: Seconds per request. The expensive tier can take 10s or more.
        transport: An ``httpx`` transport, for tests.
    """

    def __init__(
        self,
        base_url: str,
        *,
        api_key: Optional[str] = None,
        tier: Tier = "cheap",
        timeout: float = 60.0,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        if tier not in TIER_OPTIONS:
            raise ValueError(f"unknown tier: {tier}")
        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._tier = tier
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"), headers=headers, timeout=timeout, transport=transport
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "Aperture":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def ask(
        self,
        question: str,
        *,
        tier: Optional[Tier] = None,
        conversation_id: Optional[str] = None,
        dataset_id: Optional[str] = None,
        ambiguity: Literal["ask", "guess"] = "ask",
    ) -> Answer:
        """Ask in English. Returns even when ``ok`` is False; raises on HTTP errors.

        ``conversation_id`` continues a thread ("and for April?"). ``ambiguity="guess"``
        always answers, which suits batch jobs where nobody can reply to a question.
        """
        tier = tier or self._tier
        if tier not in TIER_OPTIONS:
            raise ValueError(f"unknown tier: {tier}")
        raw = self._request(
            "POST",
            "/api/ask",
            json={
                "question": question,
                "conversation_id": conversation_id,
                "dataset_id": dataset_id,
                "options": {
                    **TIER_OPTIONS[tier],
                    "ambiguity_handling": "best_effort" if ambiguity == "guess" else "ask_human",
                },
            },
        )
        return Answer(
            question=raw["question"],
            answer=raw["answer"],
            conversation_id=raw.get("conversation_id"),
            sql=raw.get("sql"),
            columns=raw["columns"],
            rows=raw["rows"],
            row_count=raw["row_count"],
            truncated=raw["truncated"],
            ok=raw["ok"],
            error=raw.get("error"),
            clarifications=raw.get("clarification_asks") or [],
            warnings=raw.get("warnings") or [],
            tables_considered=raw.get("tables_considered") or [],
            tier=tier,
            model_calls=raw.get("model_calls", 0),
            input_tokens=raw.get("input_tokens", 0),
            output_tokens=raw.get("output_tokens", 0),
            seconds=raw.get("seconds", 0.0),
        )

    def query(self, sql: str, *, dataset_id: Optional[str] = None) -> QueryResult:
        """Run a statement again, read-only, with no model call.

        It passes the same checks the agent's own queries do: one SELECT, known
        tables, a row cap and a timeout. A refused statement raises ``ApertureError``
        with status 400.
        """
        raw = self._request("POST", "/api/query", json={"sql": sql, "dataset_id": dataset_id})
        return QueryResult(
            columns=raw["columns"],
            rows=raw["rows"],
            row_count=raw["row_count"],
            truncated=raw["truncated"],
            seconds=raw["seconds"],
        )

    def schema(self, *, dataset_id: Optional[str] = None) -> dict[str, Any]:
        """The tables and columns the agent can see."""
        return self._request("GET", "/api/schema", params={"dataset_id": dataset_id})

    def health(self, *, dataset_id: Optional[str] = None) -> dict[str, Any]:
        return self._request("GET", "/api/health", params={"dataset_id": dataset_id})

    def upload_dataset(self, path: "str | Path") -> dict[str, Any]:
        """Upload a CSV, Excel file or plain-SQL pg_dump. Returns the dataset (use its ``id``)."""
        file = Path(path)
        with file.open("rb") as handle:
            return self._request("POST", "/api/datasets", files={"file": (file.name, handle)})

    def list_datasets(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/datasets")

    def delete_dataset(self, dataset_id: str) -> None:
        self._request("DELETE", f"/api/datasets/{dataset_id}")

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        params = kwargs.get("params")
        if params is not None:
            kwargs["params"] = {k: v for k, v in params.items() if v is not None}
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ApertureError(f"aperture request failed: {exc}") from exc
        try:
            body = response.json() if response.content else None
        except ValueError:
            body = response.text
        if response.is_error:
            detail = body.get("detail") if isinstance(body, dict) else str(body)[:300]
            raise ApertureError(f"aperture {response.status_code}: {detail}", response.status_code, body)
        return body
