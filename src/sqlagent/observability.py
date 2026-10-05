"""Langfuse tracing: one trace per question, one observation per step.

Off unless configured
---------------------
Tracing runs only when ``LANGFUSE_PUBLIC_KEY`` and ``LANGFUSE_SECRET_KEY`` are
set *and* ``Settings.tracing_enabled`` is true. Otherwise every helper here is a
no-op that costs a function call, so the CLI, the tests and a deployment
without Langfuse behave exactly as before. The benchmark turns it off unless
``--trace`` is passed: 500 questions per run would bury real traffic.

The SDK is created lazily, after ``.env`` has been read by pydantic, and never
when the keys are missing — the SDK logs an authentication error on every
start otherwise.

What a trace looks like
-----------------------
::

    answer-question (agent)          input: question     output: answer + SQL
    ├── screen-question (guardrail)
    ├── select-tables (retriever)
    ├── build-schema-context (retriever)
    ├── generate-sql (chain)
    │   └── generate-sql (generation)    model, messages, tokens, temperature
    ├── execute-sql (tool)               SQL in, columns + row count out
    ├── check-intent (evaluator)
    │   └── check-intent (generation)
    └── write-answer (chain)
        └── write-answer (generation)

Names are verb-first and stable — evaluators and dashboards key on them, so
they are an API. Run-specific values go in metadata, never in a name.

What is never sent
------------------
Result *rows*. Execution reports columns, row count and truncation only: rows
are the customer's data, and the trace needs to say what ran, not repeat what
it returned. Prompts do carry schema and a few sample values (that is what the
model saw, and a trace without it cannot explain a decision), so a masking
hook redacts e-mail addresses and phone numbers from every input, output and
metadata attribute before export.
"""

from __future__ import annotations

import contextvars
import logging
import os
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlagent.config import Settings

logger = logging.getLogger(__name__)

_client: Any = None
_client_failed = False

# The graph step currently running, so a model call can be named after the
# step that made it without every call site passing a name.
_current_step: contextvars.ContextVar[str] = contextvars.ContextVar(
    "sqlagent_trace_step", default="call-model"
)

MAX_TEXT = 4000
"""Longest string kept in a step's input or output. Schema text and state can
run to tens of kilobytes; the head says what happened."""

MAX_PROMPT_TEXT = 100_000
"""Longest message kept on a generation. Much higher than ``MAX_TEXT``: the
prompt is the record of exactly what the model was told, and a cut prompt
cannot answer "why did it do that?" — an audit of a live trace found the
glossary-dependent part of the prompt past the old 4,000-character cut."""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


_dotenv_loaded = False


def _load_langfuse_env() -> None:
    """Copy LANGFUSE_* lines from ``.env`` into the process environment, once.

    pydantic-settings reads ``.env`` into ``Settings`` without exporting it, and
    the Langfuse SDK reads only ``os.environ``. Only the LANGFUSE_ keys are
    copied, and never over a value already set in the real environment.
    """
    global _dotenv_loaded
    if _dotenv_loaded:
        return
    _dotenv_loaded = True
    try:
        from dotenv import dotenv_values

        for key, value in dotenv_values(".env").items():
            if key.startswith("LANGFUSE_") and value and key not in os.environ:
                os.environ[key] = value
    except Exception:  # noqa: BLE001
        logger.exception("could not read LANGFUSE_ settings from .env")


def _keys_present() -> bool:
    _load_langfuse_env()
    return bool(os.environ.get("LANGFUSE_PUBLIC_KEY")) and bool(
        os.environ.get("LANGFUSE_SECRET_KEY")
    )


def enabled(config: Settings | None) -> bool:
    """Whether to trace at all for this configuration."""
    if config is not None and not getattr(config, "tracing_enabled", True):
        return False
    return _keys_present()


