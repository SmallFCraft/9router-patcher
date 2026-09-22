# Nuitka Onefile App (dashboard-only) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Đóng gói 9router Patch Manager thành 1 file `.exe` duy nhất (Nuitka onefile, dashboard-only), tự động mở browser, nhúng cứng `patches.toml` dạng ciphertext AES-GCM (giải mã trong RAM, không ghi đĩa), tương thích đầy đủ với pipeline update và port lifecycle trên Windows.

**Architecture:** Nuitka biên dịch toàn bộ code Python (`app.py`, `main.py`, `engine.py`, `updater.py`) sang mã máy C với MSVC; nhúng thư mục `templates/` và file mã hóa `assets/patches.enc`. Đường dẫn được phân tầng: read-only resolve cạnh `__file__` (trong temp dir một file); writable app data (`logs/`, `router-stack.json`, `update-history.jsonl`) resolve vào `%APPDATA%/9router-patch/` khi frozen; backups và target npm resolve ngoài exe. `app.py` là entry point mở browser sau khi uvicorn khởi động.

**Tech Stack:** Python 3.14.3, Nuitka 4.2.1, MSVC 14.51 (`cl.exe`), `cryptography` (AES-256-GCM), FastAPI 0.136, Uvicorn 0.46, Jinja2 3.1, pytest 9.0.

**Spec:** [docs/superpowers/specs/2026-09-20-nuitka-onefile-app-design.md](../../specs/2026-09-20-nuitka-onefile-app-design.md)

## Global Constraints

- **KHÔNG thêm CLI trong exe** — user đã chốt dashboard-only (§1). `engine.py` giữ CLI cho dev qua `python -m engine`, nhưng bản `.exe` chỉ chạy dashboard.
- **KHÔNG ghi `patches.toml` plaintext ra đĩa** — runtime chỉ giải mã `patches.enc` trong RAM qua `AESGCM.decrypt`, parse bằng `tomllib.loads` (§3.2).
- **Frozen detection dùng `"__compiled__" in globals()`** — Nuitka onefile đặt `sys.frozen = None` (đã chứng minh bằng spike S2). Tuyệt đối không dùng `getattr(sys, "frozen", False)`.
- **Writables không ghi vào `%TEMP%\onefile_*`** — thư mục temp bị xóa khi process kết thúc. Mọi file ghi (`logs/`, `update-history.jsonl`, `router-stack.json`) phải vào `%APPDATA%/9router-patch/` khi frozen (§3.3).
- **Backups (`BACKUP_ROOT`) nằm cạnh file `.exe` thật** — dùng `Path(sys.argv[0]).resolve().parent.parent / "9router-backups"` khi frozen (§3.3), giữ nguyên `HERE.parent / "9router-backups"` khi dev.
- **Không hồi quy 229 tests hiện tại** — `python -m pytest tests/ -q` phải tiếp tục pass 229, skip 1 sau mỗi task.
- **Windows-specific flags** — Nuitka build dùng `--windows-console-mode=attach` (chạy từ terminal hiện log, double-click không hiện cửa sổ đen), `--assume-yes-for-downloads`, `--output-filename=9router-patch.exe`.

---

### Task 1: AES-GCM Encrypted Patches Loader & Tool

**Files:**
- Create: `tools/make_patches_blob.py`
- Modify: `engine.py:18-78`
- Test: `tests/test_engine_enc.py`

**Interfaces:**
- Consumes: `patches.toml` (source of truth, 31 patches), `cryptography.hazmat.primitives.ciphers.aead.AESGCM`.
- Produces:
  - `tools/make_patches_blob.py` CLI: `python tools/make_patches_blob.py [--toml PATH] [--out PATH]`
  - `assets/patches.enc` (41,418 bytes binary: 12-byte nonce + AES-256-GCM ciphertext)
  - `engine.PATCHES_ENC_FILE: Path`
  - `engine._BLOB_KEY: bytes` (hardcoded 32-byte constant)
  - `engine.load_patches(path=None)` fallback logic: nếu `path` không truyền và `PATCHES_ENC_FILE.is_file()` → giải mã trong RAM; ngược lại đọc `PATCHES_FILE` plaintext như cũ.

