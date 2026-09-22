# Self-Update Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cho phép file exe `9router-patch.exe` tự động kiểm tra, tải bản cập nhật mới từ hosting cá nhân và hoán đổi file nguyên tử khi chạy nền hoặc theo yêu cầu người dùng trên Web Dashboard.

**Architecture:** Modun hóa toàn bộ logic cập nhật vào `self_update.py` với state snapshot thread-safe; cấu hình URL tập trung trong `config.py`; lưu cài đặt bật/tắt vào `%APPDATA%\9router-patch\settings.json`; chạy thread daemon `_auto_update_worker()` chu kỳ 300s (5 phút) tại `main.py`; cung cấp 2 endpoint FastAPI `/update/self` và `/settings/auto-update` (bảo vệ bằng CSRF); tích hợp giao diện pixel toggle switch và nút kiểm tra thủ công vào `templates/update.html`.

**Tech Stack:** Python 3.11+ (tested on 3.14), stdlib (`urllib.request`, `hashlib`, `json`, `os`, `shutil`, `threading`, `time`), FastAPI, Starlette, Jinja2.

**Spec:** `docs/superpowers/specs/2026-09-22-self-update-design.md`

## Global Constraints

- Never expose, log, or commit `.env` contents, API keys, or provider secrets.
- Never read or modify the proxy's `data.sqlite` directly from the dashboard.
- Local dashboard only accepts `127.0.0.1:20129`. Mutating POST requests enforce local `Origin`/`Referer` (`CSRF`).
- Backups are stored outside the repo tree (`<repo-parent>/9router-backups/`). Never commit backup files.
- UI privacy: dashboard never renders patch `find`/`replace`/`why`, internal file paths (`server/*`, build tree), or lock file paths.
- Attribution on git commit messages:
  `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`
  `🤖 Generated with [Claude Code](https://claude.com/claude-code)`

---

### Task 1: Cấu hình URL và so sánh phiên bản semver

**Files:**
- Create: `config.py`
- Create: `self_update.py`
- Test: `tests/test_self_update.py`

**Interfaces:**
- Produces:
  - `config.UPDATE_BASE_URL: str`
  - `config.VERSION_CHECK_URL: str`
  - `config.AUTO_UPDATE_INTERVAL_SECONDS: int`
  - `self_update.parse_version(v: str) -> tuple[int, ...]`
  - `self_update.is_newer(remote: str, local: str) -> bool`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_self_update.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_self_update.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'self_update'`

- [ ] **Step 3: Write minimal implementation**

```python
# config.py
"""Centralized configuration for 9router Patch Manager self-update."""
from __future__ import annotations

UPDATE_BASE_URL = "https://phmyhu1710.dev/9router-patcher"
VERSION_CHECK_URL = f"{UPDATE_BASE_URL}/version.json"
AUTO_UPDATE_INTERVAL_SECONDS = 300  # 5 phút
```

```python
# self_update.py
"""Self-update engine for 9router Patch Manager standalone executable."""
from __future__ import annotations

import re
import config
import version


def parse_version(v: str) -> tuple[int, ...]:
    """Chuyển chuỗi version thành tuple 4 số nguyên để so sánh chuẩn xác."""
    cleaned = re.sub(r"^[^\d]*", "", (v or "").strip())
    parts = []
    for chunk in cleaned.split("."):
        m = re.match(r"^\d+", chunk)
        if m:
            parts.append(int(m.group(0)))
        else:
            break
    if not parts:
        return (0, 0, 0, 0)
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])


def is_newer(remote: str, local: str) -> bool:
    """True nếu phiên bản remote lớn hơn local."""
    return parse_version(remote) > parse_version(local)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_self_update.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add config.py self_update.py tests/test_self_update.py
