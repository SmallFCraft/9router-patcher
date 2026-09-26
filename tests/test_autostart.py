"""Tests for Windows autostart registry integration."""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest


class _FakeKey:
    def __enter__(self): return self
    def __exit__(self, *a): pass


def _fake_winreg(monkeypatch, autostart):
    """winreg giả cho mọi platform: autostart.winreg là None trên Linux/macOS nên
    không monkeypatch trực tiếp lên nó được — thay cả module attribute."""
    fake = SimpleNamespace(
        HKEY_CURRENT_USER="HKCU",
        KEY_READ=1,
        KEY_SET_VALUE=2,
        REG_SZ=1,
    )
    fake.OpenKey = lambda *a, **k: _FakeKey()
    fake.QueryValueEx = lambda key, name: (_ for _ in ()).throw(FileNotFoundError())
    fake.SetValueEx = lambda *a, **k: None
    fake.DeleteValue = lambda *a, **k: None
    monkeypatch.setattr(autostart, "winreg", fake)
    return fake


def _frozen_env(monkeypatch, exe="C:\\Apps\\9router-patch.exe"):
    import app_paths
    import autostart
    monkeypatch.setattr(app_paths, "is_frozen", lambda: True)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "argv", [exe])
    return _fake_winreg(monkeypatch, autostart)


def test_autostart_unsupported_when_not_frozen(monkeypatch):
    import app_paths
    import autostart
    monkeypatch.setattr(app_paths, "is_frozen", lambda: False)
    assert autostart.is_supported() is False
    assert autostart.is_enabled() is False
    ok, err = autostart.set_enabled(True)
    assert ok is False
    assert "exe" in (err or "").lower()


def test_autostart_is_enabled_true_when_value_matches(monkeypatch):
    import autostart

    exe = "C:\\Apps\\9router-patch.exe"
    fake = _frozen_env(monkeypatch, exe)
    fake.QueryValueEx = lambda key, name: (f'"{exe}" --tray', 1) if name == autostart.REG_NAME else (_ for _ in ()).throw(FileNotFoundError())

    assert autostart.is_supported() is True
    assert autostart.is_enabled() is True


def test_autostart_is_enabled_rejects_stale_entry_without_flag(monkeypatch):
    """Hồi quy: entry cũ trỏ binary lạ / thiếu --tray không được đọc thành ON."""
    import autostart

    fake = _frozen_env(monkeypatch)
    fake.QueryValueEx = lambda key, name: ('"C:\\Old\\9router-patch-old.exe"', 1) if name == autostart.REG_NAME else (_ for _ in ()).throw(FileNotFoundError())
    assert autostart.is_enabled() is False


def test_autostart_set_enabled_writes_and_deletes_key(monkeypatch):
    import autostart

    exe = "C:\\Apps\\9router-patch.exe"
    fake = _frozen_env(monkeypatch, exe)

    fake_store = {}
    fake.SetValueEx = lambda key, name, res, typ, val: fake_store.__setitem__(name, val)
    fake.DeleteValue = lambda key, name: fake_store.pop(name, None)

    # Enable
    ok, err = autostart.set_enabled(True)
    assert ok is True
    assert err is None
    assert autostart.REG_NAME in fake_store
    assert f'"{exe}" --tray' == fake_store[autostart.REG_NAME]

    # Disable
    ok, err = autostart.set_enabled(False)
    assert ok is True
    assert err is None
    assert autostart.REG_NAME not in fake_store


def test_autostart_set_enabled_reports_registry_error(monkeypatch):
    """Registry lỗi (quyền/hỏng key): ok=False kèm thông báo, không ném exception."""
    import autostart

    fake = _frozen_env(monkeypatch)
    fake.SetValueEx = lambda *a, **k: (_ for _ in ()).throw(OSError("Access denied"))

    ok, err = autostart.set_enabled(True)
    assert ok is False
    assert "Registry" in (err or "")

    fake.OpenKey = lambda *a, **k: (_ for _ in ()).throw(OSError("key missing"))
    ok, err = autostart.set_enabled(False)
    assert ok is False
    assert "Registry" in (err or "")