- [ ] **Step 1: Write failing test for encrypted patches roundtrip and precedence**

Tạo `tests/test_engine_enc.py`:
```python
"""Tests for AES-GCM encrypted patches blob loading and generation."""
import os
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import pytest

import engine


def test_blob_roundtrip_decrypts_to_valid_patches(tmp_path, monkeypatch):
    """Tool-created blob is decrypted in RAM and produces identical Patch objects."""
    key = engine._BLOB_KEY
    nonce = os.urandom(12)
    fake_toml = (
        '[[patch]]\n'
        'id = "test-enc-patch"\n'
        'order = 1\n'
        'summary = "encrypted test"\n'
        'why = "unit test"\n'
        'find = "FIND_ME"\n'
        'replace = "REPLACED"\n'
    ).encode("utf-8")
    ciphertext = AESGCM(key).encrypt(nonce, fake_toml, None)
    enc_file = tmp_path / "patches.enc"
    enc_file.write_bytes(nonce + ciphertext)

    monkeypatch.setattr(engine, "PATCHES_ENC_FILE", enc_file)
    patches = engine.load_patches()
    assert len(patches) == 1
    assert patches[0].id == "test-enc-patch"
    assert patches[0].find == "FIND_ME"
    assert patches[0].replace == "REPLACED"


def test_load_patches_explicit_path_overrides_enc(tmp_path, monkeypatch):
    """Passing path explicitly must read that file as plaintext TOML, ignoring .enc."""
    enc_file = tmp_path / "patches.enc"
    enc_file.write_bytes(b"corrupt-data-not-a-valid-blob")
    monkeypatch.setattr(engine, "PATCHES_ENC_FILE", enc_file)

    plain = tmp_path / "custom.toml"
    plain.write_text(
        '[[patch]]\n'
        'id = "plain-patch"\n'
        'order = 1\n'
        'summary = "s"\nwhy = "w"\nfind = "f"\nreplace = "r"\n',
        encoding="utf-8",
    )
    patches = engine.load_patches(plain)
    assert len(patches) == 1
    assert patches[0].id == "plain-patch"


def test_load_patches_falls_back_to_toml_when_no_enc(tmp_path, monkeypatch):
    """When patches.enc does not exist, load_patches reads patches.toml normally."""
    missing = tmp_path / "nonexistent.enc"
    monkeypatch.setattr(engine, "PATCHES_ENC_FILE", missing)
    patches = engine.load_patches()
    assert len(patches) == 31
    assert patches[0].id == "connect-timeout-180s"


def test_make_patches_blob_cli(tmp_path):
    """tools/make_patches_blob.py CLI encrypts source toml to output file."""
    import subprocess
    import sys
    out = tmp_path / "out.enc"
    cmd = [sys.executable, "tools/make_patches_blob.py", "--out", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert out.is_file()
    assert out.stat().st_size > 40000
    # Decrypt with engine key
    blob = out.read_bytes()
    nonce, ct = blob[:12], blob[12:]
    plain = AESGCM(engine._BLOB_KEY).decrypt(nonce, ct, None).decode("utf-8")
    assert "connect-timeout-180s" in plain
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_engine_enc.py -v`
Expected: FAIL with `AttributeError: module 'engine' has no attribute '_BLOB_KEY'` or similar.

- [ ] **Step 3: Implement `tools/make_patches_blob.py` and modify `engine.py`**