git commit -m "feat(self-update): add config and semver version parser"
```

---

### Task 2: Lưu trữ cài đặt người dùng (`settings.json`)

**Files:**
- Modify: `self_update.py`
- Test: `tests/test_self_update.py`

**Interfaces:**
- Consumes: `app_paths.get_app_data_dir() -> Path`
- Produces:
  - `self_update.get_settings_file() -> Path`
  - `self_update.is_enabled() -> bool`
  - `self_update.set_enabled(on: bool) -> None`

- [ ] **Step 1: Write the failing test**

```python
# Append to tests/test_self_update.py
def test_settings_persistence(tmp_path, monkeypatch):
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_self_update.py -k test_settings_persistence`
Expected: FAIL with `AttributeError: module 'self_update' has no attribute 'is_enabled'`

- [ ] **Step 3: Write minimal implementation**

Add to `self_update.py`:
```python
import json
import os
from pathlib import Path
import app_paths


def get_settings_file() -> Path:
    return app_paths.get_app_data_dir() / "settings.json"


def is_enabled() -> bool:
    """Đọc cấu hình auto_update từ settings.json, mặc định là True."""
    sf = get_settings_file()
    if not sf.is_file():
        return True
    try:
        data = json.loads(sf.read_text(encoding="utf-8"))
        return bool(data.get("auto_update", True))
    except Exception:
        return True


def set_enabled(on: bool) -> None:
    """Lưu cấu hình auto_update nguyên tử bằng file tạm + replace."""
    sf = get_settings_file()
    sf.parent.mkdir(parents=True, exist_ok=True)
    tmp = sf.with_suffix(".tmp")
    payload = {"auto_update": bool(on)}
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, sf)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_self_update.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add self_update.py tests/test_self_update.py
git commit -m "feat(self-update): add auto_update switch settings persistence"
```

---

### Task 3: Kiểm tra phiên bản từ server (`check_update`)

**Files:**
- Modify: `self_update.py`
- Test: `tests/test_self_update.py`

**Interfaces:**
- Consumes: `config.VERSION_CHECK_URL`, `version.APP_VERSION`
- Produces:
  - `self_update.check_update(url: str | None = None) -> dict | None`
  - Trả về dictionary:
    ```python
    {
        "version": "2.0.1",
        "url": "https://...",
        "sha256": "...",
        "changelog": "...",
        "has_update": True,
    }
    ```

- [ ] **Step 1: Write the failing test**

```python
# Append to tests/test_self_update.py
import urllib.error
from io import BytesIO


def test_check_update_parses_json_and_detects_newer_version(monkeypatch):
    import self_update

    sample_json = b'{"version": "2.1.0", "url": "https://example.com/app.exe", "sha256": "abcdef", "changelog": "test"}'

    class DummyResponse:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return sample_json

    monkeypatch.setattr(self_update.urllib.request, "urlopen", lambda req, timeout=5: DummyResponse())

    res = self_update.check_update()
    assert res is not None
    assert res["version"] == "2.1.0"
    assert res["has_update"] is True
    assert res["url"] == "https://example.com/app.exe"


def test_check_update_handles_network_error_gracefully(monkeypatch):
    import self_update

    def fail(*a, **k):
        raise urllib.error.URLError("server down")

    monkeypatch.setattr(self_update.urllib.request, "urlopen", fail)
    assert self_update.check_update() is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_self_update.py -k "test_check_update"`
Expected: FAIL with `AttributeError: module 'self_update' has no attribute 'check_update'`

- [ ] **Step 3: Write minimal implementation**

Add to `self_update.py`:
```python
import urllib.request


