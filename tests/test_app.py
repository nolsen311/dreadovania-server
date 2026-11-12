from fastapi.testclient import TestClient

from dreadovania import __version__
from dreadovania.app import SECURITY_HEADERS, create_app

client = TestClient(create_app())


def test_healthz() -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_info_reports_version() -> None:
    response = client.get("/api/v1/info")
    assert response.status_code == 200
    assert response.json() == {"name": "dreadovania", "version": __version__}


def test_security_headers_on_every_response() -> None:
    for path in ("/healthz", "/api/v1/info", "/does-not-exist"):
        response = client.get(path)
        for name, value in SECURITY_HEADERS.items():
            assert response.headers[name] == value, (path, name)


def test_referrer_policy_keeps_the_origin_on_form_posts() -> None:
    # "no-referrer" makes browsers send "Origin: null" on a plain form POST, which
    # the CSRF check refuses (saving cosmetics did).
    assert SECURITY_HEADERS["Referrer-Policy"] == "same-origin"


def test_openapi_served_but_docs_ui_disabled() -> None:
    assert client.get("/api/v1/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
