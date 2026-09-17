"""HTTP API.

`/ask` streams the graph's node updates as server-sent events, because the
interesting part of an answer is the path taken to reach it -- especially when
that path loops back through a repair. Everything else here is the product
around that: conversations, connections, uploads.
"""

from __future__ import annotations

import json
import logging
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from .api_deps import (
    analyst_for,
    default_connection,
    find_connection,
    forget_analyst,
    list_connections,
    local_user,
    register_upload,
    remove_connection,
    save_connection,
)
from .budget import LEDGER
from .charts import jsonable
from .config import settings
from .db import Database
from .ingest import load_any, safe_identifier
from .store import (
    User,
    add_message,
    create_conversation,
    delete_conversation,
    get_conversation,
    list_conversations,
    list_messages,
    rename_conversation,
)

log = logging.getLogger(__name__)

# Uploads are held in memory while being written to disk, so cap them.
MAX_UPLOAD_BYTES = 200 * 1024 * 1024


@asynccontextmanager
async def lifespan(app: FastAPI):
    local_user()
    log.info("aperture ready")
    yield


app = FastAPI(title="Aperture", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


async def current_user(authorization: str | None = Header(default=None)) -> User:
    """The requesting user.

    Authentication is optional: with no credentials the local profile is used,
    so every feature works signed out.
    """
    from .auth import user_from_header

    return user_from_header(authorization)


class GoogleCredential(BaseModel):
    credential: str = Field(..., min_length=10, description="Google ID token")


@app.post("/auth/google")
async def sign_in_with_google(body: GoogleCredential):
    """Exchange a Google ID token for an Aperture session."""
    from .auth import auth_enabled, issue_session, verify_google_token

    if not auth_enabled():
        raise HTTPException(400, "sign-in is not configured on this server")
    try:
        user = verify_google_token(body.credential)
    except Exception as err:
        log.warning("google sign-in rejected: %s", err)
        raise HTTPException(401, "could not verify that Google account") from err

    return {
        "token": issue_session(user),
        "user": {"id": user.id, "email": user.email, "name": user.name, "picture": user.picture},
    }


@app.get("/auth/config")
async def auth_config():
    from .auth import auth_enabled

    return {"enabled": auth_enabled(), "client_id": settings().google_client_id}


# --- conversations -----------------------------------------------------------


class NewConversation(BaseModel):
    title: str = "New chat"
    connection: str = ""


class RenameConversation(BaseModel):
    title: str = Field(..., min_length=1, max_length=120)


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1)
    conversation_id: str = ""
    connection: str = ""


@app.get("/me")
async def me(user: User = Depends(current_user)):
    from .auth import auth_enabled

    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "picture": user.picture,
        "anonymous": user.is_anonymous,
        "auth_enabled": auth_enabled(),
    }


@app.get("/conversations")
async def conversations(user: User = Depends(current_user)):
    return [
        {
            "id": c.id,
            "title": c.title,
            "connection": c.connection,
            "updated_at": c.updated_at,
            "messages": c.message_count,
        }
        for c in list_conversations(user.id)
    ]


@app.post("/conversations")
async def new_conversation(body: NewConversation, user: User = Depends(current_user)):
    connection = body.connection
    if not connection:
        chosen = default_connection(user)
        connection = chosen["name"] if chosen else ""
    conversation = create_conversation(user.id, title=body.title, connection=connection)
    return {"id": conversation.id, "title": conversation.title, "connection": connection}


@app.get("/conversations/{conversation_id}")
async def conversation_detail(conversation_id: str, user: User = Depends(current_user)):
    conversation = get_conversation(user.id, conversation_id)
    if not conversation:
        raise HTTPException(404, "no such conversation")
    return {
        "id": conversation.id,
        "title": conversation.title,
        "connection": conversation.connection,
        "messages": [
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "payload": m.payload,
                "created_at": m.created_at,
            }
            for m in list_messages(conversation_id)
        ],
    }


@app.patch("/conversations/{conversation_id}")
async def rename(conversation_id: str, body: RenameConversation, user: User = Depends(current_user)):
    if not rename_conversation(user.id, conversation_id, body.title):
        raise HTTPException(404, "no such conversation")
    return {"ok": True, "title": body.title}


@app.delete("/conversations/{conversation_id}")
async def remove_conversation(conversation_id: str, user: User = Depends(current_user)):
    if not delete_conversation(user.id, conversation_id):
        raise HTTPException(404, "no such conversation")
    return {"ok": True}


# --- asking ------------------------------------------------------------------


def _clean(update: dict) -> dict:
    out: dict[str, Any] = {}
    for key, value in update.items():
        if key == "rows" and isinstance(value, list):
            out[key] = [[jsonable(v) for v in row] for row in value]
        elif key == "trace":
            out[key] = value[-1:] if value else []
        else:
            out[key] = jsonable(value) if not isinstance(value, (dict, list, type(None))) else value
    return out


