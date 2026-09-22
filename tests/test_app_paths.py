"""Tests for app_paths resolution across dev and frozen environments."""
import sys
from pathlib import Path
import pytest

import app_paths


def test_is_frozen_false_in_normal_pytest():
    """Pytest runs under plain python interpreter, not compiled C binary."""
    assert not app_paths.is_frozen()


def test_dev_paths_resolve_inside_repo(monkeypatch):
    """In dev mode, logs and backups resolve relative to repo tree."""
    monkeypatch.setattr(app_paths, "is_frozen", lambda: False)
    bundle = app_paths.get_bundle_dir()
    assert (bundle / "main.py").is_file()
    assert app_paths.get_log_dir() == bundle / "logs"
    assert app_paths.get_backup_root() == bundle.parent / "9router-backups"
    assert app_paths.get_history_file() == bundle / "logs" / "update-history.jsonl"
    assert app_paths.get_stack_state_file() == bundle / "logs" / "router-stack.json"


def test_frozen_paths_resolve_to_appdata_and_exe_dir(tmp_path, monkeypatch):
    """In frozen mode, writables go to APPDATA and backups sit alongside the EXE."""
    monkeypatch.setattr(app_paths, "is_frozen", lambda: True)
    fake_appdata = tmp_path / "AppData"
    monkeypatch.setenv("APPDATA", str(fake_appdata))

    fake_exe = tmp_path / "bin" / "9router-patch.exe"
    fake_exe.parent.mkdir(parents=True)
    fake_exe.touch()
    monkeypatch.setattr(sys, "argv", [str(fake_exe)])

    app_data = app_paths.get_app_data_dir()
    assert app_data == fake_appdata / "9router-patch"
    assert app_data.is_dir()

    log_dir = app_paths.get_log_dir()
    assert log_dir == app_data / "logs"
    assert log_dir.is_dir()

    assert app_paths.get_history_file() == log_dir / "update-history.jsonl"
    assert app_paths.get_stack_state_file() == log_dir / "router-stack.json"
    assert app_paths.get_backup_root() == tmp_path / "9router-backups"


def test_headroom_cmd_uses_pythonw_not_console_shim(monkeypatch, tmp_path):
    """Regression 2026-09-21: the headroom.exe pip shim is CONSOLE-subsystem, so spawning it
    detached makes its inner python.exe allocate a Windows Terminal window; closing that window
    kills headroom. The command must go through pythonw.exe -m headroom.cli instead."""
    import updater
    fake_pyw = tmp_path / "pythonw.exe"
    fake_pyw.write_text("", encoding="utf-8")
    monkeypatch.setattr(updater.sys, "executable", str(tmp_path / "python.exe"))
    monkeypatch.setattr(updater, "_pythonw_has_headroom", lambda p: True)
    cmd = updater._default_headroom_cmd()
    assert cmd is not None
    assert "pythonw.exe" in cmd
    assert "-m headroom.cli" in cmd
    assert "headroom.exe" not in cmd, "console shim re-introduces the terminal window bug"
    assert f"--port {updater.HEADROOM_PORT}" in cmd


def test_headroom_cmd_falls_back_to_interpreter_dir(monkeypatch, tmp_path):
    """Frozen onefile: sys.executable is the temp python, PATH lookup can miss; the
    interpreter-sibling pythonw.exe must still be found."""
    import updater
    fake_pyw = tmp_path / "pythonw.exe"
    fake_pyw.write_text("", encoding="utf-8")
    monkeypatch.setattr(updater.sys, "executable", str(tmp_path / "python.exe"))
    monkeypatch.setattr(updater, "_pythonw_has_headroom", lambda p: True)
    cmd = updater._default_headroom_cmd()
    assert cmd is not None and str(fake_pyw) in cmd


def test_headroom_cmd_returns_none_without_pythonw(monkeypatch, tmp_path):
    """No pythonw anywhere: report failure rather than falling back to the window-spawning shim."""
    import updater
    monkeypatch.setattr(updater.sys, "executable", str(tmp_path / "python.exe"))
    assert updater._default_headroom_cmd() is None


def test_headroom_cmd_returns_none_when_headroom_not_installed(monkeypatch, tmp_path):
    """pythonw.exe có thật nhưng chưa cài package headroom -> trả None, không đẻ log rác."""
    import updater
    fake_pyw = tmp_path / "pythonw.exe"
    fake_pyw.write_text("", encoding="utf-8")
    monkeypatch.setattr(updater.sys, "executable", str(tmp_path / "python.exe"))
    monkeypatch.setattr(updater, "_pythonw_has_headroom", lambda p: False)
    assert updater._default_headroom_cmd() is None
    updater._HEADROOM_STATUS = None
    st = updater.headroom_status(refresh=True)
    assert st["installed"] is False
    assert "Chưa cài headroom" in st["reason"]

