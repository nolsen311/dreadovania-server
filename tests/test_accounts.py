# SPDX-License-Identifier: GPL-3.0-or-later
"""Accounts for friends on the same server: roles, invites and removal (ADR 0020)."""

import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import FakeRunner
from fastapi.testclient import TestClient
from test_api import ORIGIN, H, _client, use_session, wait_for_job
from test_devices import as_device, pair, pairing_code

from dreadovania import accounts, auth
from dreadovania.db import Database

PASSWORD = "another good password"
HX = H | {"HX-Request": "true"}


@pytest.fixture
def client(tmp_path: Path, fake_runner: FakeRunner) -> Iterator[TestClient]:
    """Signed in as samus, the admin created at setup."""
    yield from _client(tmp_path, fake_runner)


def invite(client: TestClient, note: str = "Ridley") -> dict:
    r = client.post("/api/v1/invites", json={"note": note}, headers=H)
    assert r.status_code == 201, r.text
    return r.json()


def join(client: TestClient, token: str, username: str = "ridley", **kw):
    body = {"token": token, "username": username, "password": PASSWORD} | kw
    return client.post("/api/v1/invites/accept", json=body, headers=H)


def as_member(client: TestClient, username: str = "ridley") -> str:
    """Invite a member and switch the client to their session. Returns the admin's cookie."""
    admin_cookie = client.cookies["dreadovania_session"]
    created = invite(client)
    client.cookies.clear()
    assert join(client, created["token"], username).status_code == 201
    return admin_cookie


# ---- roles ------------------------------------------------------------------------------------


def test_the_setup_account_is_the_admin(client: TestClient) -> None:
    assert client.get("/api/v1/auth/session").json() == {"username": "samus", "admin": True}


def test_upgrading_makes_the_existing_account_the_admin(tmp_path: Path, monkeypatch) -> None:
    db = Database(tmp_path / "d.db")
    monkeypatch.setattr(db, "_migrations", [m for m in db._migrations if m[0] <= 8])
    db.migrate()
    with db.connect() as conn:
        conn.execute("INSERT INTO users (id, created_at, username) VALUES ('a', '1', 'samus')")
    monkeypatch.undo()
    db.migrate()
    assert accounts.is_admin(db, "a")


# ---- invites ----------------------------------------------------------------------------------


def test_an_invite_link_creates_a_signed_in_member(client: TestClient) -> None:
    created = invite(client)
    assert created["note"] == "Ridley"
    assert created["url"] == f"{ORIGIN}/invite#token={created['token']}"  # in the fragment
    listed = client.get("/api/v1/invites", headers=H).json()
    assert [i["id"] for i in listed] == [created["id"]]
    assert "token" not in listed[0]  # shown once, never again

    client.cookies.clear()
    r = join(client, created["token"])
    assert r.status_code == 201, r.text
    assert client.get("/api/v1/auth/session").json() == {"username": "ridley", "admin": False}
    client.cookies.clear()
    assert client.post("/api/v1/auth/login", json={"username": "ridley", "password": PASSWORD},
                       headers=H).status_code == 200  # fmt: skip


def test_an_invite_works_once(client: TestClient) -> None:
    token = invite(client)["token"]
    assert join(client, token, "ridley").status_code == 201
    again = join(client, token, "kraid")
    assert again.status_code == 403
    assert "invalid, used or expired" in again.json()["detail"]


def test_revoked_expired_and_made_up_invites_are_refused(client: TestClient) -> None:
    revoked = invite(client)
    assert client.delete(f"/api/v1/invites/{revoked['id']}", headers=H).status_code == 204
    assert client.delete(f"/api/v1/invites/{revoked['id']}", headers=H).status_code == 404
    expired = invite(client)
    with client.app.state.db.connect() as conn:
        past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        conn.execute("UPDATE invites SET expires_at = ? WHERE id = ?", (past, expired["id"]))
    assert client.get("/api/v1/invites", headers=H).json() == []  # expired ones are pruned
    for token in (revoked["token"], expired["token"], "made-up"):
        assert join(client, token).status_code == 403


def test_usernames_and_passwords_are_checked(client: TestClient) -> None:
    token = invite(client)["token"]
    assert join(client, token, "SAMUS").status_code == 400  # taken, whatever the case
    assert join(client, token, "bad name").status_code == 400
    assert join(client, token, password="short").status_code == 400
    assert join(client, token).status_code == 201  # a refused try doesn't use up the invite


