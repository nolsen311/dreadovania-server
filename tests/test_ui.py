# SPDX-License-Identifier: GPL-3.0-or-later
"""The mobile web UI (ADR 0011 PR 4): pages, forms, and the security rules."""

import hashlib
import json
import re
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import FakeRunner
from fastapi.testclient import TestClient

from dreadovania import auth
from dreadovania.app import create_app
from dreadovania.config import Settings

ORIGIN = "https://dread.test"
H = {"Origin": ORIGIN}
HX = {"Origin": ORIGIN, "HX-Request": "true"}
PKG = Path(__file__).resolve().parent.parent / "src" / "dreadovania"
HTMX = PKG / "static" / "vendor" / "htmx-2.0.11.min.js"
HTMX_SHA256 = "d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717"


@pytest.fixture
def fresh(tmp_path: Path, fake_runner: FakeRunner) -> Iterator[TestClient]:
    app = create_app(Settings(data_dir=tmp_path / "data"), runner=fake_runner)
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as client:
        yield client


def form_setup(client: TestClient, **overrides: str):
    data = {"token": auth.create_setup_token(client.app.state.db), "username": "samus",
            "password": "correct horse battery", "public_url": ORIGIN} | overrides  # fmt: skip
    return client.post("/setup", data=data, headers=HX)


@pytest.fixture
def signed_in(fresh: TestClient) -> TestClient:
    assert form_setup(fresh).headers.get("hx-redirect") == "/"
    return fresh


def _all_routes(routes, prefix: str = ""):
    """Flatten routes, including those inside included routers."""
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from _all_routes(inner.routes, prefix + inner.prefix)
        else:
            yield prefix, route


def ui_routes(client: TestClient) -> list[tuple[str, str]]:
    """Every UI route that changes something, except sign-in and joining by invite."""
    routes = []
    for prefix, route in _all_routes(client.app.routes):
        methods = getattr(route, "methods", None) or set()
        path = prefix + getattr(route, "path", "")
        if path.startswith(("/api/", "/static")) or path in ("/setup", "/login", "/invite"):
            continue
        for method in methods & {"POST"}:
            routes.append((method, path.replace("{seed_id}", "x").replace("{job_id}", "x")
                           .replace("{preset_id}", "x")))  # fmt: skip
    return routes


# ---- supply chain and CSP ------------------------------------------------------------------


def test_vendored_htmx_matches_the_recorded_hash() -> None:
    assert hashlib.sha256(HTMX.read_bytes()).hexdigest() == HTMX_SHA256
    assert HTMX_SHA256 in (HTMX.parent / "VENDOR.md").read_text()


def test_templates_have_no_inline_script_style_or_handlers() -> None:
    """The CSP (default-src 'self') would block them; keep it that way."""
    for template in (PKG / "templates").glob("*.html"):
        text = template.read_text()
        assert not re.search(r"<script(?![^>]*\bsrc=)", text), template.name
        assert "<style" not in text and " style=" not in text, template.name
        assert not re.search(r"\son[a-z]+=", text), template.name  # onclick= etc.
        assert "hx-on" not in text, template.name  # htmx's eval-based handlers


def test_pages_carry_the_strict_csp(signed_in) -> None:
    page = signed_in.get("/")
    assert page.status_code == 200
    assert page.headers["content-security-policy"].startswith("default-src 'self'")
    config = re.search(r"name=\"htmx-config\" content='([^']+)'", page.text).group(1)
    assert json.loads(config) | {} == json.loads(config)
    assert json.loads(config)["allowEval"] is False
    assert json.loads(config)["selfRequestsOnly"] is True


# ---- routing and protection ---------------------------------------------------------------


def test_before_setup_everything_leads_to_setup(fresh) -> None:
    for path in ("/", "/login", "/presets"):
        assert fresh.get(path).headers["location"] == "/setup", path
    assert fresh.get("/setup").status_code == 200


def test_after_setup_pages_need_sign_in(signed_in) -> None:
    signed_in.cookies.clear()
    for path in ("/", "/presets", "/seeds/00000000-0000-0000-0000-000000000000"):
        assert signed_in.get(path).headers["location"] == "/login", path
    assert signed_in.get("/setup").headers["location"] == "/login"


def test_every_changing_ui_route_needs_a_session(signed_in) -> None:
    routes = ui_routes(signed_in)
    assert routes
    signed_in.cookies.clear()
    for method, path in routes:
        response = signed_in.request(method, path, headers=H)
        assert response.headers.get("location") == "/login", (method, path)


def test_every_changing_ui_route_needs_a_same_origin_request(signed_in) -> None:
    for method, path in ui_routes(signed_in):
        assert signed_in.request(method, path).status_code == 403, (method, path)
        evil = signed_in.request(method, path, headers={"Origin": "https://evil.example"})
        assert evil.status_code == 403, (method, path)


