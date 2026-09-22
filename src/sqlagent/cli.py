"""Terminal interface.

Useful for three things the web UI is bad at: trying a question quickly without
starting two servers, checking which models your AWS account can actually call,
and scripting.

    python -m sqlagent.cli "How many customers are there?"
    python -m sqlagent.cli --schema
    python -m sqlagent.cli --models
"""

from __future__ import annotations

import argparse
import logging
import sys

from sqlalchemy import create_engine

from sqlagent.config import settings
from sqlagent.llm.mantle import MantleClient
from sqlagent.pipeline import SqlAgent

# ANSI escape codes. Written out rather than pulled from a dependency because
# this is the entire colour requirement of the program.
DIM = "\033[2m"
BOLD = "\033[1m"
CLAY = "\033[38;5;173m"
GREEN = "\033[38;5;71m"
RED = "\033[38;5;131m"
RESET = "\033[0m"


def supports_colour() -> bool:
    """Colour only when writing to a terminal.

    Piping output to a file or another program should produce clean text, not
    escape sequences.
    """
    return sys.stdout.isatty()


def paint(text: str, colour: str) -> str:
    return f"{colour}{text}{RESET}" if supports_colour() else text


def show_progress(stage: str, detail: dict) -> None:
    labels = {
        "seeds": "Finding relevant tables",
        "schema": "Reading the schema",
        "generating": "Writing SQL",
        "validating": "Checking the query is safe",
        "executing": "Running the query",
        "repairing": "That failed — trying again",
        "answering": "Writing the answer",
    }
    label = labels.get(stage, stage)
    print(paint(f"  · {label}", DIM), file=sys.stderr, flush=True)


def ask(question: str, *, verbose: bool) -> int:
    config = settings()
    engine = create_engine(config.database_url)
    agent = SqlAgent(engine, config=config)

    result = agent.ask(question, on_progress=show_progress if verbose else None)

    print()
    if result.sql:
        print(paint("SQL", BOLD))
        print(paint(result.sql, CLAY))
        print()

    if result.result and result.result.row_count:
        print(paint("Results", BOLD))
        print(result.result.preview(limit=15))
        print()

    print(paint("Answer", BOLD))
    print(paint(result.answer, GREEN if result.ok else RED))

    trace = result.trace
    print()
    print(
        paint(
            f"{trace.seconds:.1f}s · {trace.model_calls} model calls · "
            f"{trace.input_tokens + trace.output_tokens:,} tokens · "
            f"{trace.repair_count} repairs",
            DIM,
        )
    )

    return 0 if result.ok else 1


def show_schema() -> int:
    config = settings()
    agent = SqlAgent(create_engine(config.database_url), config=config)
    snapshot = agent.snapshot

    print(paint(f"{len(snapshot)} tables · version {snapshot.version}", BOLD))
    print()

    for table in sorted(snapshot.tables.values(), key=lambda t: t.name):
        print(paint(table.name, CLAY))
        for column in table.columns:
            marker = " PK" if column.primary_key else ""
            print(f"  {column.name:<26} {paint(column.type + marker, DIM)}")
        for fk in table.foreign_keys:
            print(paint(f"  → {fk.join_condition()}", DIM))
        print()

    return 0


def list_models() -> int:
    """Show which models this AWS account can actually call.

    Mantle's model list reports what it hosts, which is not the same as what
    your account is entitled to invoke. The only reliable test is to call each
    one, which is what this does.
    """
    client = MantleClient()
    models = client.list_models()
    available = sorted(m["id"] for m in models if m.get("status") == "available")

    print(f"{len(available)} models listed as available. Probing each…\n")

    callable_models: list[str] = []
    for model_id in available:
        ok, reason = client.probe_model(model_id)
        if ok:
            callable_models.append(model_id)
            print(f"  {paint('✓', GREEN)} {model_id}")
        else:
            short = "not entitled" if "not available" in reason else reason[:60]
            print(f"  {paint('✗', RED)} {model_id} {paint(short, DIM)}")

    print(f"\n{len(callable_models)} callable on this account.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sqlagent", description="Ask a database questions in plain language."
    )
    parser.add_argument("question", nargs="?", help="the question to ask")
    parser.add_argument("--schema", action="store_true", help="print the database structure")
    parser.add_argument("--models", action="store_true", help="probe which models are callable")
    parser.add_argument("-q", "--quiet", action="store_true", help="hide progress")
    parser.add_argument("--debug", action="store_true", help="verbose logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.models:
        return list_models()
    if args.schema:
        return show_schema()
    if not args.question:
        parser.print_help()
        return 2

    return ask(args.question, verbose=not args.quiet)


if __name__ == "__main__":
    raise SystemExit(main())
