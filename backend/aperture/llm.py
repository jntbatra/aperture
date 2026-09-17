"""Bedrock chat model, wrapped so every call lands in the spend ledger."""

from __future__ import annotations

import logging
from typing import AsyncIterator

from botocore.config import Config as BotoConfig
from langchain_aws import ChatBedrockConverse
from langchain_core.messages import AIMessageChunk, BaseMessage

from .budget import LEDGER
from .config import settings

log = logging.getLogger(__name__)


def build_llm(*, max_tokens: int | None = None, temperature: float | None = None):
    """Construct the chat model.

    Adaptive retries matter: an unattended benchmark run will hit Bedrock
    throttling, and the default retry policy gives up too early.
    """
    cfg = settings()
    boto_config = BotoConfig(
        retries={"max_attempts": cfg.bedrock_max_retries, "mode": "adaptive"},
        read_timeout=cfg.bedrock_read_timeout,
    )
    return ChatBedrockConverse(
        model_id=cfg.bedrock_model_id,
        region_name=cfg.bedrock_region,
        max_tokens=max_tokens or cfg.max_tokens,
        temperature=cfg.temperature if temperature is None else temperature,
        config=boto_config,
    )


def usage_of(message: BaseMessage) -> tuple[int, int]:
    usage = getattr(message, "usage_metadata", None) or {}
    return int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))


def stop_reason(message: BaseMessage) -> str:
    """`max_tokens` here means truncated output, which is never repairable."""
    meta = getattr(message, "response_metadata", None) or {}
    return str(meta.get("stopReason") or "")


def _record(message: BaseMessage, label: str) -> None:
    tokens_in, tokens_out = usage_of(message)
    if tokens_in == 0 and tokens_out == 0:
        # Silently recording zero would make the spend ceiling meaningless.
        log.warning("no usage metadata on %s response; spend is undercounted", label)
    LEDGER.record(tokens_in, tokens_out, label=label, model_id=settings().bedrock_model_id)


def invoke_metered(llm, messages: list, *, label: str) -> BaseMessage:
    response = llm.invoke(messages)
    _record(response, label)
    return response


async def ainvoke_metered(llm, messages: list, *, label: str) -> BaseMessage:
    response = await llm.ainvoke(messages)
    _record(response, label)
    return response


async def astream_metered(llm, messages: list, *, label: str) -> AsyncIterator[AIMessageChunk]:
    """Stream chunks, accumulating usage so streamed calls are billed too."""
    accumulated: AIMessageChunk | None = None
    async for chunk in llm.astream(messages):
        accumulated = chunk if accumulated is None else accumulated + chunk
        yield chunk
    if accumulated is not None:
        _record(accumulated, label)