def test_unused_invites_are_limited(client: TestClient) -> None:
    for _ in range(accounts.MAX_LIVE_INVITES):
        invite(client)
    r = client.post("/api/v1/invites", json={}, headers=H)
    assert r.status_code == 409
    assert "revoke some first" in r.json()["detail"]


def test_accepting_invites_is_rate_limited(client: TestClient) -> None:
    client.cookies.clear()
    for _ in range(10):
        assert join(client, "guess").status_code == 403
    assert join(client, "guess").status_code == 429


def test_accepting_an_invite_from_another_site_is_refused(client: TestClient) -> None:
    token = invite(client)["token"]
    r = client.post("/api/v1/invites/accept", json={"token": token, "username": "ridley",
                    "password": PASSWORD}, headers={"Origin": "https://evil.test"})  # fmt: skip
    assert r.status_code == 403
    assert join(client, token).status_code == 201  # still unused


# ---- members can't manage accounts ----------------------------------------------------------


ADMIN_ROUTES = [("GET", "/api/v1/invites"), ("POST", "/api/v1/invites"),
                ("DELETE", "/api/v1/invites/x"), ("GET", "/api/v1/accounts"),
                ("DELETE", "/api/v1/accounts/x")]  # fmt: skip


def test_members_cant_reach_admin_routes(client: TestClient) -> None:
    as_member(client)
    for method, path in ADMIN_ROUTES:
        r = client.request(method, path, json={}, headers=H)
        assert r.status_code == 403, (method, path)
        assert r.json()["title"] == "Admin only"
    for path in ("/account/invites", "/account/invites/x/revoke", "/account/members/x/remove"):
        assert client.post(path, data={}, headers=HX).status_code == 403, path
    page = client.get("/account").text
    assert "Create an invite link" not in page and 'id="accounts"' not in page


def test_devices_cant_reach_admin_routes_even_the_admins(client: TestClient) -> None:
    device = pair(client, pairing_code(client))
    client.cookies.clear()
    for method, path in ADMIN_ROUTES:
        r = client.request(method, path, json={}, headers=as_device(device["token"]))
        assert r.status_code == 403, (method, path)
        assert r.json()["title"] == "Not allowed for devices"


def test_the_admin_sees_every_account(client: TestClient) -> None:
    admin_cookie = as_member(client)
    use_session(client, admin_cookie)
    listed = client.get("/api/v1/accounts", headers=H).json()
    assert [(a["username"], a["role"]) for a in listed] == [("samus", "admin"),
                                                           ("ridley", "member")]  # fmt: skip
    page = client.get("/account").text
    assert "ridley" in page and "Create an invite link" in page


# ---- the server's game files stay the admin's (until ADR 0020 §3) ----------------------------


def test_members_cant_use_or_replace_the_servers_game_files(client: TestClient) -> None:
    as_member(client)
    romfs = client.get("/api/v1/romfs", headers=H).json()
    assert romfs["ready"] is False and romfs["upload_allowed"] is False
    assert "invited accounts" in romfs["message"]
    upload = client.post("/api/v1/romfs/upload", json={"size": 1, "fingerprint": "x"}, headers=H)
    assert upload.status_code == 403
    assert client.get("/api/v1/romfs/upload", headers=H).status_code == 403
    assert "invited accounts" in client.get("/game-files").text

    r = client.post("/api/v1/seeds", json={"preset_id": "starter_preset"}, headers=H)
    assert r.status_code == 202  # generating works
    seed_id = r.json()["seed_id"]
    assert wait_for_job(client, r.json()["job_id"])["status"] == "succeeded"
    patch = client.post(f"/api/v1/seeds/{seed_id}/mod", headers=H)
    assert patch.status_code == 409
    assert "your own game files" in patch.json()["detail"]


def test_from_the_switch_app_too_only_the_admin_can_patch(client: TestClient) -> None:
    def patch_from_switch() -> str:
        device = pair(client, pairing_code(client))
        r = client.post("/api/v1/seeds", json={"preset_id": "starter_preset"}, headers=H)
        assert wait_for_job(client, r.json()["job_id"])["status"] == "succeeded"
        cookie = client.cookies["dreadovania_session"]
        client.cookies.clear()
        patch = client.post(f"/api/v1/seeds/{r.json()['seed_id']}/mod",
                            headers=as_device(device["token"]))  # fmt: skip
        use_session(client, cookie)
        assert patch.status_code == 409  # this test server has no game files at all
        return patch.json()["detail"]

    assert "your own game files" not in patch_from_switch()  # the admin's Switch
    as_member(client)
    assert "your own game files" in patch_from_switch()  # a member's Switch


# ---- removing an account ------------------------------------------------------------------------


