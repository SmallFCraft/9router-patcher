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
    assert cfg["handlers"]["default"]["class"] == "logging.StreamHandler"


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
        assert app._port_busy(free_port) is True
    finally:
        probe.close()
    assert app._port_busy(free_port) is False
