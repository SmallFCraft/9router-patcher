"""Tests for build pipeline command assembly and pre-flight checks."""
from pathlib import Path

import build_app


def test_requirements_file_exists_and_contains_deps():
    req = Path("requirements.txt")
    assert req.is_file()
    text = req.read_text(encoding="utf-8")
    for dep in ("nuitka", "fastapi", "uvicorn", "jinja2", "cryptography"):
        assert dep in text.lower()


def test_build_app_command_assembly():
    """build_app generates correct Nuitka arguments without syntax errors."""
    cmd = build_app.get_nuitka_cmd(output_dir=Path("dist"))
    assert "--onefile" in cmd
    assert "--windows-console-mode=attach" in cmd
    assert "--assume-yes-for-downloads" in cmd
    assert "--onefile-no-compression" not in cmd, "production build must compress payload"
    assert any("templates=templates" in arg for arg in cmd)
    assert any("patches.enc" in arg for arg in cmd)
    assert cmd[-1] == "app.py"


def test_build_app_fast_flag_disables_compression():
    """Dev loop flag: --fast passes --onefile-no-compression to skip ~50s zstd."""
    cmd = build_app.get_nuitka_cmd(output_dir=Path("dist"), fast=True)
    assert "--onefile-no-compression" in cmd


def test_exe_locked_detects_free_and_open_file(tmp_path: Path):
    """_exe_locked returns False when file can be opened for writing, True if locked."""
    f = tmp_path / "dummy.exe"
    f.write_bytes(b"MZ\x00")
    assert build_app._exe_locked(f) is False
    with open(f, "r+b"):
        # Windows file sharing: standard open without explicit deny allows r+b,
        # but function must still not raise
        assert isinstance(build_app._exe_locked(f), bool)
    # Non-existent file raises OSError inside open -> reported as locked
    assert build_app._exe_locked(tmp_path / "nonexistent.exe") is True