def client() -> Any:
    """The Langfuse client, created on first use. ``None`` when unavailable."""
    global _client, _client_failed
    if _client is not None or _client_failed:
        return _client
    if not _keys_present():
        return None
    try:
        from langfuse import Langfuse

        # Environment and base URL come from LANGFUSE_TRACING_ENVIRONMENT and
        # LANGFUSE_BASE_URL, which the SDK reads itself.
        _client = Langfuse(mask_otel_spans=mask_otel_spans)
    except Exception:  # noqa: BLE001 - tracing must never take the app down
        logger.exception("Langfuse could not be initialised; tracing is off")
        _client_failed = True
        _client = None
    return _client


def flush() -> None:
    """Send buffered observations. For short-lived processes (CLI, scripts)."""
    if _client is not None:
        try:
            _client.flush()
        except Exception:  # noqa: BLE001
            logger.exception("Langfuse flush failed")


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------


class _NoOp:
    """Stands in for an observation when tracing is off."""

    def update(self, **_: Any) -> None:
        return None


_NOOP = _NoOp()


@contextmanager
def observation(
    config: Settings | None,
    name: str,
    *,
    as_type: str = "span",
    input: Any = None,
    metadata: Mapping[str, Any] | None = None,
    model: str | None = None,
    model_parameters: Mapping[str, Any] | None = None,
    input_limit: int = MAX_TEXT,
) -> Iterator[Any]:
    """Open an observation as the current one; yields it (or a no-op).

    Errors inside the block are recorded on the observation by the SDK and
    re-raised unchanged. A failure *in tracing itself* is logged and swallowed.
    """
    langfuse = client() if enabled(config) else None
    if langfuse is None:
        yield _NOOP
        return

    kwargs: dict[str, Any] = {"name": name, "as_type": as_type}
    if input is not None:
        kwargs["input"] = clip(input, input_limit)
    if metadata:
        kwargs["metadata"] = dict(metadata)
    if model:
        kwargs["model"] = model
    if model_parameters:
        kwargs["model_parameters"] = dict(model_parameters)

    try:
        manager = langfuse.start_as_current_observation(**kwargs)
        span = manager.__enter__()
    except Exception:  # noqa: BLE001
        logger.exception("could not open Langfuse observation %s", name)
        yield _NOOP
        return

    try:
        yield span
    except BaseException as exc:
        manager.__exit__(type(exc), exc, exc.__traceback__)
        raise
    else:
        manager.__exit__(None, None, None)


@contextmanager
def question_trace(
    config: Settings,
    question: str,
    *,
    session_id: str | None,
    source: str,
) -> Iterator[Any]:
    """The root observation for one question, with trace-wide attributes.

    One trace per question and one session per conversation thread: a chat is
    open-ended, so per-turn traces stay small and the Sessions view stitches
    them back together.
    """
    tier = "expensive" if config.check_result_intent else "cheap"
    with observation(
        config, "answer-question", as_type="agent", input={"question": question}
    ) as root:
        if root is _NOOP:
            yield root
            return
        try:
            from langfuse import propagate_attributes

            attributes = propagate_attributes(
                session_id=session_id or None,
                tags=[f"tier:{tier}", f"source:{source}"],
                trace_name="answer-question",
                metadata={
                    "tier": tier,
                    "quality_tier": config.quality_tier,
                    "model": config.strong_model,
                },
            )
            attributes.__enter__()
        except Exception:  # noqa: BLE001
            logger.exception("could not set Langfuse trace attributes")
            yield root
            return
        try:
            yield root
        finally:
            attributes.__exit__(None, None, None)


