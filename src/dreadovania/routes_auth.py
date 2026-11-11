# SPDX-License-Identifier: GPL-3.0-or-later
"""Sign-in endpoints and the dependency that protects everything else (ADR 0011 §4).

Every route added to ``protected`` requires a valid session. Routes outside it
are public on purpose: setup, login, and server info.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field

from dreadovania import accounts, auth, devices, passkeys
from dreadovania.config import Settings
from dreadovania.db import Database
from dreadovania.problems import ApiProblem

API_PREFIX = "/api/v1"
COOKIE = "dreadovania_session"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Limits on sign-in attempts (ADR 0011 §4). Login counts failures; setup counts every try.
LOGIN_LIMIT = {"per_client": 10, "overall": 100, "window_seconds": 15 * 60}
SETUP_LIMIT = {"per_client": 10, "overall": 30, "window_seconds": 15 * 60}
# Starting a passkey sign-in stores a challenge, so it is limited too. Every page
# load of the sign-in page starts one (for passkey autofill), hence the higher limit.
# Exchanging a pairing code is a guess at a secret, so it is limited like sign-in.
PAIR_LIMIT = {"per_client": 10, "overall": 50, "window_seconds": 15 * 60}
PASSKEY_START_LIMIT = {"per_client": 60, "overall": 600, "window_seconds": 15 * 60}
# Accepting an invite is a guess at a secret too; every try counts, as for setup.
INVITE_LIMIT = {"per_client": 10, "overall": 30, "window_seconds": 15 * 60}


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
        # The usual cause is opening the server at another address than the public
        # URL chosen at setup (an IP instead of the hostname, or http behind an
        # https proxy), so say which address to use. It's the server's own address.
        detail = (
            f"this page is open at {origin}, but the server's address is {expected}: "
            f"open {expected} instead"
            if expected is not None
            else "the request's origin isn't allowed"
        )
        raise ApiProblem(403, "Cross-site request refused", detail)


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


def _device_session(request: Request, header: str) -> auth.Session:
    """A paired device's bearer token: only for the routes in DEVICE_ROUTES (ADR 0017).

    No Origin check: a bearer token is never sent automatically by a browser,
    so a cross-site page can't use one.
    """
    scheme, _, token = header.partition(" ")
    device = (
        devices.resolve_token(_db(request), token.strip(), _client(request))
        if scheme.lower() == "bearer"
        else None
    )
    if device is None:
        raise ApiProblem(
            401, "Sign-in required", "this device isn't paired, or was removed: pair it again",
            headers={"WWW-Authenticate": "Bearer"},
        )  # fmt: skip
    route = request.scope.get("route")
    if (request.method, getattr(route, "path", None)) not in devices.DEVICE_ROUTES:
        raise ApiProblem(403, "Not allowed for devices", "sign in to the web app to do this")
    return auth.Session(
        user_id=device.owner_id, username=device.username, renewed=False, device_id=device.id
    )


def require_user(request: Request, response: Response) -> auth.Session:
    db = _db(request)
    if auth.setup_required(db):
        raise ApiProblem(503, "Setup required", "finish first-run setup before using the server")
    header = request.headers.get("authorization")
    if header:
        return _device_session(request, header)
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


class SessionOut(SignedIn):
    admin: bool = Field(description="Whether this is the server's admin account (ADR 0020).")


# ---- routes ------------------------------------------------------------------------------


@public.get("/setup")
def get_setup_status(request: Request) -> SetupStatus:
    """Whether first-run setup is still needed. Reveals nothing else."""
    return SetupStatus(setup_required=auth.setup_required(_db(request)))


def perform_setup(
    request: Request, response: Response, token: str, username: str, password: str, public_url: str
) -> str:
    """The whole setup flow, shared by the API and the web UI. Returns the username.

    Raises ApiProblem; on success the session cookie is set on ``response``.
    """
    limiter: auth.AttemptLimiter = request.app.state.setup_limiter
    client = _client(request)
    try:
        limiter.check(client)
    except auth.RateLimited as exc:
        raise _rate_limited(exc) from None
    limiter.record(client)

    try:
        submitted_origin = auth.origin_of(public_url)
    except auth.AuthError as exc:
        raise ApiProblem(400, "Invalid public URL", str(exc)) from None
    # The browser doing setup must actually be at the URL being saved.
    check_origin(request, submitted_origin, required=False)

    db = _db(request)
    try:
        user_id = auth.complete_setup(db, token, username, password, public_url)
    except auth.SetupAlreadyDone as exc:
        raise ApiProblem(409, "Setup already completed", str(exc)) from None
    except auth.InvalidSetupToken as exc:
        raise ApiProblem(403, "Invalid setup link", str(exc)) from None
    except auth.AuthError as exc:
        raise ApiProblem(400, "Invalid setup details", str(exc)) from None

    _set_session_cookie(response, auth.create_session(db, user_id), public_origin(request))
    return username


def perform_login(request: Request, response: Response, username: str, password: str) -> str:
    """The whole sign-in flow, shared by the API and the web UI. Returns the username.

    Raises ApiProblem; on success the session cookie is set on ``response``.
    """
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
        user_id = auth.authenticate(db, username[:64], password[: auth.MAX_PASSWORD_LENGTH])
    except auth.AuthError as exc:
        limiter.record(client)
        raise ApiProblem(401, "Sign-in failed", str(exc)) from None

    _set_session_cookie(response, auth.create_session(db, user_id), origin)
    with db.connect() as conn:
        return conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()[0]


def perform_logout(request: Request, response: Response) -> None:
    token = request.cookies.get(COOKIE)
    if token:
        auth.delete_session(_db(request), token)
    response.delete_cookie(COOKIE, path="/", httponly=True, samesite="lax")


@public.post("/setup", status_code=status.HTTP_201_CREATED)
def complete_setup(body: SetupRequest, request: Request, response: Response) -> SignedIn:
    """Create the first account with the one-time setup token from the server log."""
    return SignedIn(
        username=perform_setup(
            request, response, body.token, body.username, body.password, body.public_url
        )
    )


@public.post("/auth/login")
def login(body: LoginRequest, request: Request, response: Response) -> SignedIn:
    return SignedIn(username=perform_login(request, response, body.username, body.password))


@protected.get("/auth/session", tags=["sign-in"])
def current_session(user: CurrentUser) -> SessionOut:
    """Who is signed in. Returns 401 when nobody is."""
    return SessionOut(username=user.username, admin=user.admin)


@protected.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT, tags=["sign-in"])
def logout(request: Request, response: Response) -> None:
    perform_logout(request, response)


# ---- passkeys (ADR 0004) -------------------------------------------------------------------


class PasskeyOptions(BaseModel):
    """Pass ``options`` to the browser's WebAuthn call, then send back its response
    with ``challenge_id``. Valid for 5 minutes, once."""

    challenge_id: str
    options: dict[str, Any]


class PasskeySignIn(BaseModel):
    challenge_id: str = Field(max_length=64)
    response: dict[str, Any]


class PasskeyAdd(BaseModel):
    challenge_id: str = Field(max_length=64)
    response: dict[str, Any]
    name: str = Field(default="", max_length=passkeys.MAX_NAME)


class PasskeyOut(BaseModel):
    id: str
    name: str
    rp_id: str
    created_at: str
    last_used_at: str | None


def passkey_party(request: Request) -> passkeys.Party:
    """The hostname passkeys belong to, or 409 explaining why this server can't use them."""
    try:
        return passkeys.party_for(public_origin(request))
    except passkeys.PasskeyError as exc:
        raise ApiProblem(409, "Passkeys unavailable", str(exc)) from None


