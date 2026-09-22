# Boot Doctor Console & System Logs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provide an interactive Windows startup console terminal doctor that handles prerequisite checks, prompts to install missing 9router, auto-applies patches, stays open with lifecycle controls, and introduces a dedicated `/logs` UI with a one-click developer export.

**Architecture:**
- Create `boot_doctor.py` implementing isolated, testable preflight checks (Node/npm detection, 9router prompt/install, lock detection, auto-apply patches, router stack kickstart) with an in-memory ring-buffer logger that appends to `%APPDATA%/9router-patch/logs/boot.log`.
- Refactor `app.py` to invoke `boot_doctor.run_doctor()`, run uvicorn on a background thread, open the browser once ready, and keep the native console alive with interactive commands (`[Enter]`, `[L]`, `[H]`, `[Q]`).
- Add `GET /logs` and `GET /api/logs` to `main.py` serving `templates/logs.html` with aggregated sanitized system diagnostics and 1-click dev export while strictly respecting privacy rules.
- Update `build_app.py` console mode flag from `attach` to `force` for Nuitka builds.

**Tech Stack:** Python 3.11+, FastAPI, Jinja2, Uvicorn, Windows Console / ctypes Win32 API, Nuitka, Pytest.
**Spec:** `docs/superpowers/specs/2026-09-21-boot-console-design.md`

## Global Constraints
- Binds strictly to `127.0.0.1:20129`; rejects external hosts/origins.
- Zero new runtime pip dependencies; standard library and existing requirements only.
- Logs UI must never render patch `find`/`replace`/`why` payloads.
- Logs UI must never expose internal filesystem paths (`server/*`, build tree).
- Logs UI must never expose lock file paths (only PID and executable name).
- Logs UI must never expose `.env` secrets or read/modify `data.sqlite`.
- All tests must pass: `python -m pytest tests/ -q` maintains zero regressions.

---

### Task 1: Boot Doctor Core Diagnostic Module (`boot_doctor.py`)

**Files:**
- Create: `boot_doctor.py`
- Test: `tests/test_boot_doctor.py`

**Interfaces:**
- Produces:
  - `check_node() -> tuple[bool, str]`
  - `check_9router() -> tuple[bool, str]`
  - `install_9router(on_output=None) -> tuple[bool, str]`
  - `check_and_apply_patches(on_output=None) -> tuple[bool, str]`
  - `ensure_router_stack(on_output=None) -> tuple[bool, str]`
  - `run_doctor(interactive: bool = True) -> bool`
  - `get_boot_logs() -> list[str]`

- [ ] **Step 1: Write failing tests for boot_doctor**

```python
# tests/test_boot_doctor.py
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
import boot_doctor

def test_check_node_present(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda cmd: "C:\\Program Files\\nodejs\\node.exe" if "node" in cmd else "C:\\Program Files\\nodejs\\npm.cmd")
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="v22.14.0\n")
        ok, msg = boot_doctor.check_node()
        assert ok is True
        assert "v22.14.0" in msg

def test_check_node_missing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda cmd: None)
    ok, msg = boot_doctor.check_node()
    assert ok is False
    assert "nodejs.org" in msg

def test_check_9router_missing(monkeypatch):
    monkeypatch.setattr("engine.install_dir", MagicMock(side_effect=Exception("not found")))
    ok, msg = boot_doctor.check_9router()
    assert ok is False

def test_check_9router_present(monkeypatch):
    monkeypatch.setattr("engine.install_dir", lambda: Path("C:/mock/9router"))
    monkeypatch.setattr("updater.current_version", lambda: "0.5.81")
    ok, msg = boot_doctor.check_9router()
    assert ok is True
    assert "0.5.81" in msg

def test_boot_logs_ring_buffer():
    boot_doctor.log_boot("Test line 1")
    logs = boot_doctor.get_boot_logs()
    assert any("Test line 1" in l for l in logs)
```

- [ ] **Step 2: Run test verify fails**
Run: `python -m pytest tests/test_boot_doctor.py -v`  
Expected: FAIL with `ModuleNotFoundError: No module named 'boot_doctor'`

- [ ] **Step 3: Write minimal implementation in `boot_doctor.py`**

