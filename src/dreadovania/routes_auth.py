# SPDX-License-Identifier: GPL-3.0-or-later
"""Sign-in endpoints and the dependency that protects everything else (ADR 0011 §4).

Every route added to ``protected`` requires a valid session. Routes outside it
are public on purpose: setup, login, and server info.
"""

from __future__ import annotations

import base64
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field

from dreadovania import accounts, auth, client_certs, devices, passkeys, remote_access
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


def main_origin(request: Request) -> str | None:
    """The configured public URL wins; otherwise the one chosen in the setup wizard."""
    settings: Settings = request.app.state.settings
    if settings.public_url is not None:
        return auth.origin_of(str(settings.public_url))
    return auth.stored_public_origin(_db(request))


def front_door_origin(request: Request) -> str | None:
    """Where pairing and invites are served when the server checks certificates
    itself (ADR 0023): DREADOVANIA_FRONT_DOOR_URL, else the public URL's host on
    the front-door port."""
    settings: Settings = request.app.state.settings
    if settings.front_door_url is not None:
        return auth.origin_of(str(settings.front_door_url))
    main = main_origin(request)
    if main is None or not settings.front_door_port:
        return None
    parts = urlsplit(main)
    host = parts.hostname or ""
    host = f"[{host}]" if ":" in host else host
    return f"{parts.scheme}://{host}:{settings.front_door_port}"


def on_front_door(request: Request) -> bool:
    peer = request.scope.get(remote_access.PEER_KEY)
    return peer is not None and peer.front_door


def public_origin(request: Request) -> str | None:
    """The origin the request's pages belong to: the front door's on that port."""
    return front_door_origin(request) if on_front_door(request) else main_origin(request)


def uses_front_door(request: Request) -> bool:
    """Whether pairing and invites go to the front-door port: the server checks
    certificates itself and secure remote access is on."""
    remote = getattr(request.app.state, "remote_access", None)
    if remote is None:
        return False
    state = remote.state()
    return state.enabled and state.mode == "server"


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
    presented = presented_certificate(request)
    if presented is not None and presented["device_id"] == device.id:
        # The app connects with its renewed certificate: revoke the one it replaced.
        _certs(request).confirm_renewal(device.owner_id, presented["id"])
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


class DeviceCertificate(BaseModel):
    """A Switch's client certificate (ADR 0023): keep it with the token, present it on
    every connection to this server, and ask for a new one after ``renew_after``.
    The server keeps neither the private key nor the password."""

    p12: str = Field(description="The certificate and its private key, as base64 PKCS#12.")
    password: str
    sha256: str = Field(description="The certificate's SHA-256 fingerprint, in hex.")
    not_after: str
    renew_after: str = Field(
        description="When to ask for a replacement (POST /devices/me/certificate)."
    )


class Paired(BaseModel):
    """Keep ``token`` secret; send it as ``Authorization: Bearer <token>``. It is shown once."""

    device_id: str
    name: str
    token: str
    certificate: DeviceCertificate | None = Field(
        default=None,
        description="This Switch's client certificate, for secure remote access (ADR 0023).",
    )
    server_url: str | None = Field(
        default=None,
        description="The server's main address. Pairing may have been through the front-door"
        " address, which serves nothing else.",
    )


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
    # Always, so the Switch is ready whenever secure remote access is turned on.
    issued = _certs(request).issue(device.owner_id, "device", device.name, device_id=device.id)
    return Paired(
        device_id=device.id, name=device.name, token=token,
        certificate=_device_certificate(request, issued), server_url=main_origin(request),
    )  # fmt: skip


def _device_certificate(request: Request, issued: client_certs.Issued) -> DeviceCertificate:
    cert = issued.certificate
    return DeviceCertificate(
        p12=base64.b64encode(issued.p12).decode("ascii"), password=issued.password,
        sha256=cert["sha256"], not_after=cert["not_after"],
        renew_after=_certs(request).renew_after(cert),
    )  # fmt: skip


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