def perform_passkey_sign_in(
    request: Request, response: Response, challenge_id: str, credential: Any
) -> str:
    """The passkey sign-in flow, shared by the API and the web UI. Returns the username."""
    db = _db(request)
    if auth.setup_required(db):
        raise ApiProblem(503, "Setup required", "finish first-run setup before signing in")
    party = passkey_party(request)
    check_origin(request, party.origin, required=False)
    limiter: auth.AttemptLimiter = request.app.state.login_limiter
    client = _client(request)
    try:
        limiter.check(client)
    except auth.RateLimited as exc:
        raise _rate_limited(exc) from None
    try:
        user_id = passkeys.sign_in(db, party, challenge_id, credential)
    except passkeys.PasskeyError as exc:
        limiter.record(client)
        raise ApiProblem(401, "Sign-in failed", str(exc)) from None
    _set_session_cookie(response, auth.create_session(db, user_id), party.origin)
    with db.connect() as conn:
        return conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()[0]


@public.post("/auth/passkey/options")
def passkey_sign_in_options(request: Request) -> PasskeyOptions:
    """Start signing in with a passkey. No username is needed: the passkey says who."""
    db = _db(request)
    if auth.setup_required(db):
        raise ApiProblem(503, "Setup required", "finish first-run setup before signing in")
    party = passkey_party(request)
    check_origin(request, party.origin, required=False)
    limiter: auth.AttemptLimiter = request.app.state.passkey_start_limiter
    client = _client(request)
    try:
        limiter.check(client)
    except auth.RateLimited as exc:
        raise _rate_limited(exc) from None
    limiter.record(client)
    challenge_id, options = passkeys.sign_in_options(db, party)
    return PasskeyOptions(challenge_id=challenge_id, options=options)


