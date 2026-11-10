# SPDX-License-Identifier: GPL-3.0-or-later
"""App icons for installing the web app (PWA): any, maskable and monochrome."""

import json
import struct
from pathlib import Path

from fastapi.testclient import TestClient

from dreadovania.app import create_app
from dreadovania.config import Settings

STATIC = Path(__file__).parent.parent / "src" / "dreadovania" / "static"


def png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", path
    return struct.unpack(">II", data[16:24])


def test_the_manifest_offers_maskable_and_monochrome_icons() -> None:
    manifest = json.loads((STATIC / "manifest.webmanifest").read_text())
    purposes = {icon["purpose"] for icon in manifest["icons"]}
    assert purposes == {"any", "maskable", "monochrome"}
    for icon in manifest["icons"]:
        path = STATIC / icon["src"].removeprefix("/static/")
        assert path.is_file(), icon["src"]
        if icon["type"] == "image/png":
            width, height = (int(n) for n in icon["sizes"].split("x"))
            assert png_size(path) == (width, height), icon["src"]
    # Chrome needs a raster maskable icon of at least 512px to install one.
    assert any(i["purpose"] == "maskable" and i["sizes"] == "512x512" for i in manifest["icons"])


def test_icons_are_served_and_linked(tmp_path: Path) -> None:
    with TestClient(create_app(Settings(data_dir=tmp_path / "data"))) as client:
        for path in (
            "/static/icons/maskable-512.png",
            "/static/icons/monochrome-192.png",
            "/static/icons/apple-touch-icon.png",
            "/static/manifest.webmanifest",
        ):
            assert client.get(path).status_code == 200, path
    assert png_size(STATIC / "icons" / "apple-touch-icon.png") == (180, 180)
