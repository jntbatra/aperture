"""Command line interface.

The timeline is the point: watching validate -> cost_guard -> execute, and the
loop back through diagnose when something fails, is what makes the design
legible in a way a final answer never is.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

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
from .ingest import load_csv, register_dataset
from .registry import Connection, Registry
from .schema import SchemaLinker

app = typer.Typer(
    add_completion=False,
    help="Aperture — ask a SQL database questions in plain English.",
    no_args_is_help=True,
)
console = Console()


def _version_callback(value: bool) -> None:
    if value:
        from . import __version__

        console.print(f"aperture {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-V", callback=_version_callback, is_eager=True
    ),
) -> None:
    """Ask a SQL database questions in plain English."""

NODE_LABELS = {
    "route": "routing",
    "link_schema": "linking schema",
    "generate_sql": "writing SQL",
    "validate": "validating",
    "cost_guard": "estimating cost",
    "execute": "executing",
    "diagnose": "diagnosing failure",
    "diagnose_empty": "explaining empty result",
    "clarify": "checking the question",
    "verify": "verifying result",
    "narrate": "summarising",
    "chart": "choosing chart",
    "exhausted": "giving up",
    "small_talk": "answering",
}

# Rendered per step in the live timeline.
STEP_MARKER = "\u203a"

STATUS_STYLES = {
    "needs_clarification": "cyan",
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
    spec_out: str | None = typer.Option(None, "--spec-out", help="Write the chart spec here."),
    csv_out: str | None = typer.Option(None, "--csv", help="Write the result rows to a CSV file."),
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
            console.print(f"  [cyan]{STEP_MARKER}[/cyan] {label}" + (f" [dim]({detail})[/dim]" if detail else ""))
            final.update(update)

    console.print()
    if show_sql and final.get("sql"):
        console.print(Panel(Syntax(final["sql"], "sql", theme="ansi_dark", word_wrap=True), title="SQL", border_style="dim"))

    if final.get("rows"):
        console.print(_render_rows(final.get("columns", []), final["rows"]))
        if final.get("truncated"):
            console.print("[dim]… result truncated at the row limit[/dim]")
        console.print()

    if final.get("clarify_options"):
        console.print("[dim]options:[/dim] " + "  ".join(f"[cyan]{o}[/cyan]" for o in final["clarify_options"]))

    status = final.get("status", "unknown")
    console.print(Panel(final.get("answer", "(no answer)"), title=status, border_style=STATUS_STYLES.get(status, "white")))

    for insight in final.get("insights", []):
        console.print(f"[cyan]note[/cyan]: {insight['message']}")

    for finding in final.get("verification", []):
        console.print(f"[yellow]caveat[/yellow]: {finding['message']}")

    if final.get("assumptions"):
        console.print(f"[dim]assumptions: {final['assumptions']}[/dim]")

    if csv_out and final.get("rows"):
        import csv as _csv

        with open(csv_out, "w", newline="") as handle:
            writer = _csv.writer(handle)
            writer.writerow(final.get("columns", []))
            writer.writerows(final["rows"])
        console.print(f"[dim]{len(final['rows'])} rows written to {csv_out}[/dim]")

    spec = final.get("chart_spec")
    if spec:
        mark = spec.get("mark")
        kind = mark.get("type") if isinstance(mark, dict) else mark
        console.print(f"[dim]chart: {kind}[/dim]")
        if spec_out:
            with open(spec_out, "w") as fh:
                json.dump(spec, fh, indent=2)
            console.print(f"[dim]chart spec written to {spec_out}[/dim]")

    if final.get("suggestions"):
        console.print("\n[dim]next:[/dim]")
        for suggestion in final["suggestions"]:
            console.print(f"  [cyan]·[/cyan] {suggestion['text']} [dim]({suggestion['reason']})[/dim]")

    console.print(
        f"\n[dim]{time.perf_counter() - started:.1f}s · {LEDGER.summary()}[/dim]"
    )


@app.command()
def load(
    path: str = typer.Argument(..., help="CSV file to load."),
    dataset: str | None = typer.Option(None, "--name", help="Dataset name (default: file stem)."),
    table: str | None = typer.Option(None, "--table", help="Table name (default: file stem)."),
    delimiter: str | None = typer.Option(None, "--delimiter", help="Override the CSV delimiter."),
) -> None:
    """Load a CSV and make it the active dataset.

    No database required: the file becomes a real SQLite table with inferred
    types, and every other command works against it unchanged.
    """
    result = load_csv(path, dataset=dataset, table=table, delimiter=delimiter)
    name = dataset or Path(path).stem
    register_dataset(result, name=name, source=str(Path(path).expanduser()))

    table_view = Table(title=result.summary(), box=None)
    table_view.add_column("column")
    table_view.add_column("type")
    table_view.add_column("from")
    for column in result.columns:
        renamed = "" if column.source == column.name else column.source
        table_view.add_row(column.name, column.sql_type, renamed)
    console.print(table_view)
    console.print(f"\n[dim]stored at {result.path} · connection '{name}' is now active[/dim]")
    console.print('[dim]now ask: aperture ask "what were total units by region?"[/dim]')


@app.command()
def connect(
    url: str = typer.Argument(..., help="SQLAlchemy URL, e.g. postgresql+psycopg://user:pw@host/db"),
    name: str = typer.Option(..., "--name", "-n", help="Name to remember this connection by."),
) -> None:
    """Register a database connection and make it active."""
    connection = Connection(name=name, url=url, kind="database")
    database = Database(url)  # fails fast on an unsupported or malformed URL
    bundle = load_schema(database, refresh=True)
    Registry.load().add(connection)
    console.print(
        f"[green]connected[/green] {name} · {connection.dialect} · "
        f"{len(bundle.snapshot.tables)} tables · {len(bundle.snapshot.foreign_keys)} foreign keys"
    )
    console.print('[dim]now ask: aperture ask "how many rows are there?"[/dim]')


@app.command(name="connections")
def list_connections() -> None:
    """List every registered connection and show which is active."""
    registry = Registry.load()
    if not registry.connections:
        console.print("[dim]nothing registered yet[/dim]")
        console.print("[dim]  aperture load sales.csv          # a spreadsheet[/dim]")
        console.print("[dim]  aperture connect <url> -n prod   # a database[/dim]")
        return

    table = Table(box=None)
    table.add_column("")
    table.add_column("name")
    table.add_column("kind")
    table.add_column("target", overflow="fold")
    for name, connection in sorted(registry.connections.items()):
        marker = "[green]•[/green]" if name == registry.active else " "
        target = connection.source or connection.safe_url
        table.add_row(marker, name, connection.kind, target)
    console.print(table)


@app.command()
def use(name: str = typer.Argument(..., help="Connection to make active.")) -> None:
    """Switch the active connection."""
    connection = Registry.load().use(name)
    if not connection:
        console.print(f"[red]no connection named {name!r}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]active:[/green] {name} · {connection.safe_url}")


@app.command()
def forget(name: str = typer.Argument(..., help="Connection to remove.")) -> None:
    """Remove a connection from the registry (data on disk is left alone)."""
    if Registry.load().remove(name):
        console.print(f"[dim]removed {name}[/dim]")
    else:
        console.print(f"[red]no connection named {name!r}[/red]")
        raise typer.Exit(1)


@app.command()
def profile(
    refresh: bool = typer.Option(False, "--refresh", help="Re-read the schema."),
    table: str | None = typer.Option(None, "--table", help="Show one table in full."),
) -> None:
    """Show what Aperture knows about the database."""
    db = Database.active()
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
    db = Database.active()
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
def stats(limit: int = typer.Option(500, "--limit", help="How many recent runs to summarise.")) -> None:
    """Operational metrics from recorded runs."""
    from .telemetry import summarise, telemetry_path

    data = summarise(limit)
    if not data.runs:
        console.print("[dim]no runs recorded yet[/dim]")
        return

    table = Table(box=None, title=f"last {data.runs} runs")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("median latency", f"{data.median_ms / 1000:.1f}s")
    table.add_row("p95 latency", f"{data.p95_ms / 1000:.1f}s")
    table.add_row("needed a repair", f"{data.repair_rate:.0%}")
    table.add_row("identifiers auto-fixed", f"{data.identifier_repair_rate:.0%}")
    table.add_row("answers with a caveat", f"{data.caveat_rate:.0%}")
    table.add_row("empty results", f"{data.empty_rate:.0%}")
    table.add_row("tokens", f"{data.tokens:,}")
    console.print(table)

    statuses = Table(box=None)
    statuses.add_column("status")
    statuses.add_column("runs", justify="right")
    for status, count in sorted(data.by_status.items(), key=lambda pair: -pair[1]):
        statuses.add_row(status, str(count))
    console.print(statuses)
    console.print(f"[dim]{telemetry_path()}[/dim]")


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
    """Run the web app: API, and the UI if it has been built."""
    import uvicorn

    from .server import UI_MOUNTED, ui_directory

    registry = Registry.load()
    current = registry.current()
    console.print(f"[bold]Aperture[/bold] · {current.name if current else settings().database_url}")
    if UI_MOUNTED:
        console.print(f"  app  http://{host}:{port}")
    else:
        console.print("[yellow]  UI not built[/yellow] — run: cd frontend && npm install && npm run build")
        console.print(f"[dim]  (looked in {ui_directory()})[/dim]")
    console.print(f"  api  http://{host}:{port}/docs\n")

    uvicorn.run("aperture.server:app", host=host, port=port, reload=reload)


@app.command()
def history(limit: int = typer.Option(15, "--limit", "-n")) -> None:
    """Recent questions and how they went."""
    from .telemetry import connect as telemetry_connect

    connection = telemetry_connect()
    rows = connection.execute(
        "SELECT started_at, question, status, row_count, duration_ms, attempts"
        " FROM runs ORDER BY started_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    connection.close()

    if not rows:
        console.print("[dim]no questions asked yet[/dim]")
        return

    table = Table(box=None)
    table.add_column("when")
    table.add_column("question", overflow="fold")
    table.add_column("status")
    table.add_column("rows", justify="right")
    table.add_column("took", justify="right")
    for started, question, status, row_count, duration, attempts in rows:
        stamp = time.strftime("%H:%M", time.localtime(started))
        label = status or "?"
        if attempts:
            label += f" ({attempts} repairs)"
        table.add_row(stamp, question[:70], label, str(row_count), f"{duration / 1000:.1f}s")
    console.print(table)


@app.command()
def demo() -> None:
    """Load the bundled sample dataset and suggest questions to ask."""
    sample = Path(__file__).parent / "data" / "sample_sales.csv"
    if not sample.exists():
        console.print("[red]bundled sample is missing[/red]")
        raise typer.Exit(1)

    result = load_csv(sample, dataset="demo_sales")
    register_dataset(result, name="demo_sales", source=str(sample))
    console.print(f"[green]loaded[/green] {result.summary()}\n")
    console.print("Try:")
    for question in (
        "What was total revenue by region?",
        "How did units sold change month by month?",
        "Which product has the highest average order value?",
        "What share of orders were refunded, by channel?",
    ):
        console.print(f'  [cyan]aperture ask[/cyan] "{question}"')


@app.command()
def mcp(
    install: bool = typer.Option(False, "--install", help="Register with Claude Code."),
    print_config: bool = typer.Option(False, "--print-config", help="Show the MCP config block."),
) -> None:
    """Run the MCP server on stdio, or register it with an agent.

    Any MCP client then gets ask_database, run_sql and describe_schema against
    the active connection, behind the same guardrails as the CLI.
    """
    import shutil
    import subprocess

    executable = shutil.which("aperture") or "aperture"
    block = {"mcpServers": {"aperture": {"command": executable, "args": ["mcp"]}}}

    if print_config:
        console.print(JSON(json.dumps(block)))
        return

    if install:
        claude = shutil.which("claude")
        if not claude:
            console.print("[yellow]claude CLI not found[/yellow] — add this to your MCP config:")
            console.print(JSON(json.dumps(block)))
            raise typer.Exit(1)
        result = subprocess.run(
            [claude, "mcp", "add", "aperture", "--", executable, "mcp"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            console.print("[green]registered[/green] aperture with Claude Code")
        else:
            console.print(f"[red]registration failed[/red]: {result.stderr.strip()[:200]}")
            raise typer.Exit(result.returncode)
        return

    from .mcp_server import main

    main()


if __name__ == "__main__":
    app()