@protected.post("/devices/me/certificate", tags=["devices"])
def renew_device_certificate(request: Request, user: CurrentUser) -> DeviceCertificate:
    """A new client certificate for this Switch (ADR 0023). Called by the app once
    ``renew_after`` has passed, or to get a first one if it was paired before them.
    The current certificate keeps working until the app connects with the new one;
    it is then revoked."""
    if user.device_id is None:  # pragma: no cover - only device tokens reach this route
        raise ApiProblem(403, "Devices only", "this is for the Switch app")
    certs = _certs(request)
    current = certs.current_for_device(user.user_id, user.device_id)
    if current is None:
        name = next(
            (d["name"] for d in devices.list_devices(_db(request), user.user_id)
             if d["id"] == user.device_id), "Switch",
        )  # fmt: skip
        issued = certs.issue(user.user_id, "device", name, device_id=user.device_id)
    else:
        issued = certs.renew(user.user_id, current["id"])
    return _device_certificate(request, issued)


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
    game_files_bytes: int = Field(description="Disk used by the account's game files.")


def require_admin(user: auth.Session) -> None:
    """Admin-only, and only from a browser: a device token can never manage accounts."""
    if user.device_id is not None:  # pragma: no cover - DEVICE_ROUTES already refuses these
        raise ApiProblem(403, "Not allowed for devices", "sign in to the web app to do this")
    if not user.admin:
        raise ApiProblem(403, "Admin only", "only the server's admin can manage accounts")


def invite_url(request: Request, token: str) -> str | None:
    origin = front_door_origin(request) if uses_front_door(request) else main_origin(request)
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
    request.state.accepted_user_id = user_id
    return username


class InviteAccepted(SignedIn):
    certificate: CertificateIssued | None = Field(
        default=None,
        description="While secure remote access is on: a client certificate for this browser"
        " or phone, shown only now (ADR 0023). Install it to reach the rest of the server.",
    )


def invite_certificate(request: Request, user_id: str) -> client_certs.Issued | None:
    """A new member's first browser certificate, while secure remote access is on:
    without one they couldn't get past the front door they joined through."""
    remote = getattr(request.app.state, "remote_access", None)
    if remote is None or not remote.state().enabled:
        return None
    return _certs(request).issue(user_id, "browser", "First browser")


@public.post("/invites/accept", status_code=status.HTTP_201_CREATED, tags=["accounts"])
def accept_invite(body: AcceptInvite, request: Request, response: Response) -> InviteAccepted:
    """Create an account with an invite link's token, and sign in to it. While secure
    remote access is on, the response also carries the new browser's certificate."""
    username = perform_accept_invite(request, response, body.token, body.username, body.password)
    issued = invite_certificate(request, request.state.accepted_user_id)
    return InviteAccepted(
        username=username,
        certificate=CertificateIssued(
            certificate=_cert_out(issued.certificate),
            p12=base64.b64encode(issued.p12).decode("ascii"), password=issued.password,
        ) if issued else None,
    )  # fmt: skip


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
    game_files = request.app.state.game_files
    return [
        AccountOut(**a, game_files_bytes=game_files.size(a["id"]))
        for a in accounts.list_accounts(_db(request), user.user_id)
    ]


@protected.delete(
    "/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["accounts"]
)
def remove_account(account_id: str, request: Request, user: CurrentUser) -> None:
    """Remove a member and delete everything they own: seeds, presets, game files,
    Switches, passkeys and paired devices. Refused while one of their jobs runs. Admin only."""
    require_admin(user)
    settings: Settings = request.app.state.settings
    try:
        removed = accounts.remove_account(
            _db(request), settings.data_dir, user.user_id, account_id,
            request.app.state.game_files,
        )  # fmt: skip
    except accounts.AccountError as exc:
        raise ApiProblem(409, "Account not removed", str(exc)) from None
    if removed is None:
        raise ApiProblem(404, "Account not found")


# ---- client certificates for secure remote access (ADR 0023) ---------------------------------


class CertificateOut(BaseModel):
    id: str
    kind: str = Field(description="`device` (a paired Switch) or `browser`.")
    name: str
    issuer: str
    sha256: str = Field(description="The certificate's SHA-256 fingerprint, in hex.")
    not_before: str
    not_after: str
    created_at: str
    revoked_at: str | None
    device_id: str | None


