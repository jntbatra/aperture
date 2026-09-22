"""Model access: a thin client over Amazon Bedrock Mantle.

Mantle speaks OpenAI's request/response shape, so this layer is small — build
a payload, sign it with AWS SigV4, post it, read the text back out.
"""

from sqlagent.llm.mantle import (
    Completion,
    MantleClient,
    MantleError,
    ModelUnavailableError,
    SigV4Auth,
    extract_sql,
    json_from_reply,
)

__all__ = [
    "Completion",
    "MantleClient",
    "MantleError",
    "ModelUnavailableError",
    "SigV4Auth",
    "extract_sql",
    "json_from_reply",
]
