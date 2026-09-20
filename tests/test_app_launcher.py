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


def test_app_launcher_imports_cleanly():
    """app.py imports app, HOST, PORT without side effects."""
    import app
    assert app.HOST == "127.0.0.1"
    assert app.PORT == 20129
    assert app.app is main.app
