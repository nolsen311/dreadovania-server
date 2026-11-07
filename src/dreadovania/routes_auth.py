# SPDX-License-Identifier: GPL-3.0-or-later
"""Sign-in endpoints and the dependency that protects everything else (ADR 0011 §4).

Every route added to ``protected`` requires a valid session. Routes outside it
are public on purpose: setup, login, and server info.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field

from dreadovania import auth
from dreadovania.config import Settings
from dreadovania.db import Database
from dreadovania.problems import ApiProblem

API_PREFIX = "/api/v1"
COOKIE = "dreadovania_session"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Limits on sign-in attempts (ADR 0011 §4). Login counts failures; setup counts every try.
LOGIN_LIMIT = {"per_client": 10, "overall": 100, "window_seconds": 15 * 60}
SETUP_LIMIT = {"per_client": 10, "overall": 30, "window_seconds": 15 * 60}


# ---- helpers ------------------------------------------------------------------------


def _db(request: Request) -> Database:
    return request.app.state.db


def public_origin(request: Request) -> str | None:
    """The configured public URL wins; otherwise the one chosen in the setup wizard."""
    settings: Settings = request.app.state.settings
    if settings.public_url is not None:
        return auth.origin_of(str(settings.public_url))
    return auth.stored_public_origin(_db(request))


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def check_origin(request: Request, expected: str | None, *, required: bool) -> None:
    """CSRF defence: a browser always sends Origin on these requests.

    ``required`` is used for cookie-authenticated changes. For login and setup a
    missing Origin is allowed (a non-browser client can't be tricked into a
    cross-site request), but a mismatched one is always refused.
    """
    origin = request.headers.get("origin")
    if origin is None:
        if required:
            raise ApiProblem(403, "Origin required", "this request must come from the web app")
        return
    try:
        matches = expected is not None and auth.origin_of(origin) == expected
    except auth.AuthError:
        matches = False  # e.g. "null" from a sandboxed page
    if not matches:
        raise ApiProblem(403, "Cross-site request refused", "the request's origin isn't allowed")


def _set_session_cookie(response: Response, token: str, origin: str | None) -> None:
    response.set_cookie(
        COOKIE,
        token,
        max_age=int(auth.SESSION_LIFETIME.total_seconds()),
        path="/",
        httponly=True,
        samesite="lax",
        secure=bool(origin and origin.startswith("https://")),
    )


def _rate_limited(exc: auth.RateLimited) -> ApiProblem:
    return ApiProblem(429, "Too many attempts", str(exc), {"Retry-After": str(exc.retry_after)})


# ---- the protection dependency ---------------------------------------------------------


def require_user(request: Request, response: Response) -> auth.Session:
    db = _db(request)
    if auth.setup_required(db):
        raise ApiProblem(503, "Setup required", "finish first-run setup before using the server")
    token = request.cookies.get(COOKIE)
    session = auth.resolve_session(db, token) if token else None
    if session is None:
        raise ApiProblem(401, "Sign-in required")
    origin = public_origin(request)
    if request.method not in SAFE_METHODS:
        check_origin(request, origin, required=True)
    if session.renewed and token:
        _set_session_cookie(response, token, origin)
    return session


CurrentUser = Annotated[auth.Session, Depends(require_user)]

public = APIRouter(prefix=API_PREFIX, tags=["sign-in"])
protected = APIRouter(prefix=API_PREFIX, dependencies=[Depends(require_user)])


# ---- request and response bodies ---------------------------------------------------------


class SetupStatus(BaseModel):
    setup_required: bool


class SetupRequest(BaseModel):
    token: str = Field(max_length=128)
    username: str = Field(max_length=64)
    password: str = Field(max_length=auth.MAX_PASSWORD_LENGTH)
    public_url: str = Field(max_length=2048)


class LoginRequest(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=auth.MAX_PASSWORD_LENGTH)


class SignedIn(BaseModel):
    username: str


# ---- routes ------------------------------------------------------------------------------


@public.get("/setup")
def get_setup_status(request: Request) -> SetupStatus:
    """Whether first-run setup is still needed. Reveals nothing else."""
    return SetupStatus(setup_required=auth.setup_required(_db(request)))


@public.post("/setup", status_code=status.HTTP_201_CREATED)
def complete_setup(body: SetupRequest, request: Request, response: Response) -> SignedIn:
    """Create the first account with the one-time setup token from the server log."""
    limiter: auth.AttemptLimiter = request.app.state.setup_limiter
    client = _client(request)
    try:
        limiter.check(client)
    except auth.RateLimited as exc:
        raise _rate_limited(exc) from None
    limiter.record(client)

    try:
        submitted_origin = auth.origin_of(body.public_url)
    except auth.AuthError as exc:
        raise ApiProblem(400, "Invalid public URL", str(exc)) from None
    # The browser doing setup must actually be at the URL being saved.
    check_origin(request, submitted_origin, required=False)

    db = _db(request)
    try:
        user_id = auth.complete_setup(db, body.token, body.username, body.password, body.public_url)
    except auth.SetupAlreadyDone as exc:
        raise ApiProblem(409, "Setup already completed", str(exc)) from None
    except auth.InvalidSetupToken as exc:
        raise ApiProblem(403, "Invalid setup link", str(exc)) from None
    except auth.AuthError as exc:
        raise ApiProblem(400, "Invalid setup details", str(exc)) from None

    _set_session_cookie(response, auth.create_session(db, user_id), public_origin(request))
    return SignedIn(username=body.username)


@public.post("/auth/login")
def login(body: LoginRequest, request: Request, response: Response) -> SignedIn:
    db = _db(request)
    if auth.setup_required(db):
        raise ApiProblem(503, "Setup required", "finish first-run setup before signing in")
    origin = public_origin(request)
    check_origin(request, origin, required=False)

    limiter: auth.AttemptLimiter = request.app.state.login_limiter
    client = _client(request)
    try:
        limiter.check(client)
    except auth.RateLimited as exc:
        raise _rate_limited(exc) from None
    try:
        user_id = auth.authenticate(db, body.username, body.password)
    except auth.AuthError as exc:
        limiter.record(client)
        raise ApiProblem(401, "Sign-in failed", str(exc)) from None

    _set_session_cookie(response, auth.create_session(db, user_id), origin)
    with db.connect() as conn:
        username = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()[0]
    return SignedIn(username=username)


@protected.get("/auth/session", tags=["sign-in"])
def current_session(user: CurrentUser) -> SignedIn:
    """Who is signed in. Returns 401 when nobody is."""
    return SignedIn(username=user.username)


@protected.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT, tags=["sign-in"])
def logout(request: Request, response: Response) -> None:
    token = request.cookies.get(COOKIE)
    if token:
        auth.delete_session(_db(request), token)
    response.delete_cookie(COOKIE, path="/", httponly=True, samesite="lax")