def check_update(url: str | None = None) -> dict | None:
    """Gửi HTTP GET kiểm tra version.json từ hosting, timeout 5s."""
    target_url = url or config.VERSION_CHECK_URL
    req = urllib.request.Request(
        target_url,
        headers={"User-Agent": f"9router-patcher/{version.APP_VERSION}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            raw = resp.read().decode("utf-8")
        data = json.loads(raw)
        rem_ver = str(data.get("version", "")).strip()
        dl_url = str(data.get("url", "")).strip()
        if not rem_ver or not dl_url:
            return None
        return {
            "version": rem_ver,
            "url": dl_url,
            "sha256": str(data.get("sha256", "")).strip().lower(),
            "changelog": str(data.get("changelog", "")).strip(),
            "has_update": is_newer(rem_ver, version.APP_VERSION),
        }
    except Exception:
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_self_update.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add self_update.py tests/test_self_update.py
git commit -m "feat(self-update): implement check_update with network error handling"
```

---

### Task 4: Tải file, kiểm tra SHA256, hoán đổi nguyên tử & dọn dẹp

**Files:**
- Modify: `self_update.py`
- Test: `tests/test_self_update.py`

**Interfaces:**
- Consumes: `app_paths.is_frozen()`
- Produces:
  - `self_update.download_and_swap(meta: dict, current_exe: Path | None = None) -> dict`
  - `self_update.cleanup_old_files(exe_dir: Path | None = None) -> int`
  - `self_update.state() -> dict`

- [ ] **Step 1: Write the failing test**

```python
# Append to tests/test_self_update.py
import hashlib


def test_download_and_swap_success_flow(tmp_path, monkeypatch):
    import self_update

    exe_file = tmp_path / "9router-patch.exe"
    exe_file.write_bytes(b"OLD_VERSION_EXE")

    new_content = b"NEW_VERSION_EXE_DATA"
    valid_sha = hashlib.sha256(new_content).hexdigest()

    class DummyDlResponse:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self, chunk_size=65536):
            if not hasattr(self, "_done"):
                self._done = True
                return new_content
            return b""

    monkeypatch.setattr(self_update.urllib.request, "urlopen", lambda req, timeout=30: DummyDlResponse())
    monkeypatch.setattr(self_update.app_paths, "is_frozen", lambda: True)

    meta = {
        "version": "2.0.1",
        "url": "https://example.com/files/app.exe",
        "sha256": valid_sha,
    }

    res = self_update.download_and_swap(meta, current_exe=exe_file)
    assert res["ok"] is True
    assert exe_file.read_bytes() == new_content

    # Kiểm tra file .old tồn tại
    old_files = list(tmp_path.glob("9router-patch.old-*"))
    assert len(old_files) == 1
    assert old_files[0].read_bytes() == b"OLD_VERSION_EXE"

    # Dọn dẹp
    cleaned = self_update.cleanup_old_files(exe_dir=tmp_path)
    assert cleaned == 1
    assert len(list(tmp_path.glob("9router-patch.old-*"))) == 0


def test_download_and_swap_rejects_sha256_mismatch(tmp_path, monkeypatch):
    import self_update

    exe_file = tmp_path / "9router-patch.exe"
    exe_file.write_bytes(b"ORIGINAL_EXE")

    new_content = b"CORRUPTED_EXE"

    class DummyDlResponse:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self, chunk_size=65536):
            if not hasattr(self, "_done"):
                self._done = True
                return new_content
            return b""

    monkeypatch.setattr(self_update.urllib.request, "urlopen", lambda req, timeout=30: DummyDlResponse())
    monkeypatch.setattr(self_update.app_paths, "is_frozen", lambda: True)

    meta = {
        "version": "2.0.1",
        "url": "https://example.com/files/app.exe",
        "sha256": "0000000000000000000000000000000000000000000000000000000000000000",
    }

    res = self_update.download_and_swap(meta, current_exe=exe_file)
    assert res["ok"] is False
    assert "SHA256 mismatch" in res["error"]
    assert exe_file.read_bytes() == b"ORIGINAL_EXE"
    assert not (tmp_path / "9router-patch.new").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_self_update.py -k "test_download_and_swap"`
Expected: FAIL with `AttributeError: module 'self_update' has no attribute 'download_and_swap'`

- [ ] **Step 3: Write minimal implementation**

Add to `self_update.py`:
```python
import hashlib
import sys
import threading
import time

_SWAP_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()
_STATE = {
    "checked_at": 0.0,
    "remote_version": "",
    "has_update": False,
    "phase": "idle",  # idle, checking, downloading, ready, error, dev-mode
    "error": None,
    "applied_version": None,
    "changelog": "",
}


def state() -> dict:
    """Trả về bản sao snapshot trạng thái self-update an toàn đa luồng."""
    with _STATE_LOCK:
        return dict(_STATE)


def _set_state(**kwargs):
    with _STATE_LOCK:
        _STATE.update(kwargs)


def cleanup_old_files(exe_dir: Path | None = None) -> int:
    """Xóa các file .old-* và .new còn sót lúc khởi động ứng dụng."""
    if exe_dir is None:
        if not app_paths.is_frozen():
            return 0
        exe_dir = Path(sys.argv[0]).resolve().parent

    count = 0
    patterns = ["9router-patch.old-*", "9router-patch.new"]
    for pat in patterns:
        for f in exe_dir.glob(pat):
            try:
                f.unlink()
                count += 1
            except (OSError, PermissionError):
                pass
    return count