@app.post("/ask")
async def ask(body: AskRequest, user: User = Depends(current_user)):
    conversation_id = body.conversation_id
    if conversation_id:
        conversation = get_conversation(user.id, conversation_id)
        if not conversation:
            raise HTTPException(404, "no such conversation")
    else:
        conversation = create_conversation(
            user.id, title=body.question[:60], connection=body.connection
        )
        conversation_id = conversation.id

    # A chat is pinned to one connection: "last month" in turn five must mean
    # the same database as turn one.
    name = conversation.connection or body.connection
    chosen = find_connection(user, name) if name else default_connection(user)
    if not chosen:
        raise HTTPException(400, "no connection available; load a file or add a database")

    graph, ctx = analyst_for(chosen["url"])

    # A follow-up amends the previous query, so prior turns travel with it.
    history = []
    pending_question = ""
    for message in list_messages(conversation_id):
        if message.role == "user":
            pending_question = message.content
        elif pending_question:
            history.append({"question": pending_question, "sql": message.payload.get("sql", "")})
            pending_question = ""

    add_message(conversation_id, "user", body.question)

    async def events():
        final: dict = {}
        yield {
            "event": "start",
            "data": json.dumps(
                {"conversation_id": conversation_id, "connection": chosen["name"]}
            ),
        }
        try:
            async for chunk in graph.astream(
                {"question": body.question, "history": history[-3:]},
                {"configurable": {"thread_id": conversation_id}},
                stream_mode="updates",
            ):
                for node, update in chunk.items():
                    payload = _clean(update)
                    final.update(payload)
                    yield {"event": "node", "data": json.dumps({"node": node, "update": payload})}
        except Exception as err:
            log.exception("graph failed")
            yield {"event": "error", "data": json.dumps({"message": str(err)})}
            return

        result = {
            "conversation_id": conversation_id,
            "answer": final.get("answer"),
            "sql": final.get("sql"),
            "columns": final.get("columns", []),
            "rows": final.get("rows", []),
            "row_count": final.get("row_count", 0),
            "chart_spec": final.get("chart_spec"),
            "assumptions": final.get("assumptions"),
            "verification": final.get("verification", []),
            "insights": final.get("insights", []),
            "suggestions": final.get("suggestions", []),
            "status": final.get("status"),
            "attempts": final.get("attempts", 0),
            "identifier_fixes": final.get("identifier_fixes", []),
            "usage": LEDGER.summary(),
        }
        add_message(conversation_id, "assistant", result["answer"] or "", result)
        yield {"event": "final", "data": json.dumps(result)}

    return EventSourceResponse(events())


# --- connections and uploads -------------------------------------------------


class NewConnection(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    url: str = Field(..., min_length=1)


@app.get("/connections")
async def connections(user: User = Depends(current_user)):
    chosen = default_connection(user)
    # `list_connections` already strips URLs; `chosen` carries one, so only its
    # name is exposed.
    return {
        "active": chosen["name"] if chosen else "",
        "connections": list_connections(user),
    }


@app.post("/connections")
async def add_connection(body: NewConnection, user: User = Depends(current_user)):
    try:
        database = Database(body.url)
        database.scalar("SELECT 1")
    except Exception as err:
        raise HTTPException(400, f"could not connect: {str(err)[:200]}") from err

    save_connection(user, name=body.name, url=body.url, kind="database")
    return {"ok": True, "name": body.name, "dialect": database.dialect}


@app.delete("/connections/{name}")
async def drop_connection(name: str, user: User = Depends(current_user)):
    connection = find_connection(user, name)
    if connection:
        forget_analyst(connection["url"])
    if not remove_connection(user, name):
        raise HTTPException(404, "no such connection")
    return {"ok": True}


@app.post("/upload")
async def upload(file: UploadFile = File(...), user: User = Depends(current_user)):
    """Accept a CSV, Excel workbook or SQLite file and register it."""
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {
        ".csv", ".tsv", ".xlsx", ".xlsm", ".db", ".sqlite", ".sqlite3", ".sql", ".dump",
    }:
        raise HTTPException(400, f"unsupported file type: {suffix or 'none'}")

    name = safe_identifier(Path(file.filename or "dataset").stem, fallback="dataset")
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as staged:
        size = 0
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                staged.close()
                Path(staged.name).unlink(missing_ok=True)
                raise HTTPException(413, "file is too large")
            staged.write(chunk)
        staged_path = Path(staged.name)

    try:
        result = load_any(staged_path, dataset=name)
    except Exception as err:
        raise HTTPException(400, f"could not read the file: {str(err)[:200]}") from err
    finally:
        staged_path.unlink(missing_ok=True)

    kind = {
        ".db": "sqlite", ".sqlite": "sqlite", ".sqlite3": "sqlite",
        ".xlsx": "excel", ".xlsm": "excel",
        ".sql": "dump", ".dump": "dump",
    }.get(suffix, "csv")
    register_upload(user, result, name=name, source=file.filename or name, kind=kind)
    forget_analyst(result.database_url)

    return {
        "ok": True,
        "name": name,
        "kind": kind,
        "rows": result.rows,
        "table": result.table,
        "columns": [{"name": c.name, "type": c.sql_type} for c in result.columns],
    }


# --- inspection --------------------------------------------------------------


@app.get("/schema")
async def schema(connection: str = "", user: User = Depends(current_user)):
    chosen = find_connection(user, connection) if connection else default_connection(user)
    if not chosen:
        raise HTTPException(400, "no connection available")
    _, ctx = analyst_for(chosen["url"])
    return {
        "connection": chosen["name"],
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


@app.get("/health")
async def health():
    return {"ok": True}


def ui_directory() -> Path:
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
        candidate = dist / path
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(dist / "index.html")

    return True


UI_MOUNTED = mount_ui(app)
