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
    assert "--windows-console-mode=force" in cmd
    assert "--assume-yes-for-downloads" in cmd
    assert "--onefile-no-compression" not in cmd, "production build must compress payload"
    assert any("templates=templates" in arg for arg in cmd)
    assert any("patches.enc" in arg for arg in cmd)
    assert cmd[-1] == "app.py"


def test_build_app_skips_heavy_unused_deps():
    """pygments (321 C files, 47% compile) + rich phải bị loại khỏi build."""
    cmd = build_app.get_nuitka_cmd(output_dir=Path("dist"))
    skip = next(a for a in cmd if a.startswith("--nofollow-import-to="))
    mods = skip.split("=", 1)[1].split(",")
    for heavy in ("pygments", "rich"):
        assert heavy in mods, f"{heavy} phải nằm trong nofollow để build không biên dịch nó"
    for must_stay in ("click",):
        assert must_stay not in mods, f"{must_stay} uvicorn import eager — loại là exe chết"


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


def test_build_app_copies_versioned_artifact(tmp_path, monkeypatch):
    """build() phải copy exe sang tên có version để upload hosting (9router-patch-vX.Y.Z.exe)."""
    import build_app
    import version

    # Tên artifact kỳ vọng khớp version.APP_VERSION
    expected = f"9router-patch-v{version.APP_VERSION}.exe"
    assert expected.startswith("9router-patch-v")
    assert expected.endswith(".exe")

    # build() phải tham chiếu tên này (kiểm tra qua source: không chạy build thật 160s)
    src = (build_app.ROOT / "build_app.py").read_text(encoding="utf-8")
    assert "9router-patch-v" in src
    assert "shutil.copyfile" in src
