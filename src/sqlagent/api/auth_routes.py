"""Session exchange, sign-out, and the administrator's approval page."""

from __future__ import annotations

import hashlib
import hmac
import html
import logging
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from sqlagent.auth import Accounts, AuthError, User
from sqlagent.config import settings

logger = logging.getLogger(__name__)

COOKIE = "aperture_session"

# Reachable without a session. Everything else under /api needs one.
PUBLIC_PATHS = frozenset(
    {
        "/api/healthz",
        "/api/auth/session",
        "/api/auth/logout",
        "/api/auth/approve",
    }
)


@lru_cache(maxsize=1)
def get_accounts() -> Accounts:
    return Accounts(settings())


LOCAL_USER = User("local", "local@localhost", "approved")


def _api_key(request: Request, keys: list[str]) -> str | None:
    """The configured key the request presented, if any. Constant-time compare."""
    scheme, _, presented = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not presented:
        return None
    return next((k for k in keys if hmac.compare_digest(k, presented.strip())), None)


async def require_user(request: Request) -> User | None:
    """Fail closed: no valid session for an approved user, no access."""
    path = request.url.path
    if not path.startswith("/api/") or path in PUBLIC_PATHS:
        return None
    config = settings()
    if not config.auth_required:
        return LOCAL_USER
    key = _api_key(request, config.api_key_list)
    if key is not None:
        return User(f"key:{hashlib.sha256(key.encode()).hexdigest()[:12]}", "api-key", "approved")
    accounts = get_accounts()
    user = await run_in_threadpool(accounts.user_for_session, request.cookies.get(COOKIE))
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in required.")
    return user


router = APIRouter(prefix="/api/auth", tags=["auth"])


class Me(BaseModel):
    email: str


@router.post("/session", response_model=Me)
async def start_session(
    response: Response,
    authorization: Annotated[str | None, Header()] = None,
) -> Me:
    """Trade a Neon Auth token for a session cookie, if the user is approved."""
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Sign in required.")
    try:
        user, session = await run_in_threadpool(get_accounts().start_session, token)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    config = settings()
    response.set_cookie(
        COOKIE,
        session,
        max_age=config.session_days * 86400,
        httponly=True,
        samesite="lax",
        secure=config.cookie_secure,
        path="/",
    )
    return Me(email=user.email)


@router.post("/logout")
async def logout(request: Request, response: Response) -> dict:
    await run_in_threadpool(get_accounts().sign_out, request.cookies.get(COOKIE))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/me", response_model=Me)
async def me(user: Annotated[User | None, Depends(require_user)]) -> Me:
    if user is None:  # unreachable: /api/auth/me is not public
        raise HTTPException(status_code=401, detail="Sign in required.")
    return Me(email=user.email)


def _page(body: str) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
        "<title>Aperture approval</title>"
        "<body style='font:16px system-ui;max-width:28rem;margin:4rem auto;padding:0 1rem'>" + body
    )


@router.get("/approve", response_class=HTMLResponse)
async def approve_page(token: str) -> HTMLResponse:
    """Show who is asking. Changes nothing: a GET must not approve anyone."""
    user = await run_in_threadpool(get_accounts().pending_for_token, token)
    if user is None:
        return _page("<p>This link has already been used or is not valid.</p>")
    return _page(
        f"<h2>Sign-up request</h2><p><b>{html.escape(user.email)}</b> wants access.</p>"
        "<form method=post>"
        f"<input type=hidden name=token value='{html.escape(token)}'>"
        "<button name=decision value=approve>Approve</button> "
        "<button name=decision value=reject>Reject</button></form>"
    )


@router.post("/approve", response_class=HTMLResponse)
async def approve_decide(
    token: Annotated[str, Form()], decision: Annotated[str, Form()]
) -> HTMLResponse:
    if decision not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="Unknown decision.")
    try:
        user = await run_in_threadpool(get_accounts().decide, token, decision == "approve")
    except AuthError as exc:
        return _page(f"<p>{html.escape(str(exc))}</p>")
    verb = "approved" if decision == "approve" else "rejected"
    return _page(f"<p><b>{html.escape(user.email)}</b> {verb}.</p>")