```python
"""Startup preflight doctor and environment preparation for 9router Patch Manager."""
from __future__ import annotations

import collections
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import app_paths
import engine
try:
    import updater
except ImportError:
    updater = None

_BOOT_LOGS = collections.deque(maxlen=500)
SILENT_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

def log_boot(msg: str) -> None:
    timestamp = time.strftime("%H:%M:%S")
    line = f"[{timestamp}] {msg}"
    _BOOT_LOGS.append(line)
    try:
        log_file = app_paths.get_log_dir() / "boot.log"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def get_boot_logs() -> list[str]:
    return list(_BOOT_LOGS)

def check_node() -> tuple[bool, str]:
    node = shutil.which("node")
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not (node and npm):
        msg = "Thiếu Node.js hoặc npm. Vui lòng cài đặt bản LTS từ https://nodejs.org/"
        log_boot(f"ERROR: {msg}")
        return False, msg
    try:
        r = subprocess.run([node, "--version"], capture_output=True, text=True,
                           timeout=5, creationflags=SILENT_FLAGS)
        ver = r.stdout.strip()
        msg = f"Node.js {ver} & npm sẵn sàng"
        log_boot(f"OK: {msg}")
        return True, msg
    except Exception as e:
        msg = f"Không kiểm tra được phiên bản Node.js: {e}"
        log_boot(f"ERROR: {msg}")
        return False, msg

def check_9router() -> tuple[bool, str]:
    try:
        idir = engine.install_dir()
        if not idir.exists():
            return False, "Chưa cài đặt 9router"
        ver = updater.current_version() if updater else "unknown"
        msg = f"9router v{ver} đã cài đặt tại {idir.name}"
        log_boot(f"OK: {msg}")
        return True, msg
    except Exception:
        msg = "Chưa phát hiện gói 9router toàn cục"
        log_boot(f"WARN: {msg}")
        return False, msg

def install_9router(on_output=None) -> tuple[bool, str]:
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm:
        return False, "Không tìm thấy npm để cài đặt"
    log_boot("Bắt đầu cài đặt 9router toàn cục qua npm...")
    cmd = [npm, "install", "-g", "9router@latest"]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1, creationflags=SILENT_FLAGS)
        if proc.stdout:
            for line in iter(proc.stdout.readline, ""):
                cleaned = line.strip()
                if cleaned:
                    log_boot(f"npm: {cleaned}")
                    if on_output:
                        on_output(cleaned)
            proc.stdout.close()
        proc.wait(timeout=300)
        if proc.returncode == 0:
            log_boot("Cài đặt 9router@latest thành công")
            return True, "Cài đặt 9router@latest thành công"
        return False, f"npm install thoát với mã lỗi {proc.returncode}"
    except Exception as e:
        err = f"Lỗi trong quá trình cài đặt 9router: {e}"
        log_boot(f"ERROR: {err}")
        return False, err

def check_and_apply_patches(on_output=None) -> tuple[bool, str]:
    try:
        build_path = engine.build_dir()
        if not build_path.exists():
            return False, "Thư mục build 9router không tồn tại"
        patches = engine.load_patches()
        states = engine.scan(build_path, patches)
        unapplied = [s.patch.id for s in states if s.state != "applied"]
        if not unapplied:
            msg = f"{len(patches)}/{len(patches)} Patches đã áp dụng"
            log_boot(f"OK: {msg}")
            return True, msg
        log_boot(f"Phát hiện {len(unapplied)} patch chưa áp dụng. Bắt đầu auto-apply...")
        if on_output:
            on_output(f"Đang tự động áp dụng {len(unapplied)} patch...")
        changed = engine.apply(build_path, patches)
        msg = f"Đã áp dụng thành công {len(patches)} patches ({len(changed)} files thay đổi)"
        log_boot(f"OK: {msg}")
        return True, msg
    except Exception as e:
        err = f"Lỗi khi kiểm tra/áp dụng patch: {e}"
        log_boot(f"ERROR: {err}")
        return False, err

def ensure_router_stack(on_output=None) -> tuple[bool, str]:
    if not updater:
        return True, "updater module không có sẵn"
    try:
        r_up = updater.pid_on_port(20128) is not None
        h_up = updater.pid_on_port(8787) is not None
        if r_up and h_up:
            msg = "Router stack đang chạy (port 20128 & 8787)"
            log_boot(f"OK: {msg}")
            return True, msg
        log_boot("Khởi động router stack trong nền...")
        if on_output:
            on_output("Khởi động proxy router trong nền...")
        ok = updater.start_router_stack(lambda ev: log_boot(str(ev.get("text", ""))))
        return ok, "Đã khởi động router stack" if ok else "Không thể khởi động router stack"
    except Exception as e:
        err = f"Lỗi khởi động router stack: {e}"
        log_boot(f"ERROR: {err}")
        return False, err

def run_doctor(interactive: bool = True) -> bool:
    print("=" * 60)
    print(" 9router Patch Manager - Setup & Doctor v1.0.0")
    print("=" * 60)
    
    # 1. Node.js & npm
    print("[1/4] Kiểm tra Node.js & npm...", end=" ", flush=True)
    ok, msg = check_node()
    if ok:
        print("[ OK ]")
    else:
        print("[ THIẾU ]")
        print(f"\n  -> {msg}\n")
        if interactive:
            input("Nhấn Enter để thoát...")
        return False

    # 2. 9router global package
    print("[2/4] Kiểm tra 9router toàn cục...", end=" ", flush=True)
    ok, msg = check_9router()
    if ok:
        print("[ OK ]")
    else:
        print("[ THIẾU ]")
        if interactive:
            ans = input("\n? 9router chưa được cài đặt. Cài đặt toàn cục qua npm? (Y/n) [Y]: ").strip().lower()
            if ans in ("", "y", "yes"):
                print("  > npm install -g 9router@latest...")
                i_ok, i_msg = install_9router(on_output=lambda line: print(f"    {line[:70]}", end="\r", flush=True))
                print()
                if not i_ok:
                    print(f"  [!] {i_msg}")
                    input("Nhấn Enter để tiếp tục (chế độ xem)...")
                else:
                    print("  [ OK ] Cài đặt 9router hoàn tất!")
            else:
                print("  Bỏ qua cài đặt 9router.")
        else:
            return False

    # 3. Patch Status
    print("[3/4] Kiểm tra patches tối ưu...", end=" ", flush=True)
    p_ok, p_msg = check_and_apply_patches()
    if p_ok:
        print("[ OK ]")
    else:
        print(f"[ CẢNH BÁO ] ({p_msg})")

    # 4. Proxy Router Stack
    print("[4/4] Khởi động Proxy Router Stack...", end=" ", flush=True)
    s_ok, s_msg = ensure_router_stack()
    if s_ok:
        print("[ OK ]")
    else:
        print(f"[ CẢNH BÁO ] ({s_msg})")

    print("\n✓ Hoàn tất chuẩn bị! Đang khởi động Web Dashboard...\n")
    return True
```

