"""Tests for Windows autostart registry integration."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


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
    import app_paths
    import autostart

    monkeypatch.setattr(app_paths, "is_frozen", lambda: True)
    monkeypatch.setattr(sys, "platform", "win32")
    exe = "C:\\Apps\\9router-patch.exe"
    monkeypatch.setattr(sys, "argv", [exe])

    fake_reg = {autostart.REG_NAME: f'"{exe}" --tray'}

    class FakeKey:
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def fake_open_key(root, subkey, res=0, sam=0):
        return FakeKey()

    def fake_query_value(key, name):
        if name in fake_reg:
            return (fake_reg[name], 1)
        raise FileNotFoundError()

    monkeypatch.setattr(autostart.winreg, "OpenKey", fake_open_key)
    monkeypatch.setattr(autostart.winreg, "QueryValueEx", fake_query_value)

    assert autostart.is_supported() is True
    assert autostart.is_enabled() is True


def test_autostart_set_enabled_writes_and_deletes_key(monkeypatch):
    import app_paths
    import autostart

    monkeypatch.setattr(app_paths, "is_frozen", lambda: True)
    monkeypatch.setattr(sys, "platform", "win32")
    exe = "C:\\Apps\\9router-patch.exe"
    monkeypatch.setattr(sys, "argv", [exe])

    fake_store = {}

    class FakeKey:
        def __enter__(self): return self
        def __exit__(self, *a): pass

    monkeypatch.setattr(autostart.winreg, "OpenKey", lambda *a, **k: FakeKey())
    monkeypatch.setattr(autostart.winreg, "SetValueEx",
                        lambda key, name, res, typ, val: fake_store.__setitem__(name, val))
    monkeypatch.setattr(autostart.winreg, "DeleteValue",
                        lambda key, name: fake_store.pop(name, None))

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