def traced_node(
    config: Settings,
    name: str,
    as_type: str,
    fn: Callable,
    *,
    inputs: tuple[str, ...] = (),
) -> Callable:
    """Wrap a graph node so each run is an observation of its own.

    Input is only the state keys the step actually reads (``inputs``): the
    whole incoming state is the conversation, the trace object and every
    earlier result, and repeating it per step would bury what matters. Output
    is the node's return value — the part of the state it changed — with rows
    replaced by their shape. A guardrail or evaluator that changed nothing
    passed, and says so rather than showing ``{}``.
    """
    if not enabled(config):
        return fn

    def node(state: dict) -> dict:
        token = _current_step.set(name)
        try:
            step_input = summarise_state(
                {k: state.get(k) for k in inputs if state.get(k) not in (None, "", [])}
            ) or None
            with observation(config, name, as_type=as_type, input=step_input) as span:
                update = fn(state)
                try:
                    output = summarise_state(update)
                    if not output and as_type in ("guardrail", "evaluator"):
                        output = {"verdict": "passed"}
                    span.update(output=output or None)
                except Exception:  # noqa: BLE001
                    logger.exception("could not record output for %s", name)
                return update
        finally:
            _current_step.reset(token)

    return node


def current_step() -> str:
    """Name of the graph step running now, for naming model calls."""
    return _current_step.get()


# ---------------------------------------------------------------------------
# Shaping data for traces
# ---------------------------------------------------------------------------


def clip(value: Any, limit: int = MAX_TEXT) -> Any:
    """Shorten long strings, recursively, so one value cannot dominate a trace."""
    if isinstance(value, str):
        if len(value) > limit:
            return value[:limit] + f"… [{len(value) - limit} more characters]"
        return value
    if isinstance(value, Mapping):
        return {k: clip(v, limit) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [clip(v, limit) for v in value]
    return value


def summarise_state(update: Any) -> Any:
    """A node's state update, with result rows replaced by their shape."""
    if not isinstance(update, Mapping):
        return clip(update)
    out: dict[str, Any] = {}
    for key, value in update.items():
        if key == "trace":
            continue
        if hasattr(value, "rows") and hasattr(value, "columns"):
            out[key] = {
                "columns": list(value.columns),
                "row_count": len(value.rows),
                "truncated": bool(getattr(value, "truncated", False)),
            }
        elif key == "failure_kind" and value is None:
            continue  # "no failure" is the absence of one, not a field
        elif hasattr(value, "tables") and hasattr(value, "hops"):
            out[key] = {"tables": sorted(value.tables), "hops": value.hops}
        elif isinstance(value, str | int | float | bool | type(None)):
            out[key] = clip(value)
        elif isinstance(value, list | tuple | Mapping):
            try:
                out[key] = clip(value)
            except Exception:  # noqa: BLE001
                out[key] = repr(value)[:200]
        else:
            out[key] = type(value).__name__
    return out


# ---------------------------------------------------------------------------
# Masking
# ---------------------------------------------------------------------------

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<![\w.-])\+?\d[\d -]{8,}\d(?![\w.-])")
_MASKED_PREFIXES = (
    "langfuse.observation.input",
    "langfuse.observation.output",
    "langfuse.observation.metadata",
    "langfuse.trace.input",
    "langfuse.trace.output",
    "langfuse.trace.metadata",
)


def redact(text: str) -> str:
    """Replace e-mail addresses and phone-number-shaped digit runs."""
    return _PHONE.sub("[phone]", _EMAIL.sub("[email]", text))


def mask_otel_spans(*, params: Any) -> Any:
    """Langfuse export hook: redact PII in inputs, outputs and metadata.

    Runs on the exporter thread, per batch. Only string attributes under the
    Langfuse input/output/metadata keys are touched; a span with nothing to
    redact gets no patch.
    """
    from langfuse.types import MaskOtelSpansResult, OtelSpanPatch

    patches = {}
    for identifier, span in params.spans.items():
        changed = {}
        for key, value in span.attributes.items():
            if isinstance(value, str) and key.startswith(_MASKED_PREFIXES):
                masked = redact(value)
                if masked != value:
                    changed[key] = masked
        if changed:
            patches[identifier] = OtelSpanPatch(set_attributes=changed)
    return MaskOtelSpansResult(span_patches=patches) if patches else None
