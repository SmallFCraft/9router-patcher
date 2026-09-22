"""Self-update engine for 9router Patch Manager standalone executable."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.request
from pathlib import Path

import app_paths
import config
import version

_SWAP_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()
_STATE: dict = {
    "checked_at": 0.0,
    "remote_version": "",
    "has_update": False,
    "phase": "idle",            # idle | checking | downloading | ready | error | dev-mode
    "error": None,
    "applied_version": None,
    "changelog": "",
}


def parse_version(v: str) -> tuple[int, ...]:
    """Chuyển chuỗi version thành tuple 4 số nguyên để so sánh chính xác."""
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


def get_settings_file() -> Path:
    """Đường dẫn file settings.json trong %APPDATA%."""
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


def check_update(url: str | None = None) -> dict | None:
    """Gửi HTTP GET kiểm tra version.json từ hosting, timeout 5s. Thất bại trả None."""
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


def state() -> dict:
    """Snapshot trạng thái self-update, an toàn đa luồng."""
    with _STATE_LOCK:
        return dict(_STATE)


def _set_state(**kwargs) -> None:
    with _STATE_LOCK:
        _STATE.update(kwargs)


def cleanup_old_files(exe_dir: Path | None = None) -> int:
    """Xóa file .old-* và .new còn sót lúc khởi động. PermissionError bị bỏ qua
    (file cũ của tiến trình khác đang chạy) — lần khởi động sau dọn tiếp."""
    if exe_dir is None:
        if not app_paths.is_frozen():
            return 0
        exe_dir = Path(sys.argv[0]).resolve().parent

    count = 0
    for pattern in ("9router-patch.old-*", "9router-patch.new"):
        for f in exe_dir.glob(pattern):
            try:
                f.unlink()
                count += 1
            except (OSError, PermissionError):
                pass
    return count


def download_and_swap(meta: dict, current_exe: Path | None = None) -> dict:
    """Tải exe mới, xác thực SHA256 rồi hoán đổi nguyên tử vào exe hiện tại.

    Windows cho phép os.replace() trên file exe đang chạy (đã đo trực tiếp) —
    không cần batch script, không cần khởi động lại, không cần pending-flag.
    """
    if not _SWAP_LOCK.acquire(blocking=False):
        return {"ok": False, "error": "Cập nhật đang diễn ra"}

    try:
        if not app_paths.is_frozen() and current_exe is None:
            _set_state(phase="dev-mode", error="Chạy từ source .py — bỏ qua hoán đổi exe.")
            return {"ok": False, "error": "dev-mode"}

        exe = current_exe or Path(sys.argv[0]).resolve()
        new_file = exe.parent / "9router-patch.new"

        _set_state(phase="downloading", error=None,
                   remote_version=meta.get("version", ""))
        hasher = hashlib.sha256()
        req = urllib.request.Request(
            meta["url"], headers={"User-Agent": f"9router-patcher/{version.APP_VERSION}"})
        try:
            with urllib.request.urlopen(req, timeout=30.0) as resp, open(new_file, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    hasher.update(chunk)
        except Exception as e:
            new_file.unlink(missing_ok=True)
            _set_state(phase="error", error=f"Lỗi tải file: {e}")
            return {"ok": False, "error": str(e)}

        actual = hasher.hexdigest().lower()
        expected = (meta.get("sha256") or "").strip().lower()
        if expected and actual != expected:
            new_file.unlink(missing_ok=True)
            err = f"SHA256 mismatch: mong muốn {expected[:8]}…, nhận {actual[:8]}…"
            _set_state(phase="error", error=err)
            return {"ok": False, "error": err}

        # Tên .old có timestamp: tiến trình đang chạy vẫn giữ handle trên file cũ,
        # đổi tên cố định sẽ đè nhau ở lần cập nhật sau trong cùng phiên.
        old_file = exe.parent / f"9router-patch.old-{int(time.time())}"
        try:
            os.replace(exe, old_file)
            os.replace(new_file, exe)
        except Exception as e:
            if old_file.exists() and not exe.exists():
                try:
                    os.replace(old_file, exe)
                except Exception:
                    pass
            _set_state(phase="error", error=f"Lỗi hoán đổi exe: {e}")
            return {"ok": False, "error": str(e)}

        _set_state(phase="ready", applied_version=meta["version"],
                   has_update=False, error=None)
        return {"ok": True, "error": None}
    finally:
        _SWAP_LOCK.release()


def _check_once() -> None:
    """Một lượt kiểm tra + tải. Tách riêng để boot gọi ngay lượt đầu, worker tái dùng."""
    try:
        if is_enabled():
            _set_state(phase="checking")
            meta = check_update()
            now = time.time()
            if meta:
                if meta["has_update"]:
                    _set_state(checked_at=now, remote_version=meta["version"],
                               has_update=True, changelog=meta.get("changelog", ""))
                    download_and_swap(meta)
                else:
                    _set_state(checked_at=now, remote_version=meta["version"],
                               has_update=False, phase="idle", error=None,
                               changelog=meta.get("changelog", ""))
            else:
                _set_state(checked_at=now, phase="idle")
        # Tắt switch: giữ phase cũ, không reset về idle — tránh nhấp nháy badge trên UI.
    except Exception as e:
        _set_state(phase="error", error=str(e))


def run_worker(stop_event: threading.Event, interval: float | None = None) -> None:
    """Thread daemon: check ngay lượt đầu lúc boot, sau đó mỗi `interval` giây.

    Mọi exception bắt tại chỗ, ghi state, tick sau thử lại — thread không bao giờ chết.
    """
    sleep_time = interval if interval is not None else config.AUTO_UPDATE_INTERVAL_SECONDS
    _check_once()                       # lượt đầu ngay khi khởi động, không chờ đủ 5 phút
    while not stop_event.wait(sleep_time):
        _check_once()