Create `tools/make_patches_blob.py`:
```python
"""Mã hóa patches.toml thành assets/patches.enc bằng AES-256-GCM.

Chạy trước khi build Nuitka exe để nhúng patches dưới dạng ciphertext.
Key cố định trùng với engine._BLOB_KEY.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import engine


def encrypt_toml(src: Path, dest: Path, key: bytes = engine._BLOB_KEY) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    raw = src.read_bytes()
    nonce = os.urandom(12)
    blob = nonce + AESGCM(key).encrypt(nonce, raw, None)
    dest.write_bytes(blob)
    return len(blob)


def main() -> int:
    ap = argparse.ArgumentParser(description="Encrypt patches.toml to patches.enc")
    ap.add_argument("--toml", default=str(ROOT / "patches.toml"), help="Source TOML file")
    ap.add_argument("--out", default=str(ROOT / "assets" / "patches.enc"), help="Destination .enc file")
    args = ap.parse_args()

    src = Path(args.toml)
    dest = Path(args.out)
    if not src.is_file():
        sys.stderr.write(f"Error: {src} not found\n")
        return 1
    size = encrypt_toml(src, dest)
    print(f"Encrypted {src.name} ({src.stat().st_size} bytes) -> {dest} ({size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Modify `engine.py`:
Thêm import và hằng số ở đầu file:
```python
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# Key AES-256 cố định để giải mã patches.enc trong RAM khi chạy bản build binary.
# ponytail: key tĩnh trong C code compiled đủ để chống sửa lậu / chống đọc bằng Notepad;
# nâng cấp thành asymmetric hoặc remote key nếu cần DRM thực sự.
_BLOB_KEY = bytes.fromhex("9bb6864c583dda3b09ae2001e9eb955a69c37698bb3028e562ff64e8638cdef6")
PATCHES_ENC_FILE = HERE / "patches.enc"
```

Sửa hàm `load_patches` trong `engine.py`:
```python
def load_patches(path: str | Path | None = None) -> list[Patch]:
    """Tải danh sách patches.

    Thứ tự ưu tiên:
    1. Nếu truyền `path` cụ thể -> luôn đọc plaintext TOML từ file đó.
    2. Nếu không truyền `path` và tồn tại `PATCHES_ENC_FILE` -> giải mã AES-GCM trong RAM.
    3. Ngược lại -> đọc `PATCHES_FILE` (patches.toml) như cũ.
    """
    if path is not None:
        raw_text = Path(path).read_text(encoding="utf-8")
    elif PATCHES_ENC_FILE.is_file():
        blob = PATCHES_ENC_FILE.read_bytes()
        nonce, ciphertext = blob[:12], blob[12:]
        raw_text = AESGCM(_BLOB_KEY).decrypt(nonce, ciphertext, None).decode("utf-8")
    else:
        raw_text = PATCHES_FILE.read_text(encoding="utf-8")

    raw = toml_loads(raw_text)
    patches = [
        Patch(
            id=d["id"],
            order=d["order"],
            group=d.get("group", d["id"]),   # standalone patch is its own group
            summary=d["summary"],
            why=d["why"],
            find=d["find"],
            replace=d["replace"],
            defines=d.get("defines"),
        )
        for d in raw["patch"]
    ]
    return sorted(patches, key=lambda p: p.order)
