"""HTTP API.

`/ask` streams the graph's node updates as server-sent events, because the
interesting part of an answer is the path taken to reach it -- especially when
that path loops back through a repair.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from .budget import LEDGER
from .charts import jsonable
from .config import settings
from .graph import build_analyst
from .graph.build import async_checkpointer
from .registry import Registry
from .schema import SchemaLinker

log = logging.getLogger(__name__)

STATE: dict[str, Any] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The checkpointer owns its connection for the lifetime of the process;
    # `from_conn_string` would close it when the context exits.
    checkpointer = await async_checkpointer()
    graph, ctx = build_analyst(checkpointer=checkpointer)
    STATE["graph"] = graph
    STATE["ctx"] = ctx
    STATE["linker"] = SchemaLinker(ctx.bundle.snapshot, ctx.bundle.profile)
    log.info("aperture ready: %s tables", len(ctx.bundle.snapshot.tables))
    yield
    STATE.clear()


app = FastAPI(title="Aperture", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1)
    thread_id: str = "web"


def _clean(update: dict) -> dict:
    """Make a node update JSON-safe."""
    out = {}
    for key, value in update.items():
        if key == "rows" and isinstance(value, list):
            out[key] = [[jsonable(v) for v in row] for row in value]
        elif key == "trace":
            out[key] = value[-1:] if value else []
        else:
            out[key] = jsonable(value) if not isinstance(value, (dict, list, type(None))) else value
    return out


@app.post("/ask")
async def ask(request: AskRequest):
    graph = STATE["graph"]

    async def events():
        final: dict = {}
        try:
            async for chunk in graph.astream(
                {"question": request.question},
                {"configurable": {"thread_id": request.thread_id}},
                stream_mode="updates",
            ):
                for node, update in chunk.items():
                    payload = _clean(update)
                    final.update(payload)
                    yield {"event": "node", "data": json.dumps({"node": node, "update": payload})}
        except Exception as err:  # a stream that dies silently is undebuggable
            log.exception("graph failed")
            yield {"event": "error", "data": json.dumps({"message": str(err)})}
            return

        yield {
            "event": "final",
            "data": json.dumps(
                {
                    "answer": final.get("answer"),
                    "sql": final.get("sql"),
                    "columns": final.get("columns", []),
                    "rows": final.get("rows", []),
                    "row_count": final.get("row_count", 0),
                    "chart_spec": final.get("chart_spec"),
                    "assumptions": final.get("assumptions"),
                    "verification": final.get("verification", []),
                    "suggestions": final.get("suggestions", []),
                    "status": final.get("status"),
                    "attempts": final.get("attempts", 0),
                    "identifier_fixes": final.get("identifier_fixes", []),
                    "usage": LEDGER.summary(),
                }
            ),
        }

    return EventSourceResponse(events())


@app.get("/schema")
async def schema():
    ctx = STATE["ctx"]
    return {
        "dialect": ctx.db.dialect,
        "tables": [
            {
                "name": name,
                "rows": ctx.bundle.profile.tables[name].exact_rows
                if name in ctx.bundle.profile.tables
                else 0,
                "columns": [c.name for c in table.columns],
            }
            for name, table in sorted(ctx.bundle.snapshot.tables.items())
        ],
        "empty_tables": ctx.bundle.profile.empty_tables,
        "foreign_keys": len(ctx.bundle.snapshot.foreign_keys),
    }


@app.post("/link")
async def link(request: AskRequest):
    linked = STATE["linker"].link(request.question)
    return {
        "seeds": linked.seeds,
        "tables": linked.tables,
        "value_hints": linked.value_hints,
        "fan_out_warnings": linked.fan_out_warnings,
        "empty_tables": linked.empty_tables,
    }


@app.get("/usage")
async def usage():
    cfg = settings()
    return {
        "calls": LEDGER.total.calls,
        "input_tokens": LEDGER.total.input_tokens,
        "output_tokens": LEDGER.total.output_tokens,
        "estimated_usd": round(LEDGER.spent_usd(), 4),
        "ceiling_usd": cfg.budget_ceiling_usd,
        "model": cfg.bedrock_model_id,
    }


@app.get("/connections")
async def connections():
    registry = Registry.load()
    return {
        "active": registry.active,
        "connections": [
            {
                "name": name,
                "kind": connection.kind,
                "dialect": connection.dialect,
                "target": connection.source or connection.safe_url,
            }
            for name, connection in sorted(registry.connections.items())
        ],
    }


@app.get("/health")
async def health():
    return {"ok": True, "tables": len(STATE["ctx"].bundle.snapshot.tables)}


def ui_directory() -> Path:
    """The built frontend, if it has been compiled."""
    return Path(__file__).resolve().parents[2] / "frontend" / "dist"


def mount_ui(application: FastAPI) -> bool:
    """Serve the built UI from the API, so one process runs the whole product."""
    dist = ui_directory()
    if not (dist / "index.html").exists():
        return False

    application.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @application.get("/", include_in_schema=False)
    async def index():
        return FileResponse(dist / "index.html")

    @application.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        # Anything not matched by an API route is the single-page app.
        candidate = dist / path
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(dist / "index.html")

    return True


UI_MOUNTED = mount_ui(app)
