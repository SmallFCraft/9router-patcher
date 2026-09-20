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


def test_headroom_cmd_locates_shim_on_path(monkeypatch):
    """_default_headroom_cmd finds headroom shim from PATH when sys.executable is temp python."""
    import updater
    monkeypatch.setattr(updater.shutil, "which",
                        lambda cmd: r"C:\Scripts\headroom.exe" if "headroom" in cmd else None)
    cmd = updater._default_headroom_cmd()
    assert cmd is not None
    assert "headroom.exe" in cmd