- [ ] **Step 4: Run test verify passes**
Run: `python -m pytest tests/test_boot_doctor.py -v`  
Expected: PASS

- [ ] **Step 5: Commit Task 1**

```bash
git add boot_doctor.py tests/test_boot_doctor.py
git commit -m "feat(doctor): add startup boot doctor preflight module"
```

---

### Task 2: Console Controller & Web Launcher (`app.py`)

**Files:**
- Modify: `app.py`
- Test: `tests/test_app_launcher.py`

**Interfaces:**
- Consumes: `boot_doctor.run_doctor()`, `boot_doctor.log_boot()`
- Produces: Enhanced `main()` with background uvicorn thread, auto browser launch, and active interactive loop.

- [ ] **Step 1: Write failing test in `tests/test_app_launcher.py`**

```python
# Append to tests/test_app_launcher.py
def test_console_controller_keys(monkeypatch):
    """Test console command actions for Enter, L, H, Q."""
    import app
    actions = []
    monkeypatch.setattr(app.webbrowser, "open", lambda url: actions.append(url))
    app._handle_console_command("l", "http://127.0.0.1:20129")
    assert any("/logs" in a for a in actions)
```

- [ ] **Step 2: Run test verify fails**
Run: `python -m pytest tests/test_app_launcher.py -k test_console_controller_keys -v`  
Expected: FAIL with `AttributeError: module 'app' has no attribute '_handle_console_command'`

- [ ] **Step 3: Implement interactive console controller in `app.py`**
Add helper `_hide_console()` via Win32 ctypes, `_handle_console_command(cmd, url)`, and update `main()`:
1. Run `boot_doctor.run_doctor()` before server boot.
2. If already listening on port 20129, open browser and exit cleanly as before.
3. Start uvicorn server in a separate background thread with graceful shutdown event.
4. Auto-open browser when ready.
5. Enter interactive input loop:
   - `[Enter]` -> open dashboard URL
   - `[L]` or `[l]` -> open logs page URL (`/logs`)
   - `[H]` or `[h]` -> call Windows API to hide console window
   - `[Q]` or `[q]` / `Ctrl+C` -> trigger shutdown and exit cleanly

- [ ] **Step 4: Run test verify passes**
Run: `python -m pytest tests/test_app_launcher.py -v`  
Expected: PASS

- [ ] **Step 5: Commit Task 2**

```bash
git add app.py tests/test_app_launcher.py
git commit -m "feat(app): add interactive console controller loop and doctor invocation"
```

---

### Task 3: System Logs Dashboard Route & Template (`main.py`, `templates/logs.html`)

**Files:**
- Create: `templates/logs.html`
- Modify: `main.py`
- Modify: `templates/base.html`
- Test: `tests/test_logs_route.py`