@public.post("/auth/passkey")
def passkey_sign_in(body: PasskeySignIn, request: Request, response: Response) -> SignedIn:
    """Finish signing in with the browser's passkey response."""
    return SignedIn(
        username=perform_passkey_sign_in(request, response, body.challenge_id, body.response)
    )


@protected.get("/passkeys", tags=["sign-in"])
def list_passkeys(request: Request, user: CurrentUser) -> list[PasskeyOut]:
    return [PasskeyOut(**p) for p in passkeys.list_passkeys(_db(request), user.user_id)]


@protected.post("/passkeys/options", tags=["sign-in"])
def add_passkey_options(request: Request, user: CurrentUser) -> PasskeyOptions:
    """Start adding a passkey for the signed-in user."""
    party = passkey_party(request)
    try:
        challenge_id, options = passkeys.registration_options(
            _db(request), party, user.user_id, user.username
        )
    except passkeys.PasskeyError as exc:
        raise ApiProblem(409, "Can't add a passkey", str(exc)) from None
    return PasskeyOptions(challenge_id=challenge_id, options=options)


@protected.post("/passkeys", status_code=status.HTTP_201_CREATED, tags=["sign-in"])
def add_passkey(body: PasskeyAdd, request: Request, user: CurrentUser) -> PasskeyOut:
    """Save the passkey the browser just created."""
    db = _db(request)
    party = passkey_party(request)
    try:
        added = passkeys.register(
            db, party, user.user_id, body.challenge_id, body.response, body.name
        )
    except passkeys.PasskeyError as exc:
        raise ApiProblem(400, "Passkey not added", str(exc)) from None
    return next(PasskeyOut(**p) for p in passkeys.list_passkeys(db, user.user_id)
                if p["id"] == added["id"])  # fmt: skip


@protected.delete(
    "/passkeys/{passkey_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["sign-in"]
)
def remove_passkey(passkey_id: str, request: Request, user: CurrentUser) -> None:
    """Remove a passkey. Password sign-in always remains."""
    if not passkeys.remove(_db(request), user.user_id, passkey_id):
        raise ApiProblem(404, "Passkey not found")


# ---- the Switch app: pairing and devices (ADR 0017) ---------------------------------------


class PairingCode(BaseModel):
    """Type this code into the Switch app. Valid for 5 minutes, once."""

    code: str
    expires_at: str


class PairRequest(BaseModel):
    code: str = Field(max_length=32)
    name: str = Field(default="", max_length=devices.MAX_NAME)


class Paired(BaseModel):
    """Keep ``token`` secret; send it as ``Authorization: Bearer <token>``. It is shown once."""

    device_id: str
    name: str
    token: str