```

Tạo file blob đầu tiên:
`python tools/make_patches_blob.py` -> sinh `assets/patches.enc`.

Thêm vào `.gitignore`:
```
assets/patches.enc
build/
dist/
*.exe
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_engine_enc.py -v`
Expected: 4 passed.
Run: `python -m pytest tests/ -q`
Expected: 233 passed, 1 skipped.

- [ ] **Step 5: Commit**

```bash
git add engine.py tools/make_patches_blob.py tests/test_engine_enc.py .gitignore
git commit -m "feat(engine): add AES-GCM encrypted patches loader and generator tool"
```

---

### Task 2: Runtime Path Abstraction for Frozen Executable

**Files:**
- Create: `app_paths.py`
- Modify: `main.py:34-58,349-354`, `engine.py:20-29`, `updater.py:47-51,349,388-393`
- Test: `tests/test_app_paths.py`

**Interfaces:**
- Consumes: `"__compiled__" in globals()`, `os.environ["APPDATA"]`, `sys.argv[0]`.
- Produces: `app_paths.py` module:
  - `is_frozen() -> bool`: trả về True nếu chạy dưới Nuitka binary (`"__compiled__" in globals()`).
  - `get_app_data_dir() -> Path`: trả về `%APPDATA%/9router-patch/` (tự động `mkdir(parents=True, exist_ok=True)`).
  - `get_bundle_dir() -> Path`: trả về thư mục chứa mã chạy (`__file__.parent`, là `%TEMP%\onefile_*` khi frozen, root repo khi dev).
  - `get_backup_root() -> Path`: khi frozen trả về `Path(sys.argv[0]).resolve().parent.parent / "9router-backups"`; khi dev trả về `bundle_dir.parent / "9router-backups"`.
  - `get_log_dir() -> Path`: khi frozen trả về `get_app_data_dir() / "logs"`; khi dev trả về `bundle_dir / "logs"`.
  - `get_history_file() -> Path`: `get_log_dir() / "update-history.jsonl"`.
  - `get_stack_state_file() -> Path`: `get_log_dir() / "router-stack.json"`.

- [ ] **Step 1: Write failing test for path resolution**

Tạo `tests/test_app_paths.py`:
```python
"""Tests for app_paths resolution across dev and frozen environments."""
import os
import sys
from pathlib import Path
import pytest

import app_paths


def test_is_frozen_false_in_normal_pytest():
    """Pytest runs under plain python interpreter, not compiled C binary."""
    assert not app_paths.is_frozen()


def test_dev_paths_resolve_inside_repo(monkeypatch):
    """In dev mode, logs and backups resolve relative to repo tree."""
    monkeypatch.setattr(app_paths, "is_frozen", lambda: False)
    bundle = app_paths.get_bundle_dir()
    assert (bundle / "main.py").is_file()
    assert app_paths.get_log_dir() == bundle / "logs"
    assert app_paths.get_backup_root() == bundle.parent / "9router-backups"
    assert app_paths.get_history_file() == bundle / "logs" / "update-history.jsonl"
    assert app_paths.get_stack_state_file() == bundle / "logs" / "router-stack.json"


def test_frozen_paths_resolve_to_appdata_and_exe_dir(tmp_path, monkeypatch):
    """In frozen mode, writables go to APPDATA and backups sit alongside the EXE."""
    monkeypatch.setattr(app_paths, "is_frozen", lambda: True)
    fake_appdata = tmp_path / "AppData"
    monkeypatch.setenv("APPDATA", str(fake_appdata))
    
    # Fake sys.argv[0] as E:\Apps\9router\9router-patch.exe
    fake_exe = tmp_path / "bin" / "9router-patch.exe"
    fake_exe.parent.mkdir(parents=True)
    fake_exe.touch()
    monkeypatch.setattr(sys, "argv", [str(fake_exe)])

    app_data = app_paths.get_app_data_dir()
    assert app_data == fake_appdata / "9router-patch"
    assert app_data.is_dir()

    log_dir = app_paths.get_log_dir()
    assert log_dir == app_data / "logs"
    assert log_dir.is_dir()

    assert app_paths.get_history_file() == log_dir / "update-history.jsonl"
    assert app_paths.get_stack_state_file() == log_dir / "router-stack.json"

    # Backups outside the exe's folder: fake_exe.parent is 'bin', parent.parent is tmp_path
    assert app_paths.get_backup_root() == tmp_path / "9router-backups"


def test_headroom_cmd_locates_shim_on_path(monkeypatch):
    """_default_headroom_cmd finds headroom.exe from PATH if sys.executable is temp python."""
    import updater
    monkeypatch.setattr(updater.shutil, "which", lambda cmd: r"C:\Scripts\headroom.exe" if "headroom" in cmd else None)
    cmd = updater._default_headroom_cmd()
    assert cmd is not None
    assert "C:\\Scripts\\headroom.exe" in cmd
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_app_paths.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app_paths'`.

