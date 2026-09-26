"""Tests for main.py log config export and app launcher behavior."""
import pytest

import main


def test_get_log_config_returns_valid_uvicorn_dict():
    """get_log_config() returns expected dict structure for uvicorn."""
    cfg = main.get_log_config()
    assert isinstance(cfg, dict)
    assert cfg["version"] == 1
    assert "uvicorn" in cfg["loggers"]
    assert "formatters" in cfg
    assert "handlers" in cfg
    assert cfg["handlers"]["default"]["class"] == "logging.FileHandler"


def test_log_config_survives_consoleless_attach_mode(monkeypatch):
    """Exe chạy attach-mode: sys.stdout is None -> dictConfig + emit không được crash.

    Regression: uvicorn DefaultFormatter gọi sys.stdout.isatty() lúc khởi tạo,
    máy bạn báo 'Unable to configure formatter default' 2026-09-21.
    """
    import logging
    import logging.config
    import sys
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    logging.config.dictConfig(main.get_log_config())   # phải không raise
    logging.getLogger("uvicorn").info("consoleless probe")
    logging.shutdown()


def test_app_launcher_imports_cleanly():
    """app.py imports app, HOST, PORT without side effects."""
    import app
    assert app.HOST == "127.0.0.1"
    assert app.PORT == 20129
    assert app.app is main.app


def test_port_busy_detects_bound_port():
    """_port_busy returns True if port has listener, False if free."""
    import socket
    import app
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))   # ephemeral port ngẫu nhiên, không đụng port thật
    probe.listen(1)
    try:
        free_port = probe.getsockname()[1]
        assert app._port_busy("127.0.0.1", free_port) is True
    finally:
        probe.close()
    assert app._port_busy("127.0.0.1", free_port) is False


def test_parse_args_supports_tray():
    import app
    args = app._parse_args(["--tray"])
    assert args.tray is True
    args_default = app._parse_args([])
    assert args_default.tray is False


def test_main_tray_stays_silent_when_port_busy(monkeypatch):
    """Hồi quy 2026-09-26: lần bật thứ hai từ HKCU Run (app đã chạy trong khay)
    không được mở browser — chính thứ cờ --tray sinh ra để tránh."""
    import app
    monkeypatch.setattr(app, "_port_busy", lambda host, port: True)
    opened = []
    monkeypatch.setattr(app.webbrowser, "open", lambda url: opened.append(url))

    app.main(["--tray"])
    assert opened == []

    app.main([])
    assert len(opened) == 1


def test_main_tray_skips_browser_and_hides_console_on_free_port(monkeypatch):
    """--tray khi port rảnh: ẩn console NGAY TRƯỚC doctor, bỏ qua browser-launcher."""
    import app
    monkeypatch.setattr(app, "_port_busy", lambda host, port: False)
    hidden = []
    doctor_called = []
    monkeypatch.setattr(app, "_hide_console", lambda: hidden.append("hidden"))
    def fake_doctor(**kwargs):
        doctor_called.append(len(hidden))
        return False
    monkeypatch.setattr(app.boot_doctor, "run_doctor", fake_doctor)
    opened = []
    monkeypatch.setattr(app.webbrowser, "open", lambda url: opened.append(url))

    app.main(["--tray"])
    assert opened == []
    # _hide_console phải được gọi TRƯỚC KHI run_doctor chạy (để che suốt thời gian boot)
    assert doctor_called == [1]
