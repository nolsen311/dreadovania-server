# SPDX-License-Identifier: GPL-3.0-or-later
"""Saved Switches and the FTP connection test (ADR 0016, Phase 3 PR 1).

The "Switch" here is a local FTP server (pyftpdlib) serving a temporary folder
laid out like an SD card. No console is involved.
"""

import socket
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import make_user
from fastapi.testclient import TestClient
from test_api import ORIGIN, H

from dreadovania import auth
from dreadovania.app import create_app
from dreadovania.config import Settings
from dreadovania.db import Database
from dreadovania.switches import (
    SwitchError,
    SwitchStore,
    check_connection,
    check_host_syntax,
    clean_fields,
    lan_address,
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---- addresses -------------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["192.168.1.50", "10.0.0.7", "switch.lan", "switch", "fd00::5"])
def test_valid_addresses(host: str) -> None:
    assert check_host_syntax(f"  {host} ") == host


@pytest.mark.parametrize(
    "host", ["", "http://192.168.1.50", "192.168.1.50:5000", "switch lan", "a/b", "-bad.lan"]
)
def test_invalid_addresses_are_explained(host: str) -> None:
    with pytest.raises(SwitchError):
        check_host_syntax(host)


@pytest.mark.parametrize(
    "addresses",
    [["192.168.1.50"], ["10.1.2.3"], ["172.16.0.9"], ["127.0.0.1"], ["169.254.10.1"],
     ["100.64.1.1"], ["fd12::1"], ["fe80::1%eth0"]],
)  # fmt: skip
def test_local_addresses_are_allowed(addresses: list[str]) -> None:
    assert lan_address("x", 5000, lambda _h, _p: addresses) == addresses[0]


@pytest.mark.parametrize(
    "addresses",
    [
        ["8.8.8.8"],
        ["192.168.1.50", "93.184.216.34"],
        ["2606:4700::1111"],
        ["224.0.0.1"],
        ["0.0.0.0"],  # noqa: S104 (an address to refuse, not to bind)
    ],
)
def test_internet_addresses_are_refused(addresses: list[str]) -> None:
    # Every address a name resolves to must be local, not just the first.
    with pytest.raises(SwitchError, match="isn't a local-network address"):
        lan_address("x", 5000, lambda _h, _p: addresses)


def test_an_unknown_name_is_explained() -> None:
    with pytest.raises(SwitchError, match=r"couldn't find nowhere\.lan"):
        lan_address("nowhere.lan", 5000, lambda _h, _p: [])


def test_field_validation() -> None:
    assert clean_fields({"name": " Living room ", "host": "192.168.1.5", "port": "5000"}) == {
        "name": "Living room", "host": "192.168.1.5", "port": 5000,
    }  # fmt: skip
    assert clean_fields({"name": "a", "host": "h", "port": None})["port"] == 5000
    assert clean_fields({"password": ""}, partial=True) == {"password": None}
    with pytest.raises(SwitchError, match="name"):
        clean_fields({"name": "  ", "host": "h"})
    with pytest.raises(SwitchError, match="port"):
        clean_fields({"name": "a", "host": "h", "port": 70000})
    with pytest.raises(SwitchError, match="port"):
        clean_fields({"name": "a", "host": "h", "port": True})
    with pytest.raises(SwitchError, match="invalid characters"):
        clean_fields({"username": "a\r\nPASS x"}, partial=True)


# ---- the store -------------------------------------------------------------------------------


@pytest.fixture
def store(db: Database) -> SwitchStore:
    return SwitchStore(
        db, resolver=lambda host, _p: ["192.168.1.50"] if host != "public" else ["8.8.8.8"]
    )


def test_saving_never_returns_the_password(db: Database, store: SwitchStore) -> None:
    owner = make_user(db, "alice")
    s = store.create(
        owner, {"name": "Switch", "host": "switch.lan", "username": "u", "password": "p"}
    )
    assert "password" not in s
    assert s["has_password"] is True
    assert (s["host"], s["port"], s["username"]) == ("switch.lan", 5000, "u")
    assert store.list(owner) == [s]
    assert store.credentials(owner, s["id"])["password"] == "p"


def test_public_addresses_cant_be_saved(db: Database, store: SwitchStore) -> None:
    with pytest.raises(SwitchError, match="local-network"):
        store.create(make_user(db, "alice"), {"name": "x", "host": "public"})


def test_updating_keeps_or_clears_the_password(db: Database, store: SwitchStore) -> None:
    owner = make_user(db, "alice")
    s = store.create(owner, {"name": "Switch", "host": "switch.lan", "password": "p"})
    s = store.update(owner, s["id"], {"name": "Renamed"})
    assert (s["name"], s["has_password"]) == ("Renamed", True)
    s = store.update(owner, s["id"], {"password": ""})
    assert s["has_password"] is False
    with pytest.raises(SwitchError):
        store.update(owner, s["id"], {"host": "public"})


def test_switches_are_owner_scoped(db: Database, store: SwitchStore) -> None:
    alice, bob = make_user(db, "alice"), make_user(db, "bob")
    s = store.create(alice, {"name": "Switch", "host": "switch.lan"})
    assert store.get(bob, s["id"]) is None
    assert store.update(bob, s["id"], {"name": "x"}) is None
    assert store.credentials(bob, s["id"]) is None
    assert not store.delete(bob, s["id"])
    assert store.list(bob) == []
    assert store.delete(alice, s["id"])
    assert store.list(alice) == []


# ---- connecting to ftpd ----------------------------------------------------------------------