- [ ] **Step 3: Implement `app_paths.py` and hook into codebase**

Create `app_paths.py`:
```python
"""Centralized path resolution for 9router Patch Manager.

Handles differences between running from source (.py) and running as a Nuitka
onefile compiled executable (.exe). Read-only data files resolve from bundle dir
(temp dir in onefile); writable state resolves to %APPDATA%/9router-patch/ in frozen mode.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_BUNDLE_DIR = Path(__file__).resolve().parent


def is_frozen() -> bool:
    """True when running inside Nuitka compiled binary."""
    # Nuitka injects __compiled__ into module globals; sys.frozen is None
    return "__compiled__" in globals()


def get_bundle_dir() -> Path:
    """Directory containing code and embedded read-only assets (templates, patches.enc)."""
    return _BUNDLE_DIR


def get_app_data_dir() -> Path:
    """User-writable state directory in %APPDATA%."""
    base = Path(os.environ.get("APPDATA") or Path.home())
    app_data = base / "9router-patch"
    app_data.mkdir(parents=True, exist_ok=True)
    return app_data


def get_log_dir() -> Path:
    """Directory for restart logs and history."""
    if is_frozen():
        d = get_app_data_dir() / "logs"
    else:
        d = get_bundle_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_history_file() -> Path:
    return get_log_dir() / "update-history.jsonl"


def get_stack_state_file() -> Path:
    return get_log_dir() / "router-stack.json"


def get_backup_root() -> Path:
    """Backups directory. Sits outside the repo / outside the exe directory."""
    if is_frozen():
        # sys.argv[0] is the true path to 9router-patch.exe on disk
        exe_dir = Path(sys.argv[0]).resolve().parent
        return exe_dir.parent / "9router-backups"
    return get_bundle_dir().parent / "9router-backups"
```

Modify `engine.py`:
Thay thế khởi tạo `BACKUP_ROOT` bằng hàm `app_paths`:
```python
import app_paths

HERE = Path(__file__).resolve().parent
PATCHES_FILE = HERE / "patches.toml"
PATCHES_ENC_FILE = HERE / "patches.enc"
BACKUP_ROOT = app_paths.get_backup_root()
```
Trong hàm `snapshot()`, `BACKUP_ROOT = app_paths.get_backup_root()` được gọi động để cập nhật nếu test monkeypatch.

Modify `updater.py`:
```python
import app_paths

RESTART_LOG_DIR = app_paths.get_log_dir()
STACK_STATE_FILE = app_paths.get_stack_state_file()
```
Sửa `_default_headroom_cmd()`:
```python
def _default_headroom_cmd() -> str | None:
    # 1. Thử tìm shim trên PATH trước (hoạt động tốt cả khi frozen lẫn venv)
    which_hr = shutil.which("headroom.exe") or shutil.which("headroom")
    if which_hr:
        return f'"{which_hr}" proxy --port {HEADROOM_PORT} --code-aware'
    # 2. Fallback tìm cạnh sys.executable
    exe = Path(sys.executable).parent / "Scripts" / "headroom.exe"
    if exe.is_file():
        return f'"{sys.executable}" "{exe}" proxy --port {HEADROOM_PORT} --code-aware'
    return None
```

Modify `main.py`:
```python
import app_paths

HISTORY_FILE = app_paths.get_history_file()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_app_paths.py -v`
Expected: 4 passed.
Run: `python -m pytest tests/ -q`
Expected: 237 passed, 1 skipped.

- [ ] **Step 5: Commit**

```bash
git add app_paths.py engine.py updater.py main.py tests/test_app_paths.py
git commit -m "feat(paths): abstract bundle vs appdata paths for frozen executable"
```

---

### Task 3: App Entry Point & Refactor Log Config

