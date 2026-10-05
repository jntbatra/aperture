"""Client for Amazon Bedrock Mantle.

What Mantle is
--------------
Mantle is an AWS inference endpoint that speaks **OpenAI's API shape**. You POST
the same JSON you would send to OpenAI — ``{"model": ..., "messages": [...]}``
— to ``https://bedrock-mantle.<region>.api.aws/v1/chat/completions``, and get an
OpenAI-shaped response back. The models behind it are the ones AWS hosts
(Qwen, DeepSeek, GLM, Mistral, Llama and others), not OpenAI's.

How authentication works here
-----------------------------
AWS documents two ways in:

1. A **Bedrock API key**, sent as ``Authorization: Bearer <key>``. This is what
   the plain OpenAI SDK expects, and it is the path the AWS docs lead with.
2. **SigV4** — signing each request with ordinary AWS credentials, the same
   scheme every other AWS API uses.

This client uses SigV4, because it works with the credentials already in
``~/.aws/credentials`` and needs no extra secret to be minted, distributed or
rotated. Verified working against ``GET /v1/models`` and
``POST /v1/chat/completions``.

If you have not seen request signing before: SigV4 does not encrypt anything.
It computes an HMAC over the request's method, path, headers and body hash
using your secret key, and puts the result in the ``Authorization`` header. AWS
recomputes it on their side. Because the body is part of the signature, the
request cannot be altered in transit, and because the timestamp is included,
a captured request cannot be replayed indefinitely.

Model availability is per-account
---------------------------------
``GET /v1/models`` lists everything Mantle *hosts*, not everything **you** may
call. On this account, Anthropic and GPT-5 models appear in that list but return
``not available for this account`` when invoked. Availability is therefore
something to verify by calling, which :func:`probe_model` exists to do.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import boto3
import httpx
from botocore.auth import SigV4Auth as BotocoreSigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

from sqlagent import observability
from sqlagent.config import Settings, settings

logger = logging.getLogger(__name__)


class MantleError(RuntimeError):
    """A call to Mantle failed in a way the caller cannot retry around."""


class ModelUnavailableError(MantleError):
    """The model exists but this AWS account is not entitled to call it.

    Distinguished from other failures because the remedy is different: no amount
    of retrying helps, and the fix is to choose a different model or request
    access in the Bedrock console.
    """


@dataclass(frozen=True, slots=True)
class Completion:
    """One model response, plus the accounting needed to price it."""

    text: str
    model: str
    input_tokens: int
    output_tokens: int
    seconds: float

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class BedrockTokenAuth(httpx.Auth):
    """Bearer auth using a short-lived token minted from AWS credentials.

    Mantle's ``/openai/v1`` route does not accept SigV4; it wants a bearer token
    that ``aws_bedrock_token_generator`` derives from the ordinary AWS credential
    chain — the same profile ``aws configure`` writes. No static key is stored
    anywhere.

    The token lasts roughly twelve hours. It is re-minted when it ages past
    ``_TTL`` rather than on a timer or on a 401: a benchmark run can outlive a
    token, and discovering that through a mid-run authentication failure costs
    the run.
    """

    _TTL = 6 * 3600
    """Re-mint after this long. Half the real lifetime, so a long request that
    starts just under the wire still finishes with a valid token."""

    def __init__(self, region: str) -> None:
        self._region = region
        self._token: str | None = None
        self._minted_at = 0.0

    def _fresh_token(self) -> str:
        now = time.time()
        if self._token is None or now - self._minted_at > self._TTL:
            from aws_bedrock_token_generator import provide_token

            # The region is passed explicitly. `provide_token()` falls back to
            # the AWS_REGION environment variable and raises without it, so
            # leaving it out made the whole client depend on a variable that
            # happened to be set in an interactive shell. It was: the API
            # server, started from a terminal, worked. A benchmark launched
            # through `env PYTHONPATH= ...` did not, and failed all 150
            # questions in four seconds with "Region must be provided".
            #
            # `mantle_region` is already a validated setting. Reading it from
            # the environment as well was one source of truth too many.
            self._token = provide_token(region=self._region)
            self._minted_at = now
            logger.info("minted a Bedrock bearer token for %s", self._region)
        return self._token

    def auth_flow(self, request: httpx.Request):
        request.headers["Authorization"] = f"Bearer {self._fresh_token()}"
        yield request


class SigV4Auth(httpx.Auth):
    """Signs outgoing httpx requests with AWS SigV4.

    ``httpx.Auth`` is a hook: httpx builds the request, hands it here, and sends
    whatever this yields. We hand the request to botocore's signer (so we are
    not hand-rolling cryptography) and copy the resulting headers across.

    Credentials are re-fetched per request rather than cached, so temporary
    credentials that rotate — an assumed role, an EC2 instance profile — keep
    working without restarting the process.
    """

    requires_request_body = True
    """Tells httpx to materialise the body before calling us. SigV4 hashes the
    body, so it must be available at signing time."""

    def __init__(self, region: str, service: str, session: boto3.Session | None = None) -> None:
        self._region = region
        self._service = service
        self._session = session or boto3.Session()

    def _credentials(self) -> Credentials:
        credentials = self._session.get_credentials()
        if credentials is None:
            raise MantleError(
                "No AWS credentials found. Configure ~/.aws/credentials, set "
                "AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, or attach an instance role."
            )
        return credentials.get_frozen_credentials()

    def auth_flow(self, request: httpx.Request):
        aws_request = AWSRequest(
            method=request.method,
            url=str(request.url),
            data=request.content,
            headers={
                # Only forward headers that are part of what we want signed.
                # httpx adds hop-by-hop headers that must not be signed, and
                # botocore adds its own Host.
                key: value
                for key, value in request.headers.items()
                if key.lower() in {"content-type", "accept", "anthropic-version"}
            },
        )
        BotocoreSigV4Auth(self._credentials(), self._service, self._region).add_auth(aws_request)

        for key, value in aws_request.headers.items():
            request.headers[key] = value

        yield request


class MantleClient:
    """Calls chat-completion models on Bedrock Mantle.

    Deliberately small: one ``complete()`` method. The agent needs "send
    messages, get text back, know what it cost" and nothing else. Streaming and
    tool-calling can be added when a feature actually requires them.
    """

    def __init__(
        self,
        config: Settings | None = None,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self._config = config or settings()

        # Dispatch on the resolved authentication, not on whether a base URL
        # happens to be set. Branching on the URL made `llm_auth` unreachable
        # for any explicit endpoint — including Mantle's own /v1 route, which
        # then answered 401 with no credential attached at all.
        auth_mode = self._config.resolved_llm_auth

        if client is not None:
            self._client = client
        elif auth_mode == "bedrock_token":
            # Mantle's /openai/v1 route. Bearer token from the AWS credential
            # chain, re-minted as it ages.
            self._client = httpx.Client(
                base_url=self._config.base_url,
                timeout=httpx.Timeout(self._config.request_timeout_seconds),
                auth=BedrockTokenAuth(self._config.mantle_region),
            )
            logger.info("using bedrock token auth against %s", self._config.base_url)
        elif auth_mode == "bearer":
            # A local or self-hosted OpenAI-compatible server, or any provider
            # taking a static key. No SigV4: there are no AWS credentials to
            # sign with, and signing a request nobody verifies buys nothing but
            # latency and another way to fail.
            headers = {}
            if self._config.llm_api_key:
                headers["Authorization"] = f"Bearer {self._config.llm_api_key}"
            self._client = httpx.Client(
                base_url=self._config.base_url,
                timeout=httpx.Timeout(self._config.request_timeout_seconds),
                headers=headers,
            )
            logger.info("using local model endpoint %s", self._config.base_url)
        else:
            self._client = httpx.Client(
                base_url=self._config.base_url,
                timeout=httpx.Timeout(self._config.request_timeout_seconds),
                auth=SigV4Auth(
                    region=self._config.mantle_region,
                    service=self._config.mantle_signing_service,
                ),
            )

    def complete(
        self,
        prompt: str,
        *,
        model: str | None = None,
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> Completion:
        """Send a single-turn prompt and return the model's reply.

        Args:
            prompt: The user message.
            model: Model id, e.g. ``qwen.qwen3-coder-next``. Defaults to the
                configured light tier.
            system: Optional system message, used to set role and constraints.
            max_tokens: Output cap. Defaults to configured value.
            temperature: Defaults to configured value (0.0 — determinism).

        Raises:
            ModelUnavailableError: The account cannot call this model.
            MantleError: Any other non-retryable failure.
        """
        model = model or self._config.light_model

        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": model,
            "messages": messages,
            # Mantle follows the current OpenAI schema, where the output cap is
            # `max_completion_tokens`. The older `max_tokens` name is rejected
            # by some models on this endpoint.
            "max_completion_tokens": max_tokens or self._config.max_tokens,
            "temperature": (
                self._config.temperature if temperature is None else temperature
            ),
        }
        # Half price, more latency. Only ever set deliberately — a user
        # waiting on an answer should not be paying for it in seconds.
        if self._config.service_tier:
            payload["service_tier"] = self._config.service_tier

        # One Langfuse generation per call, named after the graph step that made
        # it. Messages go in OpenAI format so the UI renders them as a chat.
        with observability.observation(
            self._config,
            observability.current_step(),
            as_type="generation",
            input=messages,
            input_limit=observability.MAX_PROMPT_TEXT,
            model=model,
            model_parameters={
                "temperature": payload["temperature"],
                "max_completion_tokens": payload["max_completion_tokens"],
            },
            metadata={"service_tier": self._config.service_tier or "standard"},
        ) as generation:
            started = time.monotonic()
            data = self._post("/chat/completions", payload)
            elapsed = time.monotonic() - started

            try:
                message = data["choices"][0]["message"]
                text = message.get("content") or ""
            except (KeyError, IndexError) as exc:
                raise MantleError(
                    f"Unexpected response shape from Mantle: {data}"
                ) from exc

            usage = data.get("usage") or {}
            completion = Completion(
                text=text.strip(),
                model=model,
                input_tokens=int(usage.get("prompt_tokens") or 0),
                output_tokens=int(usage.get("completion_tokens") or 0),
                seconds=round(elapsed, 3),
            )

            # Reasoning models put their thinking beside the answer. It is the
            # record of *why* a query was written the way it was, so keep it.
            thinking = message.get("reasoning_content") or message.get("reasoning")
            output: dict[str, Any] = {"role": "assistant", "content": completion.text}
            if thinking:
                output["reasoning"] = thinking
            generation.update(
                output=observability.clip(output, observability.MAX_PROMPT_TEXT),
                usage_details={
                    "input": completion.input_tokens,
                    "output": completion.output_tokens,
                },
                metadata={
                    "seconds": completion.seconds,
                    "finish_reason": data["choices"][0].get("finish_reason"),
                },
            )

        logger.debug(
            "mantle call model=%s in=%d out=%d %.2fs",
            completion.model,
            completion.input_tokens,
            completion.output_tokens,
            completion.seconds,
        )
        return completion

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST with retries on transient failures.

        Retried: 429 (throttling) and 5xx (server-side). Both are expected under
        sustained load and succeed on a later attempt.

        Not retried: 4xx other than 429. A malformed request or a model this
        account cannot call will fail identically every time, and retrying only
        delays the error.
        """
        last_error: Exception | None = None

        for attempt in range(self._config.max_retries):
            try:
                response = self._client.post(path, json=payload)
            except httpx.RequestError as exc:  # connection reset, DNS, timeout
                last_error = exc
                self._sleep_before_retry(attempt)
                continue

            if response.status_code < 300:
                return response.json()

            body = response.text[:500]

            if response.status_code in (401, 403) and "not available for this account" in body:
                raise ModelUnavailableError(
                    f"{payload.get('model')} is not available for this AWS account. "
                    f"Pick a different model or request access in the Bedrock console."
                )

            retryable = response.status_code == 429 or response.status_code >= 500
            if not retryable:
                raise MantleError(f"Mantle returned HTTP {response.status_code}: {body}")

            last_error = MantleError(f"HTTP {response.status_code}: {body}")
            self._sleep_before_retry(attempt)

        raise MantleError(
            f"Mantle call failed after {self._config.max_retries} attempts: {last_error}"
        )

    @staticmethod
    def _sleep_before_retry(attempt: int) -> None:
        """Exponential backoff: 0.5s, 1s, 2s, 4s..., capped at 8s."""
        time.sleep(min(0.5 * (2**attempt), 8.0))

    def list_models(self) -> list[dict[str, Any]]:
        """Every model Mantle hosts in this region.

        Note the caveat in the module docstring: presence here does not imply
        this account may call it. ``status`` reflects the model's general
        availability, not your entitlement.
        """
        response = self._client.get("/models")
        response.raise_for_status()
        return response.json().get("data", [])

    def probe_model(self, model: str) -> tuple[bool, str]:
        """Check whether this account can actually invoke a model.

        Sends the cheapest possible request. Used by the model-selection script
        to separate "listed" from "callable" without running a full benchmark.

        Returns:
            ``(True, "")`` if callable, otherwise ``(False, reason)``.
        """
        try:
            self.complete("Reply with the single word: ok", model=model, max_tokens=16)
        except MantleError as exc:
            return False, str(exc)[:200]
        return True, ""

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> MantleClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def extract_sql(text: str) -> str:
    """Pull a bare SQL statement out of a model's reply.

    Models are asked for SQL only, and most comply. Some wrap the answer in
    markdown fences anyway (``mistral.ministral-3-8b-instruct`` does this
    consistently), and some prepend a sentence. Rather than fight the prompt,
    normalise here — the cost of being tolerant is one small function, and the
    cost of being strict is a spurious failure on an otherwise correct query.
    """
    cleaned = text.strip()

    if "```" in cleaned:
        # Take the content of the first fenced block. The opening fence may
        # carry a language tag (```sql) which must not survive.
        _, _, after = cleaned.partition("```")
        block, _, _ = after.partition("```")
        if "\n" in block:
            first_line, _, rest = block.partition("\n")
            block = rest if first_line.strip().lower() in {"sql", "postgresql", "psql"} else block
        cleaned = block.strip()

    return cleaned.rstrip(";").strip()


def json_from_reply(text: str) -> dict[str, Any]:
    """Parse a JSON object out of a model reply, tolerating surrounding prose.

    Used by the classification steps, which ask for structured output. Same
    reasoning as :func:`extract_sql`: normalise rather than fail.
    """
    cleaned = text.strip()

    if "```" in cleaned:
        _, _, after = cleaned.partition("```")
        block, _, _ = after.partition("```")
        if "\n" in block:
            first_line, _, rest = block.partition("\n")
            block = rest if first_line.strip().lower() == "json" else block
        cleaned = block.strip()

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise MantleError(f"No JSON object found in model reply: {text[:200]}")

    try:
        return json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise MantleError(f"Malformed JSON in model reply: {text[:200]}") from exc