class DeviceOut(BaseModel):
    id: str
    name: str
    created_at: str
    last_used_at: str | None
    last_ip: str | None


def _browser_only(user: auth.Session) -> None:
    if user.device_id is not None:  # pragma: no cover - DEVICE_ROUTES already refuses these
        raise ApiProblem(403, "Not allowed for devices", "sign in to the web app to do this")


@protected.post("/devices/pairing-code", tags=["devices"])
def create_pairing_code(request: Request, user: CurrentUser) -> PairingCode:
    """A one-time code for pairing the Switch app with this account."""
    _browser_only(user)
    code, expires = devices.new_pairing_code(_db(request), user.user_id)
    return PairingCode(code=code, expires_at=expires.isoformat())


@public.post("/devices/pair", status_code=status.HTTP_201_CREATED, tags=["devices"])
def pair_device(body: PairRequest, request: Request) -> Paired:
    """Exchange a pairing code for a device token. Called by the Switch app."""
    db = _db(request)
    if auth.setup_required(db):
        raise ApiProblem(503, "Setup required", "finish first-run setup first")
    limiter: auth.AttemptLimiter = request.app.state.pair_limiter
    client = _client(request)
    try:
        limiter.check(client)
    except auth.RateLimited as exc:
        raise _rate_limited(exc) from None
    try:
        device, token = devices.pair(db, body.code, body.name, client)
    except devices.DeviceError as exc:
        limiter.record(client)
        raise ApiProblem(400, "Pairing failed", str(exc)) from None
    return Paired(device_id=device.id, name=device.name, token=token)


@protected.get("/devices", tags=["devices"])
def list_devices(request: Request, user: CurrentUser) -> list[DeviceOut]:
    return [DeviceOut(**d) for d in devices.list_devices(_db(request), user.user_id)]


@protected.get("/devices/me", tags=["devices"])
def current_device(request: Request, user: CurrentUser) -> DeviceOut:
    """The device making this request (for the Switch app to check its pairing)."""
    found = (
        devices.get_device(_db(request), user.user_id, user.device_id) if user.device_id else None
    )
    if found is None:
        raise ApiProblem(404, "Not a device", "this request isn't from a paired device")
    return DeviceOut(**found)


@protected.delete("/devices/me", status_code=status.HTTP_204_NO_CONTENT, tags=["devices"])
def unpair_self(request: Request, user: CurrentUser) -> None:
    """Unpair the device making this request; its token stops working."""
    if user.device_id is None or not devices.remove(_db(request), user.user_id, user.device_id):
        raise ApiProblem(404, "Not a device", "this request isn't from a paired device")