class CertificateRequest(BaseModel):
    name: str = Field(
        default="", max_length=client_certs.MAX_NAME,
        description="Which browser or phone it's for, so you can tell them apart.",
    )  # fmt: skip


class CertificateIssued(BaseModel):
    """Install ``p12`` (base64) with ``password`` on the browser or phone. Both are shown
    only now; the server keeps neither the private key nor the password."""

    certificate: CertificateOut
    p12: str = Field(description="The certificate and its private key, as base64 PKCS#12.")
    password: str


def _certs(request: Request) -> client_certs.ClientCertificates:
    return request.app.state.client_certs


@protected.get("/certificates", tags=["remote access"])
def list_certificates(request: Request, user: CurrentUser) -> list[CertificateOut]:
    """Your client certificates that haven't been revoked."""
    _browser_only(user)
    return [CertificateOut(**c) for c in _certs(request).list(user.user_id)]


@protected.post("/certificates", status_code=status.HTTP_201_CREATED, tags=["remote access"])
def create_certificate(
    body: CertificateRequest, request: Request, user: CurrentUser
) -> CertificateIssued:
    """A client certificate for a browser or phone, valid for a year."""
    _browser_only(user)
    issued = _certs(request).issue(user.user_id, "browser", body.name)
    return CertificateIssued(
        certificate=CertificateOut(**issued.certificate),
        p12=base64.b64encode(issued.p12).decode("ascii"),
        password=issued.password,
    )


@protected.delete(
    "/certificates/{certificate_id}", status_code=status.HTTP_204_NO_CONTENT,
    tags=["remote access"],
)  # fmt: skip
def revoke_certificate(certificate_id: str, request: Request, user: CurrentUser) -> None:
    """Revoke one of your certificates: it stops working once the revocation list reloads."""
    _browser_only(user)
    if not _certs(request).revoke(user.user_id, certificate_id):
        raise ApiProblem(404, "Certificate not found")


# ---- secure remote access (ADR 0023) --------------------------------------------------------


class RemoteAccessOut(BaseModel):
    enabled: bool = Field(description="Whether every connection needs a client certificate.")
    mode: str | None = Field(
        description="Who checks: `server` (this server's own HTTPS), `proxy` (a reverse proxy"
        " that ends TLS) or `cloudflare`; null until chosen."
    )
    check_forwarded: bool = Field(
        description="Behind a proxy or Cloudflare: also check the certificate it passes on."
    )
    changed_at: str | None
    serves_tls: bool = Field(description="Whether this server serves HTTPS itself.")
    trusted_proxies: bool = Field(description="Whether DREADOVANIA_TRUSTED_PROXIES is set.")
    via_trusted_proxy: bool = Field(description="Whether this request came from one of them.")
    forwarded_header: bool = Field(
        description="Whether this request carried a certificate header from a trusted proxy."
    )
    presented: CertificateOut | None = Field(
        description="The valid certificate this request came with, if any."
    )
    front_door: list[str] = Field(description="What stays open without a certificate.")
    front_door_url: str | None = Field(
        description="Where pairing and invites go when the server checks certificates itself."
    )
    cloudflare_expression: str = Field(description="The WAF custom rule to block with.")


class RemoteAccessChange(BaseModel):
    enabled: bool
    mode: str | None = Field(default=None, description="`server`, `proxy` or `cloudflare`.")
    check_forwarded: bool = True
    confirmed: bool = Field(
        default=False,
        description="When the proxy checks alone: the admin has checked that this browser"
        " connects with its certificate.",
    )


class PresentedCertificate(BaseModel):
    certificate: CertificateOut | None = Field(
        description="The valid certificate this request came with, if any."
    )
    expires_in_days: int | None = Field(
        description="Days left, when it's a browser certificate expiring within 30 days."
    )


def _remote(request: Request) -> remote_access.RemoteAccess:
    return request.app.state.remote_access


def presented_certificate(request: Request) -> dict[str, Any] | None:
    """The valid certificate this request came with, from TLS or a trusted proxy."""
    remote = getattr(request.app.state, "remote_access", None)
    if remote is None:
        return None
    return remote.certificate_of(request.scope.get(remote_access.PEER_KEY), request.headers)


