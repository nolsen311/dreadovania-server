import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "content_guard", Path(__file__).resolve().parent.parent / "scripts" / "content_guard.py"
)
assert _SPEC and _SPEC.loader
guard = importlib.util.module_from_spec(_SPEC)
sys.modules["content_guard"] = guard
_SPEC.loader.exec_module(guard)


def _write(root: Path, rel: str, data: bytes = b"hello") -> str:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return rel


def test_clean_files_pass(tmp_path: Path) -> None:
    files = [_write(tmp_path, "src/app.py", b"print('hi')"), _write(tmp_path, "README.md")]
    assert guard.scan(tmp_path, files) == []


@pytest.mark.parametrize(
    "rel",
    ["game.nsp", "dumps/Game.XCI", "x.nca", "packs/system.pkg", "a/b/thing.bmsad"],
)
def test_blocked_suffixes(tmp_path: Path, rel: str) -> None:
    assert guard.scan(tmp_path, [_write(tmp_path, rel)])


@pytest.mark.parametrize("rel", ["prod.keys", "config/title.keys", "my.keys"])
def test_key_files(tmp_path: Path, rel: str) -> None:
    assert guard.scan(tmp_path, [_write(tmp_path, rel)])


@pytest.mark.parametrize("rel", ["romfs/packs/a.txt", "data/ExeFS/main.txt"])
def test_extracted_filesystem_dirs(tmp_path: Path, rel: str) -> None:
    assert guard.scan(tmp_path, [_write(tmp_path, rel)])


def test_renamed_nsp_caught_by_magic(tmp_path: Path) -> None:
    rel = _write(tmp_path, "innocent.bin", b"PFS0" + b"\0" * 64)
    reasons = [f.reason for f in guard.scan(tmp_path, [rel])]
    assert any("PFS0" in r for r in reasons)


def test_renamed_xci_caught_by_magic(tmp_path: Path) -> None:
    data = b"\0" * guard.XCI_MAGIC_OFFSET + guard.XCI_MAGIC + b"\0" * 16
    rel = _write(tmp_path, "notes.dat", data)
    reasons = [f.reason for f in guard.scan(tmp_path, [rel])]
    assert any("XCI" in r for r in reasons)


def test_large_files_blocked_unless_allowlisted(tmp_path: Path) -> None:
    big = b"a" * (guard.MAX_FILE_BYTES + 1)
    assert guard.scan(tmp_path, [_write(tmp_path, "big.txt", big)])
    assert guard.scan(tmp_path, [_write(tmp_path, "uv.lock", big)]) == []


def test_main_on_git_repo(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    _write(tmp_path, "ok.txt")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    assert guard.main(["content_guard", str(tmp_path)]) == 0

    _write(tmp_path, "prod.keys")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    assert guard.main(["content_guard", str(tmp_path)]) == 1
    assert "prod.keys" in capsys.readouterr().out
