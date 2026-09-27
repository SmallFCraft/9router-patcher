"""Tests for Windows autostart via Startup Folder + VBScript hidden launcher."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def _frozen_env(monkeypatch, tmp_path, exe=None, create_exe=True):
    """Môi trường frozen giả: exe THẬT nằm trong tmp_path (set_enabled chốt
    exe.is_file()), Startup folder trỏ về tmp_path."""
    import app_paths
    import autostart
    if exe is None:
        exe = str(tmp_path / "9router-patch.exe")
    p = Path(exe)
    if create_exe and not p.is_file():
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"MZ")
        except OSError:
            pass            # path không ghi được (vd ổ đĩa lạ): test guard tự xử
    monkeypatch.setattr(app_paths, "is_frozen", lambda: True)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "argv", [exe])
    monkeypatch.setattr(autostart, "_startup_dir", lambda: tmp_path)

    deleted: list[str] = []

    class _Key:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    fake_reg = SimpleNamespace(
        HKEY_CURRENT_USER="HKCU",
        KEY_SET_VALUE=2,
        OpenKey=lambda *a, **k: _Key(),
        DeleteValue=lambda key, name: deleted.append(name),
    )
    monkeypatch.setattr(autostart, "winreg", fake_reg)
    autostart._ENABLED = None
    autostart._MIGRATED = False
    return fake_reg, deleted


def test_autostart_unsupported_when_not_frozen(monkeypatch):
    import app_paths
    import autostart
    monkeypatch.setattr(app_paths, "is_frozen", lambda: False)
    assert autostart.is_supported() is False
    assert autostart.is_enabled() is False
    ok, err = autostart.set_enabled(True)
    assert ok is False
    assert "exe" in (err or "").lower()


def test_autostart_enable_writes_vbs_into_startup(monkeypatch, tmp_path):
    import autostart

    exe = "C:\\Apps\\9router-patch.exe"
    _frozen_env(monkeypatch, tmp_path, exe)

    ok, err = autostart.set_enabled(True)
    assert ok is True
    assert err is None

    vbs = tmp_path / autostart.VBS_NAME
    assert vbs.is_file()
    body = vbs.read_text(encoding="utf-8")
    assert exe in body
    assert "--tray" in body
    assert "WshShell.Run" in body
    assert ", 0, False" in body, "style 0 = SW_HIDE, no-wait"
    assert autostart._ENABLED is True


def test_autostart_disable_removes_vbs(monkeypatch, tmp_path):
    import autostart

    _frozen_env(monkeypatch, tmp_path)
    autostart.set_enabled(True)
    vbs = tmp_path / autostart.VBS_NAME
    assert vbs.is_file()

    ok, err = autostart.set_enabled(False)
    assert ok is True
    assert err is None
    assert not vbs.is_file()
    assert autostart._ENABLED is False

    # Tắt khi chưa từng bật: vẫn thành công, không ném lỗi.
    ok, err = autostart.set_enabled(False)
    assert ok is True


def test_autostart_is_enabled_true_after_enable(monkeypatch, tmp_path):
    import autostart

    _frozen_env(monkeypatch, tmp_path)
    autostart.set_enabled(True)
    autostart._ENABLED = None
    assert autostart.is_enabled() is True


def test_autostart_is_enabled_false_when_no_vbs(monkeypatch, tmp_path):
    import autostart

    _frozen_env(monkeypatch, tmp_path)
    assert autostart.is_enabled() is False


def test_autostart_is_enabled_rejects_vbs_pointing_at_other_exe(monkeypatch, tmp_path):
    """VBS cũ trỏ sang exe khác (bản cũ / đã di chuyển) phải tính là TẮT."""
    import autostart

    _frozen_env(monkeypatch, tmp_path, exe="C:\\Apps\\9router-patch.exe")
    (tmp_path / autostart.VBS_NAME).write_text(
        'WshShell.Run """C:\\Old\\9router-patch.exe"" --tray", 0, False\r\n',
        encoding="utf-8")
    assert autostart.is_enabled() is False


def test_autostart_is_enabled_rejects_vbs_without_tray_flag(monkeypatch, tmp_path):
    import autostart

    exe = "C:\\Apps\\9router-patch.exe"
    _frozen_env(monkeypatch, tmp_path, exe)
    (tmp_path / autostart.VBS_NAME).write_text(
        f'WshShell.Run """{exe}""\r\n', encoding="utf-8")
    assert autostart.is_enabled() is False


def test_autostart_reports_write_error(monkeypatch, tmp_path):
    import autostart

    _frozen_env(monkeypatch, tmp_path)

    def boom(self, *a, **k):
        raise OSError("Access is denied")

    monkeypatch.setattr(type(tmp_path), "write_text", boom, raising=False)
    ok, err = autostart.set_enabled(True)
    assert ok is False
    assert "Startup" in (err or "")


def test_autostart_migrates_legacy_run_key(monkeypatch, tmp_path):
    """Run key cũ của bản <= 2.2.12 phải bị dọn để không spawn instance thứ hai."""
    import autostart

    _, deleted = _frozen_env(monkeypatch, tmp_path)
    autostart.is_enabled()
    assert autostart.REG_NAME in deleted


def test_migrate_legacy_runs_only_once(monkeypatch, tmp_path):
    import autostart

    _, deleted = _frozen_env(monkeypatch, tmp_path)
    autostart.is_enabled()
    autostart.is_enabled()
    autostart.set_enabled(True)
    assert deleted.count(autostart.REG_NAME) == 1


def test_autostart_refuses_to_write_vbs_for_nonexistent_exe(monkeypatch, tmp_path):
    """Hồi quy 2026-09-27: argv[0] bất thường (vd '-c' lúc chạy python -c) khiến
    get_current_exe() trả path rác; ghi VBS trỏ file không tồn tại thì logon báo
    'The system cannot find the file specified' (80070002)."""
    import autostart

    _frozen_env(monkeypatch, tmp_path, exe=str(tmp_path / "does-not-exist.exe"),
                create_exe=False)
    ok, err = autostart.set_enabled(True)
    assert ok is False
    assert "exe" in (err or "").lower()
    assert not (tmp_path / autostart.VBS_NAME).is_file()


def test_autostart_cleans_stale_vbs_from_previous_name(monkeypatch, tmp_path):
    """Launcher tên cũ (9router-patch-autostart.vbs) phải bị xoá — còn cả hai file
    trong Startup thì logon mở app hai lần."""
    import autostart

    _frozen_env(monkeypatch, tmp_path)
    stale = tmp_path / "9router-patch-autostart.vbs"
    stale.write_text("' old\r\n", encoding="utf-8")
    assert stale.is_file()

    autostart.set_enabled(True)

    assert not stale.is_file()
    assert (tmp_path / autostart.VBS_NAME).is_file()