def _cert_out(cert: dict[str, Any]) -> CertificateOut:
    return CertificateOut(**client_certs.ClientCertificates._public(cert))


def remote_access_status(request: Request) -> dict[str, Any]:
    status_ = _remote(request).status(request.scope.get(remote_access.PEER_KEY), request.headers)
    presented = status_["presented"]
    return status_ | {
        "presented": _cert_out(presented) if presented else None,
        "front_door_url": front_door_origin(request),
    }


def change_remote_access(
    request: Request, user: auth.Session, enabled: bool, mode: str | None, *,
    check_forwarded: bool, confirmed: bool,
) -> None:  # fmt: skip
    """Shared by the API and the settings page. Raises ApiProblem."""
    remote = _remote(request)
    try:
        if not enabled:
            remote.turn_off(user.user_id)
            remote.choose(user.user_id, mode, check_forwarded=check_forwarded)  # type: ignore[arg-type]
            return
        state = remote.state()
        if not state.enabled or state.mode != mode or state.check_forwarded != check_forwarded:
            remote.choose(user.user_id, mode, check_forwarded=check_forwarded)  # type: ignore[arg-type]
        remote.turn_on(user.user_id, presented_certificate(request), confirmed=confirmed)
    except accounts.NotAdmin as exc:
        raise ApiProblem(403, "Admin only", str(exc)) from None
    except remote_access.RemoteAccessError as exc:
        raise ApiProblem(409, "Secure remote access not changed", str(exc)) from None


@protected.get("/remote-access", tags=["remote access"])
def get_remote_access(request: Request, user: CurrentUser) -> RemoteAccessOut:
    """Secure remote access: whether it's on, who checks, and what this request presented."""
    require_admin(user)
    return RemoteAccessOut(**remote_access_status(request))


@protected.put("/remote-access", tags=["remote access"])
def put_remote_access(
    body: RemoteAccessChange, request: Request, user: CurrentUser
) -> RemoteAccessOut:
    """Choose who checks, and turn it on or off.

    Turning it on is refused unless this request shows it will still get through:
    when the server sees certificates, it must have come with one of the admin's
    own; when only the proxy checks, ``confirmed`` must be true.
    """
    require_admin(user)
    change_remote_access(
        request, user, body.enabled, body.mode,
        check_forwarded=body.check_forwarded, confirmed=body.confirmed,
    )  # fmt: skip
    return RemoteAccessOut(**remote_access_status(request))


@protected.get(
    "/remote-access/ca.pem", tags=["remote access"],
    response_class=Response, responses={200: {"content": {"application/x-pem-file": {}}}},
)  # fmt: skip
def get_client_ca(request: Request, user: CurrentUser) -> Response:
    """The built-in CA's certificate, for a proxy's `ca-file`."""
    require_admin(user)
    return Response(
        _certs(request).ca_pem(), media_type="application/x-pem-file",
        headers={"Content-Disposition": 'attachment; filename="dreadovania-client-ca.pem"'},
    )  # fmt: skip


@protected.get(
    "/remote-access/crl.pem", tags=["remote access"],
    response_class=Response, responses={200: {"content": {"application/x-pem-file": {}}}},
)  # fmt: skip
def get_client_crl(request: Request, user: CurrentUser) -> Response:
    """The revocation list, for a proxy's `crl-file`. It changes on every revocation and
    is republished daily; mounting the data folder's copy keeps a proxy up to date."""
    require_admin(user)
    return Response(
        _certs(request).crl_pem(), media_type="application/x-pem-file",
        headers={"Content-Disposition": 'attachment; filename="dreadovania-client-crl.pem"'},
    )  # fmt: skip


@protected.get("/certificates/presented", tags=["remote access"])
def get_presented_certificate(request: Request, user: CurrentUser) -> PresentedCertificate:
    """The certificate this browser connected with, and whether it expires soon."""
    _browser_only(user)
    cert = presented_certificate(request)
    return PresentedCertificate(
        certificate=_cert_out(cert) if cert else None,
        expires_in_days=remote_access.expiring_soon(cert)
        if cert and cert["kind"] == "browser"
        else None,
    )