def download_and_swap(meta: dict, current_exe: Path | None = None) -> dict:
    """Tải bản mới, kiểm tra SHA256 và hoán đổi nguyên tử vào exe hiện tại."""
    if not _SWAP_LOCK.acquire(blocking=False):
        return {"ok": False, "error": "Cập nhật đang diễn ra"}

    try:
        if not app_paths.is_frozen() and current_exe is None:
            _set_state(phase="dev-mode", error="Chạy từ source .py, bỏ qua hoán đổi exe.")
            return {"ok": False, "error": "dev-mode"}

        exe = current_exe or Path(sys.argv[0]).resolve()
        exe_dir = exe.parent
        new_file = exe_dir / "9router-patch.new"

        _set_state(phase="downloading", error=None, remote_version=meta.get("version", ""))
        dl_url = meta["url"]
        expected_sha = meta.get("sha256", "").strip().lower()

        hasher = hashlib.sha256()
        req = urllib.request.Request(dl_url, headers={"User-Agent": f"9router-patcher/{version.APP_VERSION}"})
        try:
            with urllib.request.urlopen(req, timeout=30.0) as resp, open(new_file, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    hasher.update(chunk)
        except Exception as e:
            if new_file.exists():
                try: new_file.unlink()
                except Exception: pass
            _set_state(phase="error", error=f"Lỗi tải file: {e}")
            return {"ok": False, "error": str(e)}

        actual_sha = hasher.hexdigest().lower()
        if expected_sha and actual_sha != expected_sha:
            if new_file.exists():
                try: new_file.unlink()
                except Exception: pass
            err = f"SHA256 mismatch: mong muốn {expected_sha[:8]}..., nhận {actual_sha[:8]}..."
            _set_state(phase="error", error=err)
            return {"ok": False, "error": err}

        # Hoán đổi nguyên tử
        old_file = exe_dir / f"9router-patch.old-{int(time.time())}"
        try:
            os.replace(exe, old_file)
            os.replace(new_file, exe)
        except Exception as e:
            if old_file.exists() and not exe.exists():
                try: os.replace(old_file, exe)
                except Exception: pass
            _set_state(phase="error", error=f"Lỗi hoán đổi file exe: {e}")
            return {"ok": False, "error": str(e)}

        _set_state(phase="ready", applied_version=meta["version"], has_update=False, error=None)
        return {"ok": True, "error": None}
    finally:
        _SWAP_LOCK.release()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_self_update.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add self_update.py tests/test_self_update.py
git commit -m "feat(self-update): implement atomic download, verify and swap"
```

---

### Task 5: Background Worker loop & Thread lifecycle

**Files:**
- Modify: `self_update.py`
- Modify: `main.py:297-304` (lifespan)
- Test: `tests/test_self_update.py`

**Interfaces:**
- Consumes: `self_update.is_enabled()`, `self_update.check_update()`, `self_update.download_and_swap()`
- Produces:
  - `self_update.run_worker(stop_event: threading.Event, interval: float = 300.0) -> None`

- [ ] **Step 1: Write the failing test**

```python
# Append to tests/test_self_update.py
def test_worker_skips_when_disabled(monkeypatch):
    import self_update

    called = []
    monkeypatch.setattr(self_update, "is_enabled", lambda: False)
    monkeypatch.setattr(self_update, "check_update", lambda: called.append(True) or None)

    stop = threading.Event()
    stop.set()  # Stop ngay sau lượt đầu
    self_update.run_worker(stop, interval=0.01)

    assert len(called) == 0


def test_worker_downloads_when_newer_found(monkeypatch):
    import self_update

    meta = {"version": "2.1.0", "has_update": True, "url": "https://test.com/app.exe"}
    swapped = []
    monkeypatch.setattr(self_update, "is_enabled", lambda: True)
    monkeypatch.setattr(self_update, "check_update", lambda: meta)
    monkeypatch.setattr(self_update, "download_and_swap", lambda m: swapped.append(m) or {"ok": True})

    stop = threading.Event()
    stop.set()
    self_update.run_worker(stop, interval=0.01)

    assert len(swapped) == 1
    assert swapped[0]["version"] == "2.1.0"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_self_update.py -k "test_worker"`
Expected: FAIL with `AttributeError: module 'self_update' has no attribute 'run_worker'`

- [ ] **Step 3: Write minimal implementation**

Add to `self_update.py`:
```python
def run_worker(stop_event: threading.Event, interval: float | None = None) -> None:
    """Vòng lặp thread daemon tự động kiểm tra và tải bản cập nhật nền."""
    sleep_time = interval if interval is not None else config.AUTO_UPDATE_INTERVAL_SECONDS
    while not stop_event.is_set():
        try:
            if is_enabled():
                _set_state(phase="checking")
                meta = check_update()
                now = time.time()
                if meta:
                    _set_state(checked_at=now, remote_version=meta["version"],
                               has_update=meta["has_update"], changelog=meta.get("changelog", ""),
                               phase="idle" if not meta["has_update"] else _STATE["phase"])
                    if meta["has_update"]:
                        download_and_swap(meta)
                else:
                    _set_state(checked_at=now, phase="idle")
        except Exception as e:
            _set_state(phase="error", error=str(e))
        if stop_event.wait(sleep_time):
            break
```

Wire into `main.py:297-304`:
```python
# In main.py
import self_update

_UPDATE_STOP = threading.Event()

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    threading.Thread(target=_snap_worker, name="snap-refresh", daemon=True).start()
    threading.Thread(target=self_update.run_worker, args=(_UPDATE_STOP,),
                     name="auto-update-worker", daemon=True).start()
    self_update.cleanup_old_files()
    try:
        yield
    finally:
        _UPDATE_STOP.set()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_self_update.py -v`
Expected: PASS (9 passed)

- [ ] **Step 5: Commit**

```bash
git add self_update.py main.py tests/test_self_update.py
git commit -m "feat(self-update): add background worker and integrate into main lifespan"
```

---

### Task 6: FastAPI Endpoints (`/update/self` và `/settings/auto-update`)

**Files:**
- Modify: `main.py`
- Modify: `tests/test_web.py`

**Interfaces:**
- Produces routes:
  - `POST /update/self` (CSRF) -> `{"ok": bool, "error": str | None, "state": dict}`
  - `POST /settings/auto-update` (CSRF) -> `{"ok": bool, "auto_update": bool}`
- Context in `update_page`:
  - `"self_update": self_update.state()`
  - `"auto_update_enabled": self_update.is_enabled()`

- [ ] **Step 1: Write the failing test**

```python
# In tests/test_web.py
def test_toggle_auto_update_setting(web):
    client = web["client"]
    # 1. CSRF guard: không có Origin/Referer -> 403
    r = client.post("/settings/auto-update", data={"enabled": "0"})
    assert r.status_code == 403

    # 2. Hợp lệ:
    headers = {"Origin": OWN}
    r = client.post("/settings/auto-update", data={"enabled": "0"}, headers=headers)
    assert r.status_code == 200
    assert r.json()["auto_update"] is False

    r = client.post("/settings/auto-update", data={"enabled": "1"}, headers=headers)
    assert r.status_code == 200
    assert r.json()["auto_update"] is True


def test_post_update_self_endpoint(web, monkeypatch):
    import self_update
    client = web["client"]
    headers = {"Origin": OWN}

    monkeypatch.setattr(self_update, "check_update", lambda: {
        "version": "2.0.1", "has_update": True, "url": "https://fake.com/exe", "sha256": ""
    })
    monkeypatch.setattr(self_update, "download_and_swap", lambda meta: {"ok": True, "error": None})

    r = client.post("/update/self", headers=headers)
    assert r.status_code == 200
    assert r.json()["ok"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_web.py -k "test_toggle_auto_update_setting"`
Expected: FAIL with 404 Not Found

- [ ] **Step 3: Write minimal implementation**

Add to `main.py`:
```python
@app.post("/settings/auto-update", dependencies=CSRF)
def set_auto_update(request: Request, enabled: Annotated[str, Form()] = "1"):
    on = enabled in ("1", "true", "True")
    self_update.set_enabled(on)
    return JSONResponse({"ok": True, "auto_update": on})


@app.post("/update/self", dependencies=CSRF)
def trigger_self_update(request: Request):
    if not OP_SLOT.acquire("job"):
        return JSONResponse({"ok": False, "error": "Một thao tác khác đang chạy"}, status_code=409)
    try:
        meta = self_update.check_update()
        if not meta:
            return JSONResponse({"ok": False, "error": "Không kết nối được máy chủ cập nhật",
                                 "state": self_update.state()})
        if not meta["has_update"]:
            return JSONResponse({"ok": True, "error": None, "message": "Đã là bản mới nhất",
                                 "state": self_update.state()})
        res = self_update.download_and_swap(meta)
        return JSONResponse({"ok": res["ok"], "error": res["error"], "state": self_update.state()})
    finally:
        OP_SLOT.release()
```

Update `update_page` in `main.py:530`:
```python
    ctx = {
        "local_version": local, "latest_version": latest,
        "locks": LOCK_CACHE["locks"],
        "steps": LAST_UPDATE_STEPS,
        "history": _load_history(),
        "probed_at": LOCK_CACHE["probed_at"] or None,
        "self_update": self_update.state(),
        "auto_update_enabled": self_update.is_enabled(),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_web.py -k "test_toggle_auto_update_setting or test_post_update_self_endpoint"`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_web.py
git commit -m "feat(web): add /update/self and /settings/auto-update routes"
```

---

### Task 7: Tích hợp Giao diện Web (`templates/update.html`)

**Files:**
- Modify: `templates/update.html`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `self_update` state dictionary, `auto_update_enabled` boolean
- Visual Elements:
  - Panel "Cập nhật ứng dụng 9router Patch Manager"
  - Switch: Tự động kiểm tra bản mới mỗi 5 phút (chạy nền)
  - Nút "Kiểm tra & Cập nhật exe ngay"
  - Status badges (`ok`, `info`, `warn`, `bad`)

- [ ] **Step 1: Write the failing test**

```python
# Append to tests/test_web.py
def test_update_page_renders_self_update_section(web):
    client = web["client"]
    r = client.get("/update")
    assert r.status_code == 200
    assert "Cập nhật 9router Patch Manager" in r.text
    assert "name=\"auto_update_toggle\"" in r.text
    assert "self-update-btn" in r.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_web.py -k test_update_page_renders_self_update_section`
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

Chèn panel vào đầu trang `templates/update.html` (ngay dưới panel Local -> npm latest):
```html
<section class="card panel" id="self-update-panel">
  <div class="panel-head">
    <h2>{{ icon('refresh') }}Cập nhật 9router Patch Manager (exe)</h2>
    <span style="margin-left:auto"></span>
    {% set su = self_update %}
    {% if su.phase == "ready" %}
      <span class="badge ok">Đã tải v{{ su.applied_version }} (áp dụng khi mở lại)</span>
    {% elif su.phase == "downloading" %}
      <span class="badge warn"><span class="spin inline"></span> Đang tải exe mới…</span>
    {% elif su.has_update %}
      <span class="badge info">Có bản mới v{{ su.remote_version }}</span>
    {% elif su.phase == "dev-mode" %}
      <span class="badge muted">Môi trường Dev (.py)</span>
    {% else %}
      <span class="badge ok">Đang dùng v{{ app_version }} (mới nhất)</span>
    {% endif %}
  </div>

  <div style="margin-bottom:.8rem">
    <label class="switch">
      <input type="checkbox" id="auto-update-toggle" name="auto_update_toggle"
             {% if auto_update_enabled %}checked{% endif %}>
      Tự động kiểm tra &amp; tải bản mới mỗi 5 phút (chạy nền)
    </label>
  </div>

  <div style="display:flex; gap:.6rem; align-items:center; flex-wrap:wrap">
    <button type="button" class="btn btn-ghost btn-sm" id="self-update-btn">
      <svg class="spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" aria-hidden="true"><path d="M21 12a9 9 0 1 1-6.22-8.56"/></svg>
      <span class="btn-label">{{ icon('refresh') }}Kiểm tra &amp; Cập nhật exe ngay</span>
    </button>
    <span class="small muted" id="self-update-msg">
      {% if su.changelog %}{{ su.changelog }}{% endif %}
    </span>
  </div>
</section>
```

Và thêm JavaScript xử lý sự kiện vào block scripts trong `templates/update.html`:
```javascript
  var autoToggle = document.getElementById("auto-update-toggle");
  if (autoToggle) {
    autoToggle.addEventListener("change", function(){
      var val = autoToggle.checked ? "1" : "0";
      fetch("/settings/auto-update", {
        method: "POST",
        credentials: "same-origin",
        headers: {"Content-Type": "application/x-www-form-urlencoded"},
        body: "enabled=" + val
      }).then(function(r){ return r.json(); })
        .then(function(j){ window.toast(j.auto_update ? "Đã bật tự động cập nhật nền" : "Đã tắt tự động cập nhật nền"); })
        .catch(function(){ window.toast("Lỗi lưu cài đặt", "bad"); });
    });
  }

  var selfBtn = document.getElementById("self-update-btn");
  if (selfBtn) {
    selfBtn.addEventListener("click", function(){
      selfBtn.disabled = true; selfBtn.classList.add("is-loading");
      var msg = document.getElementById("self-update-msg");
      if (msg) msg.textContent = "Đang kiểm tra máy chủ…";
      fetch("/update/self", {
        method: "POST",
        credentials: "same-origin",
        headers: {"Content-Type": "application/x-www-form-urlencoded"},
        body: "trigger=1"
      }).then(function(r){ return r.json(); })
        .then(function(j){
          selfBtn.disabled = false; selfBtn.classList.remove("is-loading");
          if (!j.ok) {
            window.toast(j.error || "Cập nhật thất bại", "bad");
            if (msg) msg.textContent = j.error || "";
          } else {
            window.toast(j.message || "Cập nhật hoàn tất!");
            setTimeout(function(){ location.reload(); }, 1200);
          }
        }).catch(function(){
          selfBtn.disabled = false; selfBtn.classList.remove("is-loading");
          window.toast("Lỗi mạng khi kiểm tra cập nhật", "bad");
        });
    });
  }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_web.py -k test_update_page_renders_self_update_section`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add templates/update.html tests/test_web.py
git commit -m "feat(web): add self-update panel and toggle switch to update page"
```

---

### Task 8: `build_app.py` Artifact Copy & Full Suite Verification

**Files:**
- Modify: `build_app.py:116-124`
- Test: `tests/test_build_pipeline.py`

**Interfaces:**
- Produces: `dist/9router-patcher-v{APP_VERSION}.exe` copy sau khi build thành công để tiện upload lên hosting.

- [ ] **Step 1: Write the failing test**

```python
# In tests/test_build_pipeline.py
def test_build_script_prepares_versioned_artifact_name():
    import build_app
    import version

    expected_name = f"9router-patcher-v{version.APP_VERSION}.exe"
    assert "9router-patcher-v" in expected_name
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_build_pipeline.py -v`

- [ ] **Step 3: Write minimal implementation**

In `build_app.py:117`:
```python
    exe = dist / "9router-patch.exe"
    if exe.is_file():
        versioned_exe = dist / f"9router-patcher-v{version.APP_VERSION}.exe"
        try:
            shutil.copyfile(exe, versioned_exe)
        except Exception:
            pass
        size_mb = exe.stat().st_size / (1024 * 1024)
        print(f"\nEncrypt: {t1 - t0:.1f}s | Nuitka: {t2 - t1:.1f}s | Total: {t2 - t0:.1f}s")
        print(f"SUCCESS: Built {exe} ({size_mb:.1f} MB)")
        if versioned_exe.is_file():
            print(f"Artifact for hosting upload: {versioned_exe.name}")
        return 0
```

- [ ] **Step 4: Run full test suite to verify no regressions**

Run: `python -m pytest tests/ -q`
Expected: 300+ passed, 4 skipped.

- [ ] **Step 5: Commit**

```bash
git add build_app.py tests/test_build_pipeline.py
git commit -m "feat(build): auto-generate versioned artifact for hosting upload"
```
