"""Windows autostart cho 9router Patch Manager — Startup Folder + VBScript ẩn.

Vì sao dùng Startup Folder (shell:startup) + VBScript:
1. `schtasks /sc onlogon`: ĐÒI ADMIN (Access is denied trên Windows 11 với user thường),
   app chạy không elevate nên không thể dùng Task Scheduler qua web UI.
2. `HKCU\\...\\Run` (cũ): exe biên dịch console (`--windows-console-mode=force`) khiến
   Explorer cấp cửa sổ console TRƯỚC khi Python chạy một byte; `tray.hide_console()`
   che không kịp lúc boot → khung console đen treo cứng (bug 2026-09-27).
3. **Startup Folder (`shell:startup`)**:
   - Nằm trong `%APPDATA%` của chính user → **100% không cần quyền Admin**.
   - File `.vbs` đặt trong thư mục này được Windows tự chạy bằng `wscript.exe` lúc logon.
   - `wscript.exe` là GUI subsystem (không có console riêng).
   - VBS gọi WshShell.Run với exe đã bọc nháy + cờ --tray, style 0 (SW_HIDE),
     no-wait → exe con được tạo ẩn hoàn toàn ngay từ tầng Win32, không một tích
     tắc nháy console, không phụ thuộc race condition Windows Terminal lúc boot.

Legacy — tự dọn lúc `_migrate_legacy()`: Run key cũ của bản <= 2.2.12 và VBS tên cũ
trong Startup. Sót lại thì logon spawn 2 instance (một từ Run key, một từ Startup).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import app_paths

VBS_NAME = "9router-patch.vbs"
# Bản 2.2.12.1 đặt tên dài hơn; đổi về tên ngắn nhưng phải dọn file cũ kẻo logon
# chạy cả hai → mở app hai lần.
_OLD_VBS_NAMES = ("9router-patch-autostart.vbs",)
TRAY_ARGS = "--tray"

# Legacy: Run key của bản <= 2.2.12 — chỉ để dọn sạch.
REG_NAME = "9RouterPatchManager"
REG_SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

try:
    import winreg
except ImportError:  # pragma: no cover (non-Windows platform)
    winreg = None  # type: ignore

_ENABLED: bool | None = None
_MIGRATED = False


def is_supported() -> bool:
    """True chỉ khi chạy từ bản đóng gói exe trên Windows."""
    return sys.platform == "win32" and app_paths.is_frozen()


def _run_exe() -> Path:
    """Exe chuẩn cho autostart: file 9router-patch.exe cạnh file đang chạy."""
    exe = app_paths.get_current_exe()
    target = exe.parent / "9router-patch.exe"
    return target if target.is_file() else exe


def _startup_dir() -> Path:
    """Thư mục Startup của user hiện tại (shell:startup)."""
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / r"Microsoft\Windows\Start Menu\Programs\Startup"
    return Path.home() / r"AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup"


def _vbs_path() -> Path:
    return _startup_dir() / VBS_NAME


def _clear_legacy_run_key() -> None:
    """Xoá Run key cũ trong Registry nếu còn tồn tại."""
    if winreg is None:
        return
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_SUBKEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            try:
                winreg.DeleteValue(key, REG_NAME)
            except FileNotFoundError:
                pass
    except OSError:
        pass


def _clear_stale_vbs() -> None:
    """Xoá launcher tên cũ. Để lại thì logon chạy cả hai file → hai instance."""
    for name in _OLD_VBS_NAMES:
        try:
            (_startup_dir() / name).unlink(missing_ok=True)
        except OSError:
            pass


def _migrate_legacy() -> None:
    global _MIGRATED
    if _MIGRATED:
        return
    _MIGRATED = True
    _clear_legacy_run_key()
    _clear_stale_vbs()


def _read_enabled() -> bool:
    """File .vbs có trong Startup, và nội dung trỏ đúng exe hiện tại + --tray."""
    vbs = _vbs_path()
    if not vbs.is_file():
        return False
    try:
        content = vbs.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    target = str(_run_exe()).lower()
    return target in content.lower() and TRAY_ARGS in content


def is_enabled() -> bool:
    """Autostart có đang bật (file vbs hợp lệ trong Startup folder)?"""
    global _ENABLED
    _migrate_legacy()
    if not is_supported():
        return False
    if _ENABLED is None:
        _ENABLED = _read_enabled()
    return _ENABLED


def set_enabled(on: bool) -> tuple[bool, str | None]:
    """Bật/tắt autostart bằng cách tạo hoặc xóa file .vbs trong Startup Folder.

    Returns: (thành công, thông báo lỗi nếu có).
    """
    global _ENABLED
    _migrate_legacy()
    if not is_supported():
        return False, "Chỉ khả dụng trên bản build exe (.exe)"

    vbs = _vbs_path()
    if on:
        exe = _run_exe()
        # Chốt: chỉ ghi launcher khi exe có thật. Thiếu chốt này, một get_current_exe()
        # bất thường (vd argv[0]="-c" lúc chạy python -c) sẽ ghi VBS trỏ file không
        # tồn tại → logon báo lỗi 80070002 (đo 2026-09-27).
        if not exe.is_file() or exe.suffix.lower() != ".exe":
            return False, f"Không tìm thấy file exe hợp lệ để tự khởi động: {exe}"
        try:
            vbs.parent.mkdir(parents=True, exist_ok=True)
            # Script VBScript: wscript.exe chạy file này lúc login, gọi Run với style 0 (SW_HIDE)
            # không tạo console window, không nháy, không treo.
            vbs_content = (
                "' 9router Patch Manager — Silent Autostart Launcher\r\n"
                "' Tự động sinh bởi 9router Patch Manager. Không sửa thủ công.\r\n"
                'Set WshShell = CreateObject("WScript.Shell")\r\n'
                f'WshShell.Run """{exe}"" {TRAY_ARGS}", 0, False\r\n'
            )
            vbs.write_text(vbs_content, encoding="utf-8")
            _ENABLED = True
            return True, None
        except OSError as e:
            return False, f"Không thể ghi file vào thư mục Startup: {e}"
    else:
        try:
            if vbs.is_file():
                vbs.unlink()
            _ENABLED = False
            return True, None
        except OSError as e:
            return False, f"Không thể xoá file khỏi thư mục Startup: {e}"
