"""Authentication, mounted on the API.

The dependency is the whole design
----------------------------------
``PrincipalDep`` resolves a request to a tenant or raises 401. Every route that
touches tenant data declares it, and a route that forgets to is caught by a
test that walks the app's own route table — because "remember to add the
dependency" is not a security control, and the one route somebody forgets is
the one that matters.

Two credentials, one outcome
----------------------------
A browser sends a session cookie; everything else sends
``Authorization: Bearer ak_live_...``. Both resolve to a :class:`Principal`.
The header is tried first: an explicit credential should win over an ambient
one, so a developer testing an API key in a browser tab that happens to have a
session gets the key they asked for, not the session they forgot about.

Single-tenant mode
------------------
``require_auth=False`` is the default and keeps the API exactly as it was: one
configured database, no accounts. That is what the CLI, the benchmark harness
and a team running this against their own warehouse need, and forcing accounts
on them would be inventing a problem.

When it is off, :func:`current_principal` returns a fixed local principal
rather than None. A None that downstream code has to check is a fallback
waiting to be forgotten; a real Principal means the scoping code below it runs
identically in both modes, which is the only way that code is ever exercised.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from sqlagent.config import Settings, settings
from sqlagent.saas.auth import AuthError, SuspendedError, authenticate_api_key
from sqlagent.saas.control import ControlStore, EmailTaken
from sqlagent.saas.passwords import PasswordError
from sqlagent.saas.plans import PLANS, Usage, check_quota, clamp_options, plan_for, tier_of
from sqlagent.saas.tenancy import Principal

logger = logging.getLogger(__name__)

router = APIRouter()

LOCAL_TENANT = "local"
"""The tenant a single-tenant deployment runs as.

A real value rather than None so that every scoping path — cache keys, history
filters, usage counting — runs in both modes. Code that only executes when
multi-tenancy is on is code whose bugs are found by customers.
"""


@lru_cache(maxsize=1)
def get_control() -> ControlStore:
    config = settings()
    url = config.control_database_url or str(Path(config.data_dir) / "control.db")
    return ControlStore(url)


def local_principal() -> Principal:
    return Principal(tenant_id=LOCAL_TENANT, via="local", scopes=frozenset({"ask", "read"}))


def current_principal(
    config: Settings,
    control: ControlStore,
    authorization: str | None,
    session: str | None,
) -> Principal:
    """Resolve a request to a tenant, or raise.

    Raises:
        HTTPException: 401 when no credential resolves, 403 when the tenant is
            suspended. Never returns None — the absence of a principal is an
            exception, so nothing downstream has to remember to check.
    """
    if not config.require_auth:
        return local_principal()

    # The explicit credential wins over the ambient one.
    if authorization:
        try:
            return authenticate_api_key(control, authorization)
        except SuspendedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from None
        except AuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from None

    if session:
        tenant_id = control.session_tenant(session)
        if tenant_id:
            tenant = control.tenant(tenant_id)
            if tenant and tenant.active:
                return Principal(tenant_id=tenant.id, via="session")
            if tenant and not tenant.active:
                raise HTTPException(
                    status_code=403, detail="This workspace is suspended."
                )

    raise HTTPException(
        status_code=401,
        detail="Not authenticated. Send an API key as 'Authorization: Bearer <key>'.",
        # Tells a standards-aware client how to authenticate, which is the one
        # useful thing a 401 can say without leaking anything.
        headers={"WWW-Authenticate": "Bearer"},
    )


def principal_dependency(
    authorization: Annotated[str | None, Header()] = None,
    aperture_session: Annotated[str | None, Cookie(alias="aperture_session")] = None,
) -> Principal:
    return current_principal(settings(), get_control(), authorization, aperture_session)


PrincipalDep = Annotated[Principal, Depends(principal_dependency)]


# --------------------------------------------------------------------------
# Request and response models
# --------------------------------------------------------------------------


EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
"""A shape check, not a validity check.

