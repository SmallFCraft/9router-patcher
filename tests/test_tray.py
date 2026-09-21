"""Unit tests for tray.py: pure-logic paths, no Win32 calls."""
import sys

import pytest

import tray


@pytest.mark.skipif(sys.platform == "win32",
                    reason="these are the non-Windows no-op paths")
def test_hide_console_noop_off_windows():
    assert tray.console_hwnd() == 0
    assert tray.hide_console() is False
    assert tray.show_console() is False


@pytest.mark.skipif(sys.platform == "win32",
                    reason="these are the non-Windows no-op paths")
def test_available_false_without_console():
    assert tray.available() is False


def test_dispatch_calls_callback_and_returns_true():
    called = []
    cbs = {1: lambda: called.append("a"), 2: lambda: called.append("b")}
    assert tray.dispatch(cbs, 2) is True
    assert called == ["b"]


def test_dispatch_unknown_id_is_noop():
    assert tray.dispatch({1: lambda: None}, 99) is False


def test_on_menu_delegates_to_callbacks():
    seen = []
    icon = tray.TrayIcon("9router Patcher Manager")
    icon.callbacks = {tray.ID_SHOW: lambda: seen.append("show")}
    icon.on_menu(tray.ID_SHOW)
    assert seen == ["show"]
    icon.on_menu(999)          # unknown id: no crash, no call
    assert seen == ["show"]


def test_tray_icon_start_noop_off_windows():
    if sys.platform != "win32":
        assert tray.TrayIcon("x").start() is False


def test_log_line_kind_unchanged_contract():
    """Nhắc: ERROR đỏ, WARN vàng, INFO trung tính — template logs.html phụ thuộc."""
    from main import log_line_kind
    assert log_line_kind("ERROR: x") == "err"
    assert log_line_kind("WARN: x") == "warn"
    assert log_line_kind("INFO: ok") == ""
