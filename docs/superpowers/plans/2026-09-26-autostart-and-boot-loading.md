# Implementation Plan: Auto Start Windows & Console Download Loading

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement Windows Auto Start capability (via HKCU Run registry with `--tray` flag) with Web UI toggle, and enhance `boot_doctor.py` console with live visual download progress (% / MB / spinner) during self-update.

**Architecture:**
- `autostart.py`: Windows registry abstraction using stdlib `winreg`, only active in frozen mode (`app_paths.is_frozen()`).
- `app.py`: Support `--tray` CLI argument to hide console to tray at boot and skip opening browser.
- `main.py` + `templates/update.html`: Endpoint `POST /settings/auto-start` and interactive toggle switch on `/update` page.
- `self_update.py`: Accept `on_progress` callback in `download_and_swap` reporting `(downloaded_bytes, total_bytes)`.
- `console_ui.py`: New `download_run()` function rendering a single-line animated progress bar with braille spinner.
- `boot_doctor.py`: Hook `console_ui.download_run` into Step [1/5] self-update.

**Tech Stack:** Python 3.11+, FastAPI, Jinja2, Windows `winreg` (stdlib), VT100 / ANSI console.

**Spec:** `docs/superpowers/specs/2026-09-26-autostart-and-boot-loading-design.md`

## Global Constraints
- Python standard library only for autostart (`winreg`) and progress calculation; no external dependencies.
- Zero credential exposure, no logging of secrets, no touching `data.sqlite`.
- Privacy & security: local CSRF protection (`same_origin`) on mutating POST endpoints.
- Hermetic tests: mock Windows registry and network in pytest suites.

---

### Task 1: Module `autostart.py` for Windows HKCU Run Registry

**Files:**
- Create: `autostart.py`
- Test: `tests/test_autostart.py`

**Interfaces:**
- Produces:
  - `autostart.is_supported() -> bool`
  - `autostart.is_enabled() -> bool`
  - `autostart.set_enabled(on: bool) -> tuple[bool, str | None]`
- Consumes:
  - `app_paths.is_frozen() -> bool`
  - `winreg` (stdlib)

