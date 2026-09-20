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
    cmd_str = " ".join(cmd)
    assert "--onefile" in cmd
    assert "--windows-console-mode=attach" in cmd
    assert "--assume-yes-for-downloads" in cmd
    assert any("templates=templates" in arg for arg in cmd)
    assert any("patches.enc" in arg for arg in cmd)
    assert cmd[-1] == "app.py"
