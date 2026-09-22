"""Tests for self-update engine and version comparison."""
from __future__ import annotations

import time

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

    sample_json = b'{"version": "9.9.9", "url": "https://example.com/files/app.exe", "sha256": "abcdef", "changelog": "test changelog"}'

    class DummyResponse:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return sample_json

    monkeypatch.setattr(self_update.urllib.request, "urlopen", lambda req, timeout=5: DummyResponse())

    res = self_update.check_update()
    assert res is not None
    assert res["version"] == "9.9.9"
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


def _dummy_dl_monkeypatch(monkeypatch, self_update, content: bytes, chunk: int = 65536):
    """Giả lập urlopen trả về nội dung tải theo từng chunk."""
    class DummyDlResponse:
        def __init__(self): self._buf = content
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self, size=chunk):
            out, self._buf = self._buf[:size], self._buf[size:]
            return out

    monkeypatch.setattr(self_update.urllib.request, "urlopen",
                        lambda req, timeout=30: DummyDlResponse())


def test_download_and_swap_success_flow(tmp_path, monkeypatch):
    """Tải thành công, SHA256 khớp: exe được thay, file cũ đổi tên .old-<ts>."""
    import self_update
    import hashlib

    exe_file = tmp_path / "9router-patch.exe"
    exe_file.write_bytes(b"OLD_VERSION_EXE")

    new_content = b"NEW_VERSION_EXE_DATA_PAYLOAD"
    _dummy_dl_monkeypatch(monkeypatch, self_update, new_content)
    monkeypatch.setattr(self_update.app_paths, "is_frozen", lambda: True)

    meta = {
        "version": "2.0.1",
        "url": "https://example.com/files/app.exe",
        "sha256": hashlib.sha256(new_content).hexdigest(),
    }

    res = self_update.download_and_swap(meta, current_exe=exe_file)
    assert res["ok"] is True, res
    assert exe_file.read_bytes() == new_content

    old_files = list(tmp_path.glob("9router-patch.old-*"))
    assert len(old_files) == 1
    assert old_files[0].read_bytes() == b"OLD_VERSION_EXE"
    assert self_update.state()["phase"] == "ready"
    assert self_update.state()["applied_version"] == "2.0.1"

    cleaned = self_update.cleanup_old_files(exe_dir=tmp_path)
    assert cleaned == 1
    assert len(list(tmp_path.glob("9router-patch.old-*"))) == 0


def test_download_and_swap_rejects_sha256_mismatch(tmp_path, monkeypatch):
    """SHA256 lệch: xóa file tạm, exe gốc nguyên vẹn, không hoán đổi."""
    import self_update

    exe_file = tmp_path / "9router-patch.exe"
    exe_file.write_bytes(b"ORIGINAL_EXE")

    _dummy_dl_monkeypatch(monkeypatch, self_update, b"CORRUPTED_EXE")
    monkeypatch.setattr(self_update.app_paths, "is_frozen", lambda: True)

    meta = {
        "version": "2.0.1",
        "url": "https://example.com/files/app.exe",
        "sha256": "0" * 64,
    }

    res = self_update.download_and_swap(meta, current_exe=exe_file)
    assert res["ok"] is False
    assert "SHA256" in res["error"]
    assert exe_file.read_bytes() == b"ORIGINAL_EXE"
    assert not (tmp_path / "9router-patch.new").exists()


def test_download_and_swap_dev_mode_never_touches_files(tmp_path, monkeypatch):
    """Chạy từ source (không frozen): bỏ qua, không tải, không hoán đổi."""
    import self_update

    monkeypatch.setattr(self_update.app_paths, "is_frozen", lambda: False)
    res = self_update.download_and_swap({"version": "2.0.1", "url": "https://x/y.exe"})
    assert res["ok"] is False
    assert res["error"] == "dev-mode"
    assert list(tmp_path.glob("*")) == []


def test_worker_skips_when_disabled(monkeypatch):
    """Khi auto_update=False: worker không gọi check_update."""
    import self_update
    import threading

    stop = threading.Event()
    called = []

    def fake_enabled():
        stop.set()      # cho worker chạy đúng 1 lượt rồi thoát
        return False

    monkeypatch.setattr(self_update, "is_enabled", fake_enabled)
    monkeypatch.setattr(self_update, "check_update",
                        lambda url=None: called.append(True) or None)

    self_update.run_worker(stop, interval=60.0)
    assert len(called) == 0


def test_worker_downloads_when_newer_found(monkeypatch):
    """Khi có bản mới và auto_update=True: worker gọi download_and_swap."""
    import self_update
    import threading

    stop = threading.Event()
    meta = {"version": "2.1.0", "has_update": True,
            "url": "https://example.com/files/app.exe", "sha256": ""}
    swapped = []
    monkeypatch.setattr(self_update, "is_enabled", lambda: True)
    monkeypatch.setattr(self_update, "check_update", lambda url=None: meta)

    def fake_swap(m):
        swapped.append(m)
        stop.set()      # thoát sau lượt đầu
        return {"ok": True, "error": None}

    monkeypatch.setattr(self_update, "download_and_swap", fake_swap)

    self_update.run_worker(stop, interval=60.0)
    assert len(swapped) == 1
    assert swapped[0]["version"] == "2.1.0"


def test_worker_checks_immediately_at_boot_not_after_interval(monkeypatch):
    """Lượt kiểm tra đầu chạy ngay lúc khởi động, không chờ hết 5 phút.

    interval đặt 3600s — nếu worker ngủ trước rồi mới check thì test treo/timeout;
    ở đây check_update phải được gọi trước lần wait đầu tiên.
    """
    import self_update
    import threading

    stop = threading.Event()
    checks = []

    def fake_check(url=None):
        checks.append(time.time())
        stop.set()                  # thoát ngay sau lượt đầu tiên
        return None

    monkeypatch.setattr(self_update, "is_enabled", lambda: True)
    monkeypatch.setattr(self_update, "check_update", fake_check)

    t0 = time.time()
    self_update.run_worker(stop, interval=3600.0)
    elapsed = time.time() - t0

    assert len(checks) == 1
    assert elapsed < 5.0, f"worker chờ {elapsed:.1f}s trước lượt check đầu"





def test_restart_self_spawns_new_exe_and_exits(monkeypatch):
    """restart_self: spawn exe mới detached rồi thoát (SystemExit)."""
    import self_update

    spawned = []
    monkeypatch.setattr(self_update.app_paths, "is_frozen", lambda: True)

    class FakePopen:
        def __init__(self, argv, **kwargs):
            spawned.append((argv, kwargs))

    monkeypatch.setattr(self_update.subprocess, "Popen", FakePopen)

    with pytest.raises(SystemExit) as exc:
        self_update.restart_self()
    assert exc.value.code == 0
    assert len(spawned) == 1


def test_restart_self_noop_in_dev_mode(monkeypatch):
    """Chạy từ source .py: restart_self không làm gì, không thoát."""
    import self_update

    monkeypatch.setattr(self_update.app_paths, "is_frozen", lambda: False)
    self_update.restart_self()  # không raise