def test_removing_a_member_deletes_everything_they_own(client: TestClient) -> None:
    admin_cookie = as_member(client)
    member_cookie = client.cookies["dreadovania_session"]
    r = client.post("/api/v1/seeds", json={"preset_id": "starter_preset"}, headers=H)
    assert wait_for_job(client, r.json()["job_id"])["status"] == "succeeded"
    data_dir = client.app.state.settings.data_dir
    with client.app.state.db.connect() as conn:
        member_id = conn.execute("SELECT id FROM users WHERE username = 'ridley'").fetchone()[0]
    assert (data_dir / "seeds" / member_id).is_dir()

    use_session(client, admin_cookie)
    assert client.delete(f"/api/v1/accounts/{member_id}", headers=H).status_code == 204
    assert not (data_dir / "seeds" / member_id).exists()
    with client.app.state.db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM seeds WHERE owner_id = ?",
                            (member_id,)).fetchone()[0] == 0  # fmt: skip
    use_session(client, member_cookie)
    assert client.get("/api/v1/auth/session").status_code == 401  # signed out for good
    use_session(client, admin_cookie)
    assert client.delete(f"/api/v1/accounts/{member_id}", headers=H).status_code == 404


def test_the_admin_and_busy_accounts_cant_be_removed(client: TestClient, fake_runner) -> None:
    admin_cookie = as_member(client)
    fake_runner.release.clear()
    r = client.post("/api/v1/seeds", json={"preset_id": "starter_preset"}, headers=H)
    assert r.status_code == 202
    use_session(client, admin_cookie)
    with client.app.state.db.connect() as conn:
        ids = dict(conn.execute("SELECT username, id FROM users").fetchall())
    busy = client.delete(f"/api/v1/accounts/{ids['ridley']}", headers=H)
    assert busy.status_code == 409 and "job waiting or running" in busy.json()["detail"]
    admin = client.delete(f"/api/v1/accounts/{ids['samus']}", headers=H)
    assert admin.status_code == 409 and "can't be removed" in admin.json()["detail"]
    fake_runner.release.set()


def test_removing_on_the_account_page_needs_the_username_typed(client: TestClient) -> None:
    admin_cookie = as_member(client)
    use_session(client, admin_cookie)
    with client.app.state.db.connect() as conn:
        member_id = conn.execute("SELECT id FROM users WHERE username = 'ridley'").fetchone()[0]
    path = f"/account/members/{member_id}/remove"
    wrong = client.post(path, data={"confirm": "Ridley"}, headers=HX)
    assert "type ridley exactly" in wrong.text
    assert len(client.get("/api/v1/accounts", headers=H).json()) == 2
    right = client.post(path, data={"confirm": "ridley"}, headers=HX)
    assert right.headers.get("hx-redirect") == "/account"
    assert len(client.get("/api/v1/accounts", headers=H).json()) == 1


# ---- the web pages ------------------------------------------------------------------------------


def test_invite_pages(client: TestClient) -> None:
    shown = client.post("/account/invites", data={"note": "Kraid"}, headers=HX)
    assert shown.status_code == 200
    url = re.search(r'value="(https://dread\.test/invite#token=[\w-]+)"', shown.text).group(1)
    token = url.split("#token=")[1]
    assert "Kraid" in client.get("/account").text  # listed as an unused invite

    client.cookies.clear()
    page = client.get("/invite")
    assert page.status_code == 200 and 'id="invite-token"' in page.text
    joined = client.post("/invite", data={"token": token, "username": "kraid",
                                          "password": PASSWORD}, headers=HX)  # fmt: skip
    assert joined.headers.get("hx-redirect") == "/"
    assert client.get("/api/v1/auth/session").json() == {"username": "kraid", "admin": False}
    used = client.post("/invite", data={"token": token, "username": "kraid2",
                                        "password": PASSWORD}, headers=HX)  # fmt: skip
    assert "invalid, used or expired" in used.text


def test_revoking_on_the_account_page(client: TestClient) -> None:
    created = invite(client)
    r = client.post(f"/account/invites/{created['id']}/revoke", headers=HX)
    assert r.headers.get("hx-redirect") == "/account"
    assert client.get("/api/v1/invites", headers=H).json() == []
    assert join(client, created["token"]).status_code == 403


def test_stored_invites_are_only_hashes(client: TestClient) -> None:
    token = invite(client)["token"]
    with client.app.state.db.connect() as conn:
        stored = conn.execute("SELECT * FROM invites").fetchone()
    assert token not in [str(v) for v in dict(stored).values()]
    assert stored["token_hash"] == auth._hash_token(token)
