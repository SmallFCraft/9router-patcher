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
