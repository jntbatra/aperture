"""Bedrock chat model, wrapped so every call lands in the spend ledger."""

from __future__ import annotations

from langchain_aws import ChatBedrockConverse
from langchain_core.messages import BaseMessage

from .budget import LEDGER
from .config import settings


def build_llm(*, max_tokens: int | None = None, temperature: float | None = None):
    cfg = settings()
    return ChatBedrockConverse(
        model=cfg.bedrock_model_id,
        region_name=cfg.bedrock_region,
        max_tokens=max_tokens or cfg.max_tokens,
        temperature=cfg.temperature if temperature is None else temperature,
    )


def _usage_of(message: BaseMessage) -> tuple[int, int]:
    usage = getattr(message, "usage_metadata", None) or {}
    return int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))


async def ainvoke_metered(llm, messages: list, *, label: str) -> BaseMessage:
    """Invoke `llm`, record usage, and let BudgetExceeded propagate."""
    response = await llm.ainvoke(messages)
    tokens_in, tokens_out = _usage_of(response)
    LEDGER.record(tokens_in, tokens_out, label=label, model_id=settings().bedrock_model_id)
    return response


def invoke_metered(llm, messages: list, *, label: str) -> BaseMessage:
    response = llm.invoke(messages)
    tokens_in, tokens_out = _usage_of(response)
    LEDGER.record(tokens_in, tokens_out, label=label, model_id=settings().bedrock_model_id)
    return response