**Files:**
- Create: `app.py`
- Modify: `main.py:728-766`
- Test: `tests/test_app_launcher.py`

**Interfaces:**
- Consumes: `main.app`, `main.HOST`, `main.PORT`, `main.get_log_config()`.
- Produces:
  - `main.get_log_config() -> dict`: chuyển `_log_config()` ra module-level để reuse.
  - `app.py`: entry point chính thức cho Nuitka build, tự động mở trình duyệt `http://127.0.0.1:20129` sau khi uvicorn sẵn sàng.

- [ ] **Step 1: Write failing test for log config and launcher**

Tạo `tests/test_app_launcher.py`:
```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_app_launcher.py -v`
Expected: FAIL with `AttributeError: module 'main' has no attribute 'get_log_config'`.

- [ ] **Step 3: Refactor `main.py` and create `app.py`**

Modify `main.py`:
Di chuyển hàm logging config ra ngoài `if __name__ == "__main__":` và đổi tên thành `get_log_config()`:
```python
def get_log_config() -> dict:
    """Access/default logs with timestamps — the console shows WHEN each request ran.
    asctime already carries ',<ms>'; appending %(msecs)03d printed the same ms twice."""
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "()": "uvicorn.logging.DefaultFormatter",
                "fmt": "%(asctime)s %(levelprefix)s %(message)s",
                "datefmt": "%d-%m-%Y %H:%M:%S",
            },
            "access": {
                "()": "uvicorn.logging.AccessFormatter",
                "fmt": '%(asctime)s %(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
                "datefmt": "%d-%m-%Y %H:%M:%S",
            },
        },
        "handlers": {
            "default": {"class": "logging.StreamHandler", "formatter": "default",
                        "stream": "ext://sys.stdout"},
            "access": {"class": "logging.StreamHandler", "formatter": "access",
                       "stream": "ext://sys.stdout"},
        },
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"level": "INFO"},
            "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
        },
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, log_config=get_log_config())
```

Create `app.py`:
```python
"""9router Patch Manager Desktop Application Launcher.

Target entry point cho Nuitka onefile executable.
Khởi động uvicorn server trên 127.0.0.1:20129 và tự động mở trình duyệt web.
"""
from __future__ import annotations

import threading
import time
import urllib.request
import webbrowser

import uvicorn
from main import HOST, PORT, app, get_log_config


def _open_browser_when_ready(url: str, timeout: float = 10.0) -> None:
    """Thử kết nối đến dashboard, khi server phản hồi thì mở default browser."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            with urllib.request.urlopen(url, timeout=0.5):
                webbrowser.open(url)
                return
        except Exception:
            time.sleep(0.2)
    # Nếu timeout vẫn thử mở
    webbrowser.open(url)


def main() -> None:
    url = f"http://{HOST}:{PORT}"
    # Mở browser trên luồng riêng sau khi uvicorn lắng nghe port
    threading.Thread(
        target=_open_browser_when_ready,
        args=(url,),
        name="browser-launcher",
        daemon=True,
    ).start()

    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        log_config=get_log_config(),
    )


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_app_launcher.py -v`
Expected: 2 passed.
Run: `python -m pytest tests/ -q`
Expected: 239 passed, 1 skipped.

- [ ] **Step 5: Commit**

```bash
git add main.py app.py tests/test_app_launcher.py
git commit -m "feat(app): add standalone app launcher and export uvicorn log config"
```

---

### Task 4: Packaging Pipeline Script & Dependencies

**Files:**
- Create: `requirements.txt`
- Create: `build_app.py`
- Test: `tests/test_build_pipeline.py`

**Interfaces:**
- Consumes: `tools/make_patches_blob.py`, `nuitka`, `MSVC cl.exe`.
- Produces:
  - `requirements.txt`: ghim version chính xác cho build (`nuitka==4.2.1`, `fastapi==0.136.1`, `uvicorn==0.46.0`, `jinja2==3.1.6`, `cryptography>=43.0.0`).
  - `build_app.py`: automated script 2 bước:
    1. Mã hóa `patches.toml` -> `assets/patches.enc`
    2. Chạy `nuitka` đóng gói thành `dist/9router-patch.exe`