Deliberately loose. RFC 5322 is not expressible in a regex worth reading, and
every attempt to approximate it rejects addresses that work — plus-addressing,
new TLDs, unicode local parts. The only real proof an address exists is sending
to it, which is what a verification email is for.

So this rejects obvious nonsense and nothing else, and `pydantic[email]` is not
pulled in for one field.
"""


class SignUpRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254, pattern=EMAIL_PATTERN)
    password: str = Field(min_length=1, max_length=1024)
    """Bounded, and not as a strength rule: an unbounded input to a
    deliberately slow, memory-hard hash is a way to tie up a worker for free."""

    workspace: str = Field(default="", max_length=80)


class SignInRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=1024)


class AccountResponse(BaseModel):
    tenant_id: str
    workspace: str
    email: str
    plan: str
    plan_label: str


class UsageResponse(BaseModel):
    """What is left this period.

    Both the used and the limit, because a bare percentage cannot be acted on
    and a bare count cannot be interpreted.
    """

    period: str
    plan: str
    questions_used: int
    questions_limit: int
    detailed_used: int
    detailed_limit: int
    connected_databases: int
    connected_limit: int


def _set_session_cookie(response: Response, token: str, config: Settings) -> None:
    response.set_cookie(
        config.session_cookie,
        token,
        # httponly: JavaScript cannot read it, so an XSS bug cannot exfiltrate
        # the session. This is the single highest-value flag here.
        httponly=True,
        # lax, not strict: strict drops the cookie on any cross-site
        # navigation, so arriving from a link in an email logs you out.
        samesite="lax",
        secure=config.secure_cookies,
        max_age=60 * 60 * 24 * 30,
        path="/",
    )


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@router.post("/api/auth/sign-up", response_model=AccountResponse)
async def sign_up(request: SignUpRequest, response: Response) -> AccountResponse:
    config = settings()
    control = get_control()

    try:
        tenant, user = await run_in_threadpool(
            control.create_account,
            email=request.email,
            password=request.password,
            name=request.workspace,
        )
    except EmailTaken:
        # 409, and it does say the address is taken. This one is unavoidable:
        # a signup form that refuses to say "already registered" is unusable,
        # and the same information is available from the form anyway.
        raise HTTPException(status_code=409, detail="That email already has an account.") from None
    except PasswordError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    token = await run_in_threadpool(control.create_session, user)
    _set_session_cookie(response, token, config)

    plan = plan_for("FREE")
    return AccountResponse(
        tenant_id=tenant.id,
        workspace=tenant.name,
        email=user.email,
        plan=plan.name,
        plan_label=plan.label,
    )


@router.post("/api/auth/sign-in", response_model=AccountResponse)
async def sign_in(request: SignInRequest, response: Response) -> AccountResponse:
    config = settings()
    control = get_control()

    user = await run_in_threadpool(control.verify_user, request.email, request.password)
    if user is None:
        # One message for "no such user" and "wrong password". Two would let
        # anyone enumerate which addresses have accounts, and `verify_user`
        # spends the same time either way so the timing does not say what the
        # message refuses to.
        raise HTTPException(status_code=401, detail="Wrong email or password.")

    tenant = await run_in_threadpool(control.tenant, user.tenant_id)
    if tenant is None or not tenant.active:
        raise HTTPException(status_code=403, detail="This workspace is suspended.")

    token = await run_in_threadpool(control.create_session, user)
    _set_session_cookie(response, token, config)

    plan = plan_for(await run_in_threadpool(control.plan_name, tenant.id))
    return AccountResponse(
        tenant_id=tenant.id,
        workspace=tenant.name,
        email=user.email,
        plan=plan.name,
        plan_label=plan.label,
    )


@router.post("/api/auth/sign-out")
async def sign_out(
    response: Response,
    aperture_session: Annotated[str | None, Cookie(alias="aperture_session")] = None,
) -> dict:
    """End the session server-side, then clear the cookie.

    Both, in that order. Clearing the cookie alone leaves the token valid for
    anyone who captured it; deleting the row alone leaves the browser sending a
    dead cookie on every request.
    """
    if aperture_session:
        await run_in_threadpool(get_control().end_session, aperture_session)
    response.delete_cookie(settings().session_cookie, path="/")
    return {"signed_out": True}


@router.get("/api/auth/me", response_model=AccountResponse)
async def me(principal: PrincipalDep) -> AccountResponse:
    """Who the caller is. The endpoint a frontend uses to decide what to show.

    Requires authentication like everything else: an endpoint that reports
    "you are nobody" with a 200 invites a client to treat the answer as a
    session.
    """
    control = get_control()
    tenant = await run_in_threadpool(control.tenant, principal.tenant_id)

    if tenant is None:
        # Single-tenant mode has no tenant row and needs none.
        plan = PLANS["ENTERPRISE"]
        return AccountResponse(
            tenant_id=principal.tenant_id,
            workspace="Local",
            email="",
            plan=plan.name,
            plan_label=plan.label,
        )

    plan = plan_for(await run_in_threadpool(control.plan_name, tenant.id))
    return AccountResponse(
        tenant_id=tenant.id,
        workspace=tenant.name,
        email="",
        plan=plan.name,
        plan_label=plan.label,
    )


@router.get("/api/usage", response_model=UsageResponse)
async def usage(principal: PrincipalDep) -> UsageResponse:
    control = get_control()
    plan = plan_for(await run_in_threadpool(control.plan_name, principal.tenant_id))
    counts = await run_in_threadpool(control.usage, principal.tenant_id)
    connected = await run_in_threadpool(control.count_connections, principal.tenant_id)

    return UsageResponse(
        period=ControlStore.current_period(),
        plan=plan.name,
        questions_used=counts.get("questions", 0),
        questions_limit=plan.questions_per_month,
        detailed_used=counts.get("detailed", 0),
        detailed_limit=plan.detailed_per_month,
        connected_databases=connected,
        connected_limit=plan.max_connected_databases,
    )


# --------------------------------------------------------------------------
# Enforcement, used by the ask routes
# --------------------------------------------------------------------------


def enforce_and_clamp(principal: Principal, options: dict | None) -> dict | None:
    """Check the quota and reduce the request to what the plan allows.

    Raises:
        HTTPException: 402 when the allowance for this kind of question is
            spent. Deliberately *not* raised when a request merely asks for a
            tier the plan does not include — that is clamped, because refusing
            a whole question over one optional field turns an upsell into an
            outage.
    """
    config = settings()
    if not config.require_auth:
        # Returned unchanged, including None. A single-tenant deployment has no
        # plan to clamp against, and turning None into {} here would quietly
        # change what the agent receives on the one path that has no tests
        # about plans to catch it.
        return options

    control = get_control()
    plan = plan_for(control.plan_name(principal.tenant_id))
    counts = control.usage(principal.tenant_id)
    used = Usage(
        questions=counts.get("questions", 0),
        detailed=counts.get("detailed", 0),
        connected_databases=control.count_connections(principal.tenant_id),
    )

    clamped = clamp_options(plan, options or {}, used)
    detailed = tier_of(clamped) == "thorough"

    denial = check_quota(plan, used, detailed=detailed)
    if denial is not None:
        raise HTTPException(status_code=402, detail=denial.render())

    return clamped


def record_question(principal: Principal, options: dict | None) -> None:
    """Count a question after it has been answered.

    After, not before: a question that failed on a model timeout should not
    consume an allowance, and a customer who is charged for an error writes a
    support ticket that costs more than the question did.
    """
    if not settings().require_auth:
        return
    kind = "detailed" if tier_of(options) == "thorough" else "questions"
    try:
        get_control().record_usage(principal.tenant_id, kind)
    except Exception:  # noqa: BLE001 - metering must never break answering
        logger.exception("could not record usage for %s", principal.tenant_id)