# ---- setup and sign-in -----------------------------------------------------------------------


def test_setup_form_signs_in(fresh) -> None:
    response = form_setup(fresh)
    assert response.headers["hx-redirect"] == "/"
    assert "httponly" in response.headers["set-cookie"].lower()
    assert "Recent seeds" in fresh.get("/").text


def test_setup_form_shows_errors(fresh) -> None:
    response = form_setup(fresh, password="short")
    assert response.status_code == 200
    assert "at least 12 characters" in response.text
    assert 'role="alert"' in response.text


def test_login_form_and_generic_failure(signed_in) -> None:
    signed_in.cookies.clear()
    wrong = signed_in.post("/login", data={"username": "samus", "password": "nope" * 4}, headers=HX)
    assert "wrong username or password" in wrong.text
    ok = signed_in.post("/login", data={"username": "samus", "password": "correct horse battery"},
                        headers=HX)  # fmt: skip
    assert ok.headers["hx-redirect"] == "/"


def test_logout(signed_in) -> None:
    assert signed_in.post("/logout", headers=HX).headers["hx-redirect"] == "/login"
    assert signed_in.get("/").headers["location"] == "/login"


def test_oversized_form_is_rejected(fresh) -> None:
    response = fresh.post("/login", content=b"username=" + b"a" * 20000, headers={
        **HX, "Content-Type": "application/x-www-form-urlencoded"})  # fmt: skip
    assert response.status_code == 413


# ---- generating ------------------------------------------------------------------------------


def generate(client: TestClient, **data: str) -> str:
    form = {"source": "preset", "preset_id": "starter_preset", **data}
    response = client.post("/seeds", data=form, headers=HX)
    location = response.headers.get("hx-redirect")
    assert location and location.startswith("/seeds/"), response.text
    return location


def wait_ready(client: TestClient, page: str) -> str:
    for _ in range(500):
        status = client.get(f"{page}/status").text
        if "every 1s" not in status:
            return status
        time.sleep(0.01)
    raise AssertionError("seed never finished")


def test_starter_preset_is_the_default(signed_in) -> None:
    first_option = re.search(r'<option value="([^"]+)"', signed_in.get("/").text).group(1)
    assert first_option == "starter_preset"


def test_generate_poll_and_reveal(signed_in) -> None:
    page = generate(signed_in, seed_number="77")
    final = wait_ready(signed_in, page)
    assert "ready" in final and "Download .rdvgame" in final
    assert "Reveal spoiler" in final and "contains the spoiler" in final
    spoiler = signed_in.get(f"{page}/spoiler").text
    assert "Artaria" in spoiler and "Power Bomb Tank" in spoiler
    assert "Intro Room" in spoiler and "Missiles, Pulse Radar" in spoiler
    assert "77" in signed_in.get(page).text


def test_race_seed_hides_the_spoiler(signed_in) -> None:
    page = generate(signed_in, race="on")
    final = wait_ready(signed_in, page)
    assert "Reveal spoiler" not in final and "without a spoiler" in final
    assert "race seeds have no spoiler" in signed_in.get(f"{page}/spoiler").text


def test_non_numeric_seed_number_is_refused(signed_in) -> None:
    response = signed_in.post("/seeds", data={"source": "preset", "preset_id": "starter_preset",
                                              "seed_number": "12abc"}, headers=HX)  # fmt: skip
    assert "whole number" in response.text
    assert "hx-redirect" not in response.headers


def test_preset_names_are_escaped(signed_in) -> None:
    preset = json.dumps({"game": "dread", "name": "<script>alert(1)</script>", "configuration": {}})
    assert signed_in.post("/api/v1/presets", content=preset, headers=H).status_code == 201
    for page in ("/", "/presets"):
        text = signed_in.get(page).text
        assert "<script>alert(1)</script>" not in text
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text


# ---- purge -------------------------------------------------------------------------------------


def test_purge_previews_the_count_and_needs_confirmation(signed_in) -> None:
    for _ in range(2):
        wait_ready(signed_in, generate(signed_in))
    preview = signed_in.post("/seeds/purge/preview", data={"scope": "all"}, headers=HX)
    assert "Delete 2 seeds?" in preview.text
    unconfirmed = signed_in.post("/seeds/purge", data={"scope": "all"}, headers=HX)
    assert "confirmed" in unconfirmed.text
    assert "hx-redirect" not in unconfirmed.headers  # nothing was deleted
    done = signed_in.post("/seeds/purge", data={"scope": "all", "confirm": "yes"}, headers=HX)
    assert done.headers["hx-redirect"] == "/"
    assert "No seeds yet." in signed_in.get("/").text


def test_purge_rejects_a_bad_scope(signed_in) -> None:
    response = signed_in.post("/seeds/purge/preview", data={"scope": "older_than", "days": "0"},
                              headers=HX)  # fmt: skip
    assert "1-3650 days" in response.text