- [ ] **Step 1: Write failing test for build pipeline validation**

Tạo `tests/test_build_pipeline.py`:
```python
"""Tests for build pipeline command assembly and pre-flight checks."""
from pathlib import Path
import pytest

import build_app


def test_requirements_file_exists_and_contains_deps():
    req = Path("requirements.txt")
    assert req.is_file()
    text = req.read_text(encoding="utf-8")
    for dep in ("nuitka", "fastapi", "uvicorn", "jinja2", "cryptography"):
        assert dep in text.lower()


def test_build_app_command_assembly():
    """build_app generates correct Nuitka arguments without syntax errors."""
    cmd = build_app.get_nuitka_cmd(output_dir=Path("dist"))
    cmd_str = " ".join(cmd)
    assert "--onefile" in cmd
    assert "--windows-console-mode=attach" in cmd
    assert "--assume-yes-for-downloads" in cmd
    assert any("templates=templates" in arg for arg in cmd)
    assert any("patches.enc" in arg for arg in cmd)
    assert cmd[-1] == "app.py"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_build_pipeline.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'build_app'`.

- [ ] **Step 3: Implement `requirements.txt` and `build_app.py`**

Create `requirements.txt`:
```text
fastapi>=0.136.0
uvicorn>=0.46.0
jinja2>=3.1.6
cryptography>=43.0.0
nuitka==4.2.1
```

Create `build_app.py`:
```python
"""Automated build script for 9router Patch Manager binary executable.

Quy trình:
1. Mã hóa patches.toml -> assets/patches.enc
2. Chạy Nuitka biên dịch app.py thành dist/9router-patch.exe
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def get_nuitka_cmd(output_dir: Path) -> list[str]:
    enc_source = ROOT / "assets" / "patches.enc"
    tpl_source = ROOT / "templates"
    return [
        sys.executable,
        "-m",
        "nuitka",
        "--onefile",
        "--assume-yes-for-downloads",
        "--windows-console-mode=attach",
        f"--include-data-dir={tpl_source}=templates",
        f"--include-data-files={enc_source}=patches.enc",
        f"--output-dir={output_dir}",
        "--output-filename=9router-patch.exe",
        "app.py",
    ]


def build() -> int:
    dist = ROOT / "dist"
    dist.mkdir(parents=True, exist_ok=True)

    print("== Step 1: Encrypting patches.toml ==")
    enc_script = ROOT / "tools" / "make_patches_blob.py"
    r = subprocess.run([sys.executable, str(enc_script)])
    if r.returncode != 0:
        print("Error: Encrypting patches failed!")
        return r.returncode

    print("\n== Step 2: Compiling with Nuitka (this may take several minutes) ==")
    cmd = get_nuitka_cmd(dist)
    print("Running:", " ".join(cmd))
    r = subprocess.run(cmd, cwd=str(ROOT))
    if r.returncode != 0:
        print(f"Error: Nuitka build failed with exit code {r.returncode}")
        return r.returncode

    exe = dist / "9router-patch.exe"
    if exe.is_file():
        size_mb = exe.stat().st_size / (1024 * 1024)
        print(f"\nSUCCESS: Built {exe} ({size_mb:.1f} MB)")
        return 0
    print(f"\nError: Expected output {exe} was not created!")
    return 1


if __name__ == "__main__":
    raise SystemExit(build())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_build_pipeline.py -v`
Expected: 2 passed.
Run: `python -m pytest tests/ -q`
Expected: 241 passed, 1 skipped.

- [ ] **Step 5: Commit**

```bash
git add requirements.txt build_app.py tests/test_build_pipeline.py
git commit -m "feat(build): add requirements.txt and automated build script"
```

---

### Task 5: End-to-End Build & Executable Black-Box Verification

