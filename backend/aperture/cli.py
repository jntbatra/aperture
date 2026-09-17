"""Command line interface.

The timeline is the point: watching validate -> cost_guard -> execute, and the
loop back through diagnose when something fails, is what makes the design
legible in a way a final answer never is.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Optional

import typer
from rich.console import Console
from rich.json import JSON
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from .budget import LEDGER
from .config import settings
from .db import Database, load_schema
from .graph import build_analyst, sync_checkpointer
from .schema import SchemaLinker

app = typer.Typer(add_completion=False, help="Ask a SQL database questions in plain English.")
console = Console()

NODE_LABELS = {
    "route": "routing",
    "link_schema": "linking schema",
    "generate_sql": "writing SQL",
    "validate": "validating",
    "cost_guard": "estimating cost",
    "execute": "executing",
    "diagnose": "diagnosing failure",
    "diagnose_empty": "explaining empty result",
    "narrate": "summarising",
    "chart": "choosing chart",
    "exhausted": "giving up",
    "small_talk": "answering",
}

STATUS_STYLES = {
    "answered": "green",
    "empty": "yellow",
    "refused": "red",
    "exhausted": "red",
    "over_budget": "red",
    "timed_out": "red",
}


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )


def _render_rows(columns: list[str], rows: list, limit: int = 20) -> Table:
    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    for column in columns:
        table.add_column(str(column), overflow="fold")
    for row in rows[:limit]:
        table.add_row(*["" if v is None else str(v) for v in row])
    return table


@app.command()
def ask(
    question: str = typer.Argument(..., help="Question to answer."),
    thread: str = typer.Option("cli", "--thread", "-t", help="Conversation thread id."),
    show_sql: bool = typer.Option(True, "--sql/--no-sql"),
    spec_out: Optional[str] = typer.Option(None, "--spec-out", help="Write the chart spec here."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Answer one question end to end."""
    _configure_logging(verbose)
    started = time.perf_counter()

    graph, ctx = build_analyst(checkpointer=sync_checkpointer())
    console.print(
        f"[dim]{ctx.db.dialect} · {len(ctx.bundle.snapshot.tables)} tables · "
        f"{settings().bedrock_model_id}[/dim]\n"
    )

    final: dict = {}
    for chunk in graph.stream(
        {"question": question},
        {"configurable": {"thread_id": thread}},
        stream_mode="updates",
    ):
        for node, update in chunk.items():
            label = NODE_LABELS.get(node, node)
            detail = ""
            if node == "link_schema":
                detail = f"{len(update.get('linked_tables', []))} tables"
            elif node == "validate" and update.get("identifier_fixes"):
                detail = f"fixed {len(update['identifier_fixes'])} identifiers"
            elif node == "cost_guard" and update.get("estimated_cost"):
                detail = f"cost {update['estimated_cost']:,.0f}"
            elif node == "execute":
                detail = f"{update.get('row_count', 0)} rows in {update.get('elapsed_ms', 0):.0f}ms"
            elif node == "diagnose":
                detail = f"attempt {update.get('attempts', 0)}"
            console.print(f"  [cyan]›[/cyan] {label}" + (f" [dim]({detail})[/dim]" if detail else ""))
            final.update(update)

    console.print()
    if show_sql and final.get("sql"):
        console.print(Panel(Syntax(final["sql"], "sql", theme="ansi_dark", word_wrap=True), title="SQL", border_style="dim"))

    if final.get("rows"):
        console.print(_render_rows(final.get("columns", []), final["rows"]))
        if final.get("truncated"):
            console.print("[dim]… result truncated at the row limit[/dim]")
        console.print()

    status = final.get("status", "unknown")
    console.print(Panel(final.get("answer", "(no answer)"), title=status, border_style=STATUS_STYLES.get(status, "white")))

    if final.get("assumptions"):
        console.print(f"[dim]assumptions: {final['assumptions']}[/dim]")

    spec = final.get("chart_spec")
    if spec:
        mark = spec.get("mark")
        kind = mark.get("type") if isinstance(mark, dict) else mark
        console.print(f"[dim]chart: {kind}[/dim]")
        if spec_out:
            with open(spec_out, "w") as fh:
                json.dump(spec, fh, indent=2)
            console.print(f"[dim]chart spec written to {spec_out}[/dim]")

    console.print(
        f"[dim]{time.perf_counter() - started:.1f}s · {LEDGER.summary()}[/dim]"
    )


@app.command()
def profile(
    refresh: bool = typer.Option(False, "--refresh", help="Re-read the schema."),
    table: Optional[str] = typer.Option(None, "--table", help="Show one table in full."),
) -> None:
    """Show what Aperture knows about the database."""
    db = Database.from_settings()
    bundle = load_schema(db, refresh=refresh)

    if table:
        console.print(Syntax(bundle.snapshot.ddl_for([table], profile=bundle.profile), "sql", theme="ansi_dark"))
        return

    sizes = sorted(
        ((name, p.exact_rows) for name, p in bundle.profile.tables.items()), key=lambda p: -p[1]
    )
    summary = Table(title=f"{len(bundle.snapshot.tables)} tables · {len(bundle.snapshot.foreign_keys)} foreign keys", box=None)
    summary.add_column("table")
    summary.add_column("rows", justify="right")
    summary.add_column("observed values", overflow="fold")
    for name, rows in sizes[:18]:
        interesting = [
            f"{col}: {', '.join(str(v) for v in cp.common_values[:4])}"
            for col, cp in bundle.profile.tables[name].columns.items()
            if cp.common_values
        ]
        summary.add_row(name, f"{rows:,}", "; ".join(interesting[:2]))
    console.print(summary)

    empty = bundle.profile.empty_tables
    if empty:
        console.print(f"\n[yellow]empty tables ({len(empty)})[/yellow]: {', '.join(empty)}")
    console.print(f"[dim]cached: {bundle.from_cache} · built {bundle.age_seconds:.0f}s ago[/dim]")


@app.command()
def link(question: str = typer.Argument(..., help="Question to link against the schema.")) -> None:
    """Show which tables a question retrieves, and why."""
    db = Database.from_settings()
    bundle = load_schema(db)
    linker = SchemaLinker(bundle.snapshot, bundle.profile)
    linked = linker.link(question)

    console.print(f"[bold]seeds[/bold]: {', '.join(linked.seeds)}")
    console.print(f"[bold]expanded[/bold] ({len(linked.tables)}): {', '.join(linked.tables)}")
    if linked.value_hints:
        console.print(f"[bold]value hints[/bold]: {'; '.join(linked.value_hints)}")
    if linked.fan_out_warnings:
        for warning in linked.fan_out_warnings:
            console.print(f"[yellow]caution[/yellow]: {warning}")
    if linked.empty_tables:
        console.print(f"[yellow]empty[/yellow]: {', '.join(linked.empty_tables)}")
    console.print(f"[dim]schema section: {len(linked.as_prompt_section()):,} chars[/dim]")


@app.command()
def usage() -> None:
    """Show token spend recorded so far."""
    console.print(LEDGER.summary())
    console.print(f"[dim]ceiling ${settings().budget_ceiling_usd:.2f} · log {LEDGER.path}[/dim]")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
    reload: bool = typer.Option(False, "--reload"),
) -> None:
    """Run the HTTP API."""
    import uvicorn

    uvicorn.run("aperture.server:app", host=host, port=port, reload=reload)


@app.command()
def mcp() -> None:
    """Run the MCP server on stdio."""
    from .mcp_server import main

    main()


if __name__ == "__main__":
    app()