def test_a_working_switch(ftpd: dict) -> None:
    result = check_connection({"host": ftpd["host"], "port": ftpd["port"]})
    assert result.ok, result.message
    assert result.message == "Connected to ftpd and found the Atmosphere folder."


def test_login_with_a_username_and_password(ftpd: dict) -> None:
    target = {"host": ftpd["host"], "port": ftpd["port"], "username": "samus"}
    assert check_connection({**target, "password": "secret"}).ok
    wrong = check_connection({**target, "password": "nope"})
    assert not wrong.ok
    assert "refused the login" in wrong.message


def test_a_card_without_atmosphere(ftpd: dict) -> None:
    (ftpd["root"] / "atmosphere" / "contents").rmdir()
    (ftpd["root"] / "atmosphere").rmdir()
    result = check_connection({"host": ftpd["host"], "port": ftpd["port"]})
    assert not result.ok
    assert "no /atmosphere folder" in result.message


def test_blocked_file_transfers_fail_the_test(ftpd_blocked: dict) -> None:
    # Logging in works, but a firewall blocks ftpd's file-transfer ports.
    result = check_connection({"host": ftpd_blocked["host"], "port": ftpd_blocked["port"]})
    assert not result.ok
    assert result.message.startswith("Logged in to ftpd, but couldn't open a file transfer")
    assert "ports 1024-65535" in result.message


def test_ftpd_not_running() -> None:
    port = free_port()
    result = check_connection({"host": "127.0.0.1", "port": port})
    assert not result.ok
    assert f"refused the connection on port {port}" in result.message


def test_a_test_result_is_remembered(db: Database, ftpd: dict) -> None:
    store = SwitchStore(db)
    owner = make_user(db, "alice")
    s = store.create(owner, {"name": "Switch", "host": ftpd["host"], "port": ftpd["port"]})
    assert store.test(owner, s["id"]).ok
    saved = store.get(owner, s["id"])
    assert saved["last_test_ok"] is True
    assert saved["last_test_message"].startswith("Connected")
    assert saved["last_test_at"]
    # Changing the address forgets the old result.
    assert store.update(owner, s["id"], {"port": ftpd["port"]})["last_test_ok"] is None


# ---- API and UI ------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, fake_runner) -> Iterator[TestClient]:
    app = create_app(Settings(data_dir=tmp_path / "data"), runner=fake_runner)
    with TestClient(app, base_url=ORIGIN) as c:
        token = auth.create_setup_token(app.state.db)
        r = c.post(
            "/api/v1/setup",
            json={"token": token, "username": "samus", "password": "correct horse battery",
                  "public_url": ORIGIN},
            headers=H,
        )  # fmt: skip
        assert r.status_code == 201
        yield c


def test_api_switches(client: TestClient, ftpd: dict) -> None:
    body = {"name": "Switch", "host": ftpd["host"], "port": ftpd["port"], "password": "pw"}
    r = client.post("/api/v1/switches", json=body, headers=H)
    assert r.status_code == 201, r.text
    s = r.json()
    assert "password" not in s
    assert s["has_password"] is True
    assert client.get("/api/v1/switches").json() == [s]
    assert client.get(f"/api/v1/switches/{s['id']}").json() == s

    r = client.patch(f"/api/v1/switches/{s['id']}", json={"password": None}, headers=H)
    assert r.json()["has_password"] is False
    r = client.patch(f"/api/v1/switches/{s['id']}", json={"name": "Renamed"}, headers=H)
    assert r.json()["name"] == "Renamed"

    r = client.post(f"/api/v1/switches/{s['id']}/test", headers=H)
    assert r.json() == {"ok": True, "message": "Connected to ftpd and found the Atmosphere folder."}

    assert client.delete(f"/api/v1/switches/{s['id']}", headers=H).status_code == 204
    assert client.get(f"/api/v1/switches/{s['id']}").status_code == 404


def test_api_refuses_public_and_invalid_addresses(client: TestClient) -> None:
    r = client.post("/api/v1/switches", json={"name": "x", "host": "8.8.8.8"}, headers=H)
    assert r.status_code == 400
    assert "local-network" in r.json()["detail"]
    r = client.post("/api/v1/switches", json={"name": "x", "host": "http://switch"}, headers=H)
    assert r.status_code == 400


def test_api_switches_need_sign_in_and_origin(client: TestClient) -> None:
    body = {"name": "x", "host": "127.0.0.1"}
    assert client.post("/api/v1/switches", json=body).status_code == 403
    missing = "00000000-0000-4000-8000-000000000000"
    assert client.post(f"/api/v1/switches/{missing}/test", headers=H).status_code == 404
    client.cookies.clear()
    assert client.get("/api/v1/switches").status_code == 401


def test_switches_page(client: TestClient, ftpd: dict) -> None:
    page = client.get("/switches").text
    assert "Add a Switch" in page
    assert 'href="/switches"' in client.get("/").text

    form = {"name": "Living room", "host": ftpd["host"], "port": str(ftpd["port"]),
            "username": "", "password": ""}  # fmt: skip
    r = client.post("/switches", data=form, headers={**H, "HX-Request": "true"})
    assert r.status_code == 204
    page = client.get("/switches").text
    assert "Living room" in page
    assert f"{ftpd['host']}:{ftpd['port']}" in page

    switch_id = client.get("/api/v1/switches").json()[0]["id"]
    result = client.post(f"/switches/{switch_id}/test", headers={**H, "HX-Request": "true"}).text
    assert "found the Atmosphere folder" in result

    bad = client.post(
        "/switches", data={**form, "host": "8.8.8.8"}, headers={**H, "HX-Request": "true"}
    )
    assert "local-network" in bad.text