- [ ] **Step 1: Write failing unit test `tests/test_autostart.py`**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_autostart.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'autostart'`

- [ ] **Step 3: Implement `autostart.py`**

```python
"""Windows HKCU Run registry helper for 9router Patch Manager Auto Start."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import app_paths

REG_NAME = "9RouterPatchManager"
REG_SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

try:
    import winreg
except ImportError:  # pragma: no cover (non-Windows platform)
    winreg = None  # type: ignore


def is_supported() -> bool:
    """True chỉ khi chạy từ bản đóng gói exe trên Windows."""
    return sys.platform == "win32" and winreg is not None and app_paths.is_frozen()


def _get_target_cmd() -> str:
    """Đường dẫn thực thi chuẩn của exe kèm flag --tray."""
    exe = Path(sys.argv[0]).resolve()
    # Nếu đang chạy file versioned cũ, chuẩn hóa về 9router-patch.exe
    target = exe.parent / "9router-patch.exe"
    run_exe = target if target.is_file() else exe
    return f'"{run_exe}" --tray'


def is_enabled() -> bool:
    """Kiểm tra registry xem app có được cấu hình khởi động cùng Windows không."""
    if not is_supported():
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_SUBKEY, 0, winreg.KEY_READ) as key:
            val, _ = winreg.QueryValueEx(key, REG_NAME)
            return bool(val and ("--tray" in str(val) or Path(sys.argv[0]).stem in str(val)))
    except (FileNotFoundError, OSError):
        return False


def set_enabled(on: bool) -> tuple[bool, str | None]:
    """Bật/tắt khởi động cùng Windows trong HKCU.

    Returns: (thành công, thông báo lỗi nếu có).
    """
    if not is_supported():
        return False, "Chỉ khả dụng trên bản build exe (.exe)"

    try:
        if on:
            cmd = _get_target_cmd()
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_SUBKEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, REG_NAME, 0, winreg.REG_SZ, cmd)
            return True, None
        else:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_SUBKEY, 0, winreg.KEY_SET_VALUE) as key:
                try:
                    winreg.DeleteValue(key, REG_NAME)
                except FileNotFoundError:
                    pass
            return True, None
    except OSError as e:
        return False, f"Lỗi truy cập Registry: {e}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_autostart.py -v`
Expected: PASS (3 tests passed)

---

### Task 2: Cập nhật `app.py` hỗ trợ đối số `--tray`

**Files:**
- Modify: `app.py:57-75`, `app.py:230-245`
- Test: `tests/test_app_launcher.py`

**Interfaces:**
- Consumes: `tray.hide_console()`, `tray.available()`

- [ ] **Step 1: Write failing test in `tests/test_app_launcher.py`**

Thêm test vào cuối `tests/test_app_launcher.py`:

```python
def test_parse_args_supports_tray():
    import app
    args = app._parse_args(["--tray"])
    assert args.tray is True
    args_default = app._parse_args([])
    assert args_default.tray is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_app_launcher.py -k test_parse_args_supports_tray -v`
Expected: FAIL with `unrecognized arguments: --tray`

- [ ] **Step 3: Modify `app.py`**

Trong `app.py`, sửa `_parse_args()`:
```python
    parser.add_argument(
        "--tray",
        action="store_true",
        help="Khởi động ẩn trực tiếp vào khay hệ thống, không mở trình duyệt",
    )
```

Và trong `main()` của `app.py`, bỏ qua mở browser và ẩn console nếu có `--tray`:
```python
    # 4. Tự động mở browser khi server sẵn sàng (bỏ qua nếu chạy ngầm vào tray)
    if not args.tray:
        threading.Thread(
            target=_open_browser_when_ready,
            args=(url,),
            name="browser-launcher",
            daemon=True,
        ).start()
    else:
        log_boot("Khởi động ở chế độ khay hệ thống (--tray), không mở trình duyệt.")
        if tray.available():
            _hide_console()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_app_launcher.py -v`
Expected: PASS

---

### Task 3: Backend API & Web UI Toggle Auto Start

**Files:**
- Modify: `main.py:560-600`
- Modify: `templates/update.html:70-85`, `templates/update.html:295-315`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `autostart.is_enabled()`, `autostart.is_supported()`, `autostart.set_enabled()`
- Produces: `POST /settings/auto-start`

- [ ] **Step 1: Write failing test in `tests/test_web.py`**

```python
def test_update_page_renders_auto_start_toggle(web):
    """Trang /update phải render switch auto-start."""
    html = web["client"].get("/update").text
    assert 'id="auto-start-toggle"' in html
    assert 'Khởi động cùng Windows' in html


def test_settings_auto_start_toggle_endpoint(web, monkeypatch):
    """POST /settings/auto-start thay đổi trạng thái thành công."""
    import autostart
    calls = []
    monkeypatch.setattr(autostart, "set_enabled", lambda on: calls.append(on) or (True, None))
    monkeypatch.setattr(autostart, "is_supported", lambda: True)

    resp = web["client"].post("/settings/auto-start", data={"enabled": "1"}, headers={"Origin": OWN})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert resp.json()["auto_start"] is True
    assert calls == [True]

    resp = web["client"].post("/settings/auto-start", data={"enabled": "0"}, headers={"Origin": OWN})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert resp.json()["auto_start"] is False
    assert calls == [True, False]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_web.py -k "test_update_page_renders_auto_start_toggle or test_settings_auto_start_toggle_endpoint" -v`
Expected: FAIL (endpoint 404 / markup missing)

- [ ] **Step 3: Modify `main.py` and `templates/update.html`**

Trong `main.py`:
- Thêm import: `import autostart`
- Trong `update_page()` context dictionary, thêm:
  ```python
        "auto_start_enabled": autostart.is_enabled(),
        "auto_start_supported": autostart.is_supported(),
  ```
- Thêm endpoint ngay sau `/settings/auto-update`:
  ```python
  @app.post("/settings/auto-start", dependencies=CSRF)
  def set_auto_start(request: Request, enabled: Annotated[str, Form()] = "1"):
      """Bật/tắt khởi động cùng Windows (chạy ẩn vào tray)."""
      on = enabled in ("1", "true", "True")
      ok, err = autostart.set_enabled(on)
      return JSONResponse({
          "ok": ok,
          "auto_start": on if ok else autostart.is_enabled(),
          "supported": autostart.is_supported(),
          "error": err,
      })
  ```

Trong `templates/update.html`:
- Thêm toggle HTML dưới `#auto-update-toggle`:
  ```html
  <div style="margin-bottom:.8rem">
    <label class="switch">
      <input type="checkbox" id="auto-start-toggle"
             {% if auto_start_enabled %}checked{% endif %}
             {% if not auto_start_supported %}disabled{% endif %}>
      Khởi động cùng Windows (chạy ẩn khay hệ thống)
    </label>
    {% if not auto_start_supported %}
      <span class="small muted" style="margin-left:.4rem">(Chỉ hỗ trợ trên bản exe)</span>
    {% endif %}
  </div>
  ```
- Thêm JS handler:
  ```javascript
  var autoStartToggle = document.getElementById("auto-start-toggle");
  if (autoStartToggle) {
    autoStartToggle.addEventListener("change", function(){
      fetch("/settings/auto-start", {
        method: "POST", credentials: "same-origin",
        headers: {"Content-Type": "application/x-www-form-urlencoded"},
        body: "enabled=" + (autoStartToggle.checked ? "1" : "0")
      }).then(function(r){ return r.json(); })
        .then(function(j){
          if (j.ok) {
            window.toast(j.auto_start ? "Đã bật khởi động cùng Windows"
                                      : "Đã tắt khởi động cùng Windows");
          } else {
            autoStartToggle.checked = !autoStartToggle.checked;
            window.toast(j.error || "Lỗi lưu cài đặt khởi động", "bad");
          }
        })
        .catch(function(){
          autoStartToggle.checked = !autoStartToggle.checked;
          window.toast("Lỗi mạng khi lưu cài đặt", "bad");
        });
    });
  }
  ```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_web.py -k "test_update_page_renders_auto_start_toggle or test_settings_auto_start_toggle_endpoint" -v`
Expected: PASS

---

### Task 4: Nâng cấp `self_update.py` với `on_progress` callback

**Files:**
- Modify: `self_update.py:137-195`
- Test: `tests/test_self_update.py`

**Interfaces:**
- Produces: `download_and_swap(meta, current_exe=None, on_progress=None)` where `on_progress(downloaded: int, total: int)`

- [ ] **Step 1: Write failing test in `tests/test_self_update.py`**

```python
def test_download_and_swap_invokes_on_progress_callback(tmp_path, monkeypatch):
    """download_and_swap báo tiến trình tải cho callback on_progress."""
    import self_update
    import hashlib

    exe_file = tmp_path / "9router-patch.exe"
    exe_file.write_bytes(b"OLD_EXE")
    new_content = b"X" * 131072  # 128 KB (2 chunks)

    class DummyResp:
        def __init__(self):
            self._buf = new_content
            self.headers = {"Content-Length": str(len(new_content))}
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self, size=65536):
            out, self._buf = self._buf[:size], self._buf[size:]
            return out

    monkeypatch.setattr(self_update.urllib.request, "urlopen", lambda *a, **k: DummyResp())
    monkeypatch.setattr(self_update.app_paths, "is_frozen", lambda: True)

    progress_ticks = []
    def on_prog(done, total):
        progress_ticks.append((done, total))

    meta = {
        "version": "2.9.9",
        "url": "https://example.com/f.exe",
        "sha256": hashlib.sha256(new_content).hexdigest(),
    }
    res = self_update.download_and_swap(meta, current_exe=exe_file, on_progress=on_prog)
    assert res["ok"] is True
    assert len(progress_ticks) >= 2
    assert progress_ticks[-1] == (len(new_content), len(new_content))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_self_update.py -k test_download_and_swap_invokes_on_progress_callback -v`
Expected: FAIL with `unexpected keyword argument 'on_progress'`

- [ ] **Step 3: Modify `self_update.py`**

Cập nhật `download_and_swap`:
```python
def download_and_swap(meta: dict, current_exe: Path | None = None,
                      on_progress: Callable[[int, int], None] | None = None) -> dict:
```
Trong khối đọc stream:
```python
        hasher = hashlib.sha256()
        req = urllib.request.Request(
            meta["url"], headers={"User-Agent": f"9router-patcher/{version.APP_VERSION}"})
        try:
            with urllib.request.urlopen(req, timeout=30.0) as resp, open(new_file, "wb") as f:
                try:
                    total_bytes = int(resp.headers.get("Content-Length", 0))
                except (ValueError, TypeError, AttributeError):
                    total_bytes = 0
                downloaded = 0

                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    hasher.update(chunk)
                    downloaded += len(chunk)
                    if on_progress:
                        on_progress(downloaded, total_bytes)
        except Exception as e:
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_self_update.py -v`
Expected: All tests PASS.

---

### Task 5: Nâng cấp `console_ui.py` với `download_run` Progress Animation

**Files:**
- Modify: `console_ui.py:220-261`
- Test: `tests/test_console_ui.py`

**Interfaces:**
- Produces: `console_ui.download_run(label: str, downloader: Callable, log_fn=None) -> tuple[bool, str]`

- [ ] **Step 1: Write failing test in `tests/test_console_ui.py`**

```python
def test_download_run_renders_progress_non_tty(capsys, monkeypatch):
    """Môi trường non-TTY (pipe/log file): in mốc sạch 25/50/75/100%, không escape \\r."""
    import console_ui
    monkeypatch.setattr(console_ui.sys.stdout, "isatty", lambda: False)
    console_ui._VT = None

    def fake_dl(on_progress=None):
        if on_progress:
            on_progress(25, 100)
            on_progress(50, 100)
            on_progress(75, 100)
            on_progress(100, 100)
        return {"ok": True, "error": None}

    res = console_ui.download_run("Tải exe mới", fake_dl)
    assert res["ok"] is True
    out = capsys.readouterr().out
    assert "Tải exe mới" in out
    assert "50%" in out
    assert "100%" in out
    assert "\r" not in out


def test_download_run_handles_failure(capsys, monkeypatch):
    """Khi download thất bại: in ✗ đỏ và trả nguyên kết quả lỗi."""
    import console_ui
    monkeypatch.setattr(console_ui.sys.stdout, "isatty", lambda: False)
    console_ui._VT = None

    def fail_dl(on_progress=None):
        return {"ok": False, "error": "Mất kết nối server"}

    res = console_ui.download_run("Tải exe mới", fail_dl)
    assert res["ok"] is False
    out = capsys.readouterr().out
    assert "Mất kết nối server" in out
    assert "✗" in out or "x" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_console_ui.py -k "test_download_run" -v`
Expected: FAIL with `AttributeError: module 'console_ui' has no attribute 'download_run'`

- [ ] **Step 3: Implement `download_run` in `console_ui.py`**

```python
def download_run(label: str, downloader, log_fn=None) -> dict:
    """Hiển thị progress bar + spinner khi tải file (exe mới).

    `downloader` là callable(on_progress=None) -> dict ({"ok": bool, "error": str | None}).
    `on_progress(done_bytes: int, total_bytes: int)`.
    """
    import sys as _sys
    p, g = palette(), glyphs()
    live = _sys.stdout.isatty() if hasattr(_sys.stdout, "isatty") else False
    frames = (["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"] if unicode_ok()
              else ["-", "\\", "|", "/"])
    print(f"  {p.gray}{g['v']}{p.reset} {p.cyan}{label}...{p.reset}", flush=True)

    i = [0]
    last_milestone = [-1]

    def on_prog(done: int, total: int) -> None:
        i[0] += 1
        done_mb = done / (1024 * 1024)
        if total > 0:
            pct = int((done / total) * 100)
            total_mb = total / (1024 * 1024)
            bar_len = 16
            filled = int(bar_len * done / total)
            bar = "=" * filled + (">" if filled < bar_len else "") + " " * max(0, bar_len - filled - 1)

            if not live:
                ms = pct // 25
                if ms > last_milestone[0]:
                    last_milestone[0] = ms
                    print(f"  {p.gray}{g['v']}{p.reset} {p.gray}Đã tải {pct}% ({done_mb:.1f}/{total_mb:.1f} MB){p.reset}", flush=True)
                return

            frame = frames[i[0] % len(frames)]
            line = f"\r  {p.gray}{g['v']}{p.reset} {p.cyan}{frame}{p.reset} [{bar}] {pct}% ({done_mb:.1f}/{total_mb:.1f} MB)"
        else:
            if not live:
                return
            frame = frames[i[0] % len(frames)]
            line = f"\r  {p.gray}{g['v']}{p.reset} {p.cyan}{frame}{p.reset} Đang tải ({done_mb:.1f} MB)..."

        _sys.stdout.write(line.ljust(width() - 2))
        _sys.stdout.flush()

    res = downloader(on_progress=on_prog)
    if live:
        _sys.stdout.write("\r" + " " * (width() - 2) + "\r")

    ok = bool(res.get("ok"))
    err = res.get("error")
    if not ok:
        if log_fn:
            log_fn(f"ERROR: {label} thất bại: {err}")
        print(f"  {p.gray}{g['v']}{p.reset} {p.red}{g['cross']}{p.reset} {err or 'Tải file thất bại'}", flush=True)
    else:
        print(f"  {p.gray}{g['v']}{p.reset} {p.green}{g['check']}{p.reset} {label} hoàn tất", flush=True)

    return res
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_console_ui.py -v`
Expected: PASS

---

### Task 6: Tích hợp `download_run` vào Bước [1/5] của `boot_doctor.py`

**Files:**
- Modify: `boot_doctor.py:305-325`
- Test: `tests/test_boot_doctor.py`

**Interfaces:**
- Consumes: `console_ui.download_run()`, `self_update.download_and_swap(meta, on_progress=...)`

- [ ] **Step 1: Update test in `tests/test_boot_doctor.py`**

Kiểm tra xem `test_boot_check_update_step_reports_results` và `test_boot_restarts_after_successful_self_update` hiện tại chạy như thế nào và thêm assertions xác nhận `download_run` được invoke.

```python
def test_boot_doctor_invokes_download_run_when_update_available(monkeypatch, capsys):
    """Khi có bản update, boot_doctor phải chạy qua console_ui.download_run."""
    import boot_doctor
    import self_update
    import console_ui

    called = []
    def fake_download_run(label, fn, log_fn=None):
        called.append(label)
        return fn(on_progress=None)

    monkeypatch.setattr(console_ui, "download_run", fake_download_run)
    monkeypatch.setattr(boot_doctor, "check_node", lambda: (True, "ok"))
    monkeypatch.setattr(self_update, "check_update",
                        lambda url=None: {"version": "3.0.0", "has_update": True,
                                          "url": "https://x/y.exe", "sha256": ""})
    monkeypatch.setattr(self_update, "download_and_swap",
                        lambda meta, on_progress=None: {"ok": True, "error": None})
    monkeypatch.setattr(self_update, "restart_self", lambda: (_ for _ in ()).throw(SystemExit(0)))

    with pytest.raises(SystemExit):
        boot_doctor.run_doctor(interactive=False)

    assert any("3.0.0" in c for c in called)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_boot_doctor.py -k test_boot_doctor_invokes_download_run_when_update_available -v`
Expected: FAIL

- [ ] **Step 3: Modify `boot_doctor.py`**

Tại Bước [1/5] trong `boot_doctor.py`:
```python
        else:
            res = console_ui.download_run(
                f"Tải bản cập nhật v{meta['version']}",
                lambda on_progress=None: self_update.download_and_swap(meta, on_progress=on_progress),
                log_boot,
            )
            if res["ok"]:
                console_ui.step_end(f"Đã cập nhật v{meta['version']}", "info", "khởi động lại...")
                self_update.restart_self()
            else:
                console_ui.step_end("Cảnh báo", "warn", (res.get("error") or "")[:60])
```

- [ ] **Step 4: Run full test suites**

Run: `pytest tests/test_boot_doctor.py tests/test_console_ui.py tests/test_self_update.py tests/test_autostart.py tests/test_web.py -q`
Expected: All PASS.

---

### Task 7: Full Regression & Documentation Check

**Files:**
- Modify: `README.md` (cập nhật số lượng tính năng / tests)
- Run: Full test suite

- [ ] **Step 1: Chạy toàn bộ test suite dự án**

Run: `pytest tests/ -q`
Expected: ~390+ passed, 5 skipped (hoàn toàn xanh).

- [ ] **Step 2: Cập nhật README.md**
Cập nhật số test mới nhất và ghi nhận 2 tính năng:
- Auto Start cùng Windows (HKCU Run + cờ `--tray`).
- Console download progress animation (% / MB) cho self-update lúc boot.