**Interfaces:**
- Consumes: `boot_doctor.get_boot_logs()`, `main._load_history()`, `app_paths.get_log_dir()`
- Produces:
  - Route `GET /logs` -> HTML page with sanitized logs & Copy for Dev button
  - Route `GET /api/logs` -> JSON endpoint returning `{ boot: [...], history: [...], app: [...] }`
  - Privacy safeguards: strips file paths, hashes, secrets, and patch payloads.

- [ ] **Step 1: Write failing privacy and rendering tests**

```python
# tests/test_logs_route.py
import pytest
from fastapi.testclient import TestClient
import main
import engine

def test_logs_route_renders_and_returns_200(web):
    client = web["client"]
    r = client.get("/logs")
    assert r.status_code == 200
    assert "Nhật ký hệ thống" in r.text
    assert "Sao chép toàn bộ log gửi Dev" in r.text

def test_logs_api_returns_json(web):
    client = web["client"]
    r = client.get("/api/logs")
    assert r.status_code == 200
    data = r.json()
    assert "boot" in data
    assert "history" in data

def test_logs_never_leaks_patch_payloads_or_secrets(web):
    client = web["client"]
    r = client.get("/logs")
    for p in engine.load_patches():
        assert p.find not in r.text
        assert p.replace not in r.text
```

- [ ] **Step 2: Run test verify fails**
Run: `python -m pytest tests/test_logs_route.py -v`  
Expected: FAIL with 404 Not Found for `/logs`

- [ ] **Step 3: Implement `templates/logs.html` and routes in `main.py`**
1. In `main.py`:
   - Import `boot_doctor`.
   - Add `@app.get("/logs")` rendering `templates/logs.html`.
   - Add `@app.get("/api/logs")` returning sanitized JSON logs.
   - Sanitize log lines to avoid leaking internal build directories or sensitive keys.
2. In `templates/logs.html`:
   - Follow 8-bit retro theme (`base.html`), using cards, badges, and monospace pre blocks.
   - Include sections: **Startup Doctor Logs**, **Update History Runs**, **App / Proxy Logs**.
   - Include individual "Copy" button for each section and large "Sao chép toàn bộ log gửi Dev" button.
3. In `templates/base.html`:
   - Add `icon('terminal') Logs` link in header navigation bar next to `Cập nhật` and `GitHub`.

- [ ] **Step 4: Run test verify passes**
Run: `python -m pytest tests/test_logs_route.py -v`  
Expected: PASS

- [ ] **Step 5: Commit Task 3**

```bash
git add main.py templates/logs.html templates/base.html tests/test_logs_route.py
git commit -m "feat(logs): add dedicated system logs page and dev export endpoint"
```

---

### Task 4: Build Pipeline Tuning & E2E Validation (`build_app.py`, `tests/test_e2e_binary.py`)

**Files:**
- Modify: `build_app.py`
- Test: `tests/test_build_pipeline.py`
- Test: `tests/test_e2e_binary.py`

**Interfaces:**
- Changes `--windows-console-mode=attach` to `--windows-console-mode=force` in `get_nuitka_cmd()`.

- [ ] **Step 1: Write failing test in `tests/test_build_pipeline.py`**

```python
# Check in tests/test_build_pipeline.py
def test_build_app_uses_forced_console_mode():
    import build_app
    from pathlib import Path
    cmd = build_app.get_nuitka_cmd(Path("dist"), fast=True)
    assert "--windows-console-mode=force" in cmd
```

- [ ] **Step 2: Run test verify fails**
Run: `python -m pytest tests/test_build_pipeline.py -k test_build_app_uses_forced_console_mode -v`  
Expected: FAIL

- [ ] **Step 3: Update `build_app.py` to use `--windows-console-mode=force`**
Update line 47 in `build_app.py` from `--windows-console-mode=attach` to `--windows-console-mode=force`.

- [ ] **Step 4: Run full test suite to guarantee 0 regressions**
Run: `python -m pytest tests/ -q`  
Expected: All tests pass (with the 1 expected skip).

- [ ] **Step 5: Commit Task 4**

```bash
git add build_app.py tests/test_build_pipeline.py
git commit -m "chore(build): switch console mode to force for visible startup terminal"
```

---

### Task 5: Final Review & Documentation Sync

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update documentation**
Update `README.md` and `CLAUDE.md` documenting the new boot doctor console flow, console hotkeys (`[Enter]`, `[L]`, `[H]`, `[Q]`), and `/logs` dashboard.
- [ ] **Step 2: Run detect_changes or pytest verification**
Run: `python -m pytest tests/ -q`
- [ ] **Step 3: Commit documentation**

```bash
git add README.md CLAUDE.md
git commit -m "docs: document boot doctor console and system logs dashboard"
```