**Files:**
- Test: `tests/test_e2e_binary.py`
- Doc: `README.md`

**Interfaces:**
- Consumes: `dist/9router-patch.exe` generated by `build_app.py`.
- Produces: Black-box automated verification test:
  1. Chạy `dist/9router-patch.exe` trong tiến trình nền độc lập.
  2. Gửi request HTTP `GET http://127.0.0.1:20129/` -> HTTP 200, chứa token HTML `9router`.
  3. Kiểm tra `%TEMP%\onefile_*` chứa `patches.enc`, **tuyệt đối không chứa** file plaintext `patches.toml`.
  4. Kill process sạch sẽ, không rò rỉ port.

- [ ] **Step 1: Run `build_app.py` to compile real binary**

Run: `python build_app.py`
Expected: Quá trình biên dịch Nuitka hoàn tất thành công, sinh ra `dist/9router-patch.exe` (kích thước ~15-25 MB).

- [ ] **Step 2: Write black-box binary verification test**

Tạo `tests/test_e2e_binary.py`:
```python
"""Black-box verification of the compiled 9router-patch.exe binary."""
import os
import subprocess
import time
import urllib.request
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parent.parent
EXE_PATH = ROOT / "dist" / "9router-patch.exe"


@pytest.mark.skipif(not EXE_PATH.is_file(), reason="dist/9router-patch.exe not built yet")
def test_compiled_binary_boots_serves_html_and_hides_plaintext():
    """The compiled binary boots uvicorn, responds to HTTP, and keeps patches encrypted."""
    # Run binary
    proc = subprocess.Popen([str(EXE_PATH)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    url = "http://127.0.0.1:20129/"
    html = ""
    try:
        # Poll up to 15 seconds for server readiness
        for _ in range(30):
            time.sleep(0.5)
            try:
                with urllib.request.urlopen(url, timeout=1.0) as resp:
                    if resp.status == 200:
                        html = resp.read().decode("utf-8")
                        break
            except Exception:
                continue
        assert html != "", "Server did not respond with 200 within 15 seconds"
        assert "9router" in html
        assert "Quản lý bản vá & cập nhật" in html

        # Verify plaintext patches.toml is NOT unpacked to temp dir
        temp_dir = Path(os.environ.get("TEMP", r"C:\Windows\Temp"))
        onefile_dirs = list(temp_dir.glob("onefile_*"))
        # Must find at least one patches.enc and zero patches.toml in recent onefile dirs
        found_enc = False
        for od in onefile_dirs:
            if (od / "patches.enc").is_file():
                found_enc = True
                assert not (od / "patches.toml").is_file(), f"Plaintext patches.toml leaked in {od}!"
        assert found_enc, "Expected patches.enc in onefile temp directory"

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
```

- [ ] **Step 3: Run E2E test**

Run: `python -m pytest tests/test_e2e_binary.py -v`
Expected: 1 passed.

- [ ] **Step 4: Update README.md with binary distribution notes**

Thêm mục `### Distribution (Executable)` vào `README.md`:
- Hướng dẫn build: `python build_app.py` -> sinh `dist/9router-patch.exe`.
- Hướng dẫn chia sẻ cho người khác: gửi 1 file `9router-patch.exe`, người nhận chỉ cần double-click. Yêu cầu máy nhận đã cài Node + `npm i -g 9router`.
- Ghi rõ cơ chế bảo vệ: `patches.toml` được mã hóa AES-GCM trong RAM; `%APPDATA%/9router-patch/` lưu trữ logs; SmartScreen có thể cảnh báo do chưa ký cert.

- [ ] **Step 5: Run full test suite regression check**

Run: `python -m pytest tests/ -q`
Expected: 242 passed, 1 skipped.

- [ ] **Step 6: Commit**

```bash
git add tests/test_e2e_binary.py README.md
git commit -m "feat(app): verify e2e binary execution and document app distribution"
```
