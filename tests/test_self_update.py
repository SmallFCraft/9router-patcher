"""Tests for self-update engine and version comparison."""
from __future__ import annotations

import pytest


def test_parse_version_normalizes_and_pads():
    import self_update
    assert self_update.parse_version("2.0.1") == (2, 0, 1, 0)
    assert self_update.parse_version("2.0") == (2, 0, 0, 0)
    assert self_update.parse_version("v2.0.1.4") == (2, 0, 1, 4)
    assert self_update.parse_version("invalid") == (0, 0, 0, 0)


def test_is_newer_compares_semver():
    import self_update
    assert self_update.is_newer("2.0.1", "2.0.0") is True
    assert self_update.is_newer("2.1.0", "2.0.9") is True
    assert self_update.is_newer("2.0.0", "2.0.0") is False
    assert self_update.is_newer("1.9.9", "2.0.0") is False
    assert self_update.is_newer("junk", "2.0.0") is False


def test_settings_persistence(tmp_path, monkeypatch):
    """auto_update switch lưu vào settings.json, mặc định là True."""
    import self_update
    import app_paths

    fake_data_dir = tmp_path / "app_data"
    fake_data_dir.mkdir()
    monkeypatch.setattr(app_paths, "get_app_data_dir", lambda: fake_data_dir)

    # Mặc định chưa có file -> True
    assert self_update.is_enabled() is True

    # Tắt đi -> ghi file -> đọc lại False
    self_update.set_enabled(False)
    assert self_update.is_enabled() is False

    # Bật lại -> đọc lại True
    self_update.set_enabled(True)
    assert self_update.is_enabled() is True


def test_check_update_parses_json_and_detects_newer_version(monkeypatch):
    """check_update đọc đúng version.json và bật has_update nếu bản remote mới hơn."""
    import self_update

    sample_json = b'{"version": "2.1.0", "url": "https://example.com/files/app.exe", "sha256": "abcdef", "changelog": "test changelog"}'

    class DummyResponse:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return sample_json

    monkeypatch.setattr(self_update.urllib.request, "urlopen", lambda req, timeout=5: DummyResponse())

    res = self_update.check_update()
    assert res is not None
    assert res["version"] == "2.1.0"
    assert res["has_update"] is True
    assert res["url"] == "https://example.com/files/app.exe"
    assert res["sha256"] == "abcdef"
    assert res["changelog"] == "test changelog"


def test_check_update_handles_network_error_gracefully(monkeypatch):
    """Khi máy chủ lỗi mạng hoặc timeout: trả về None, không crash."""
    import self_update
    import urllib.error

    def fail(*a, **k):
        raise urllib.error.URLError("server down")

    monkeypatch.setattr(self_update.urllib.request, "urlopen", fail)
    assert self_update.check_update() is None