@protected.delete("/devices/{device_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["devices"])
def remove_device(device_id: str, request: Request, user: CurrentUser) -> None:
    """Revoke a device: its token stops working at once."""
    _browser_only(user)
    if not devices.remove(_db(request), user.user_id, device_id):
        raise ApiProblem(404, "Device not found")


# ---- accounts and invites (ADR 0020) ---------------------------------------------------------


class InviteRequest(BaseModel):
    note: str = Field(
        default="", max_length=accounts.MAX_NOTE,
        description="Who it's for, so you can tell invites apart. Shown only to the admin.",
    )  # fmt: skip


class InviteOut(BaseModel):
    id: str
    note: str
    created_at: str
    expires_at: str


class InviteCreated(InviteOut):
    """Send ``url`` to the person you're inviting. It works once, and is shown only now."""

    token: str
    url: str | None = Field(description="The invite link, when the server's address is known.")


class AcceptInvite(BaseModel):
    token: str = Field(max_length=128)
    username: str = Field(max_length=64)
    password: str = Field(max_length=auth.MAX_PASSWORD_LENGTH)


class AccountOut(BaseModel):
    id: str
    username: str
    role: str = Field(description="`admin` or `member`.")
    created_at: str
    seeds: int


def require_admin(user: auth.Session) -> None:
    """Admin-only, and only from a browser: a device token can never manage accounts."""
    if user.device_id is not None:  # pragma: no cover - DEVICE_ROUTES already refuses these
        raise ApiProblem(403, "Not allowed for devices", "sign in to the web app to do this")
    if not user.admin:
        raise ApiProblem(403, "Admin only", "only the server's admin can manage accounts")


def invite_url(request: Request, token: str) -> str | None:
    origin = public_origin(request)
    return f"{origin}/invite#token={token}" if origin else None


def perform_accept_invite(
    request: Request, response: Response, token: str, username: str, password: str
) -> str:
    """Accepting an invite, shared by the API and the web UI. Returns the username.

    Raises ApiProblem; on success the new account is signed in on ``response``.
    """
    db = _db(request)
    if auth.setup_required(db):
        raise ApiProblem(503, "Setup required", "finish first-run setup first")
    origin = public_origin(request)
    check_origin(request, origin, required=False)
    limiter: auth.AttemptLimiter = request.app.state.invite_limiter
    client = _client(request)
    try:
        limiter.check(client)
    except auth.RateLimited as exc:
        raise _rate_limited(exc) from None
    limiter.record(client)
    try:
        user_id = accounts.accept_invite(db, token, username, password)
    except accounts.InvalidInvite as exc:
        raise ApiProblem(403, "Invalid invite", str(exc)) from None
    except (accounts.AccountError, auth.AuthError) as exc:
        raise ApiProblem(400, "Account not created", str(exc)) from None
    _set_session_cookie(response, auth.create_session(db, user_id), origin)
    return username


@public.post("/invites/accept", status_code=status.HTTP_201_CREATED, tags=["accounts"])
def accept_invite(body: AcceptInvite, request: Request, response: Response) -> SignedIn:
    """Create an account with an invite link's token, and sign in to it."""
    return SignedIn(
        username=perform_accept_invite(request, response, body.token, body.username, body.password)
    )


@protected.get("/invites", tags=["accounts"])
def list_invites(request: Request, user: CurrentUser) -> list[InviteOut]:
    """Invites that haven't been used, revoked or expired. Admin only."""
    require_admin(user)
    return [InviteOut(**i) for i in accounts.list_invites(_db(request), user.user_id)]


@protected.post("/invites", status_code=status.HTTP_201_CREATED, tags=["accounts"])
def create_invite(body: InviteRequest, request: Request, user: CurrentUser) -> InviteCreated:
    """A one-time invite link, valid for 7 days. Admin only."""
    require_admin(user)
    try:
        token, invite = accounts.create_invite(_db(request), user.user_id, body.note)
    except accounts.AccountError as exc:
        raise ApiProblem(409, "Invite not created", str(exc)) from None
    return InviteCreated(**invite, token=token, url=invite_url(request, token))


@protected.delete("/invites/{invite_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["accounts"])
def revoke_invite(invite_id: str, request: Request, user: CurrentUser) -> None:
    """Revoke an unused invite: its link stops working. Admin only."""
    require_admin(user)
    if not accounts.revoke_invite(_db(request), user.user_id, invite_id):
        raise ApiProblem(404, "Invite not found")


@protected.get("/accounts", tags=["accounts"])
def list_accounts(request: Request, user: CurrentUser) -> list[AccountOut]:
    """Every account on this server, the admin first. Admin only."""
    require_admin(user)
    return [AccountOut(**a) for a in accounts.list_accounts(_db(request), user.user_id)]


@protected.delete(
    "/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["accounts"]
)
def remove_account(account_id: str, request: Request, user: CurrentUser) -> None:
    """Remove a member and delete everything they own: seeds, presets, Switches,
    passkeys and paired devices. Refused while one of their jobs runs. Admin only."""
    require_admin(user)
    settings: Settings = request.app.state.settings
    try:
        removed = accounts.remove_account(_db(request), settings.data_dir, user.user_id, account_id)
    except accounts.AccountError as exc:
        raise ApiProblem(409, "Account not removed", str(exc)) from None
    if removed is None:
        raise ApiProblem(404, "Account not found")
