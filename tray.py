"""Windows system tray icon cho 9router Patcher Manager.

Ẩn console là ẩn HẲN (ShowWindow SW_HIDE + WS_EX_TOOLWINDOW: mất khỏi taskbar và
alt-tab), và để lại một icon ở khay hệ thống. Chuột phải icon mở menu:
Mở Dashboard / Hiện Console / Thoát.

Thuần ctypes — không thêm dependency. ponytail: Windows-only; non-Windows hoặc khi
không tạo được cửa sổ ẩn thì available() trả False và mọi hàm thành no-op. Thêm
backend GTK/AppIndicator nếu bao giờ build cho Linux.
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Callable

# ---------------------------------------------------------------- Win32 constants
WM_APP = 0x8000
WM_TRAY = WM_APP + 1
WM_COMMAND = 0x0111
WM_DESTROY = 0x0002
WM_RBUTTONUP = 0x0205
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_CLOSE = 0x0010

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 1, 2, 4

SW_HIDE, SW_SHOW, SW_RESTORE = 0, 5, 9
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000

TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0002, 0x0100
MF_STRING, MF_SEPARATOR = 0x0000, 0x0800

ID_BROWSER, ID_SHOW, ID_LOGS, ID_QUIT = 1, 2, 3, 4

IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x0010, 0x0040
IDI_APPLICATION = 32512

HWND_MESSAGE = -3
PM_REMOVE = 0x0001

_IS_WIN = sys.platform == "win32"


class _NOTIFYICONDATAW(ctypes.Structure):
    """NOTIFYICONDATAW (Vista+ layout). guidItem là GUID 16 byte = 4 DWORD."""
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wintypes.HICON),
    ]


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


_WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)


def available() -> bool:
    """True nếu tray dùng được: Windows + tạo được cửa sổ ẩn nhận message."""
    if not _IS_WIN:
        return False
    try:
        return ctypes.windll.user32.GetConsoleWindow() != 0
    except Exception:
        return False


def console_hwnd() -> int:
    """HWND của cửa sổ console đang chạy, 0 nếu không có."""
    if not _IS_WIN:
        return 0
    try:
        return int(ctypes.windll.kernel32.GetConsoleWindow())
    except Exception:
        return 0


def _set_exstyle(hwnd: int, on: bool) -> None:
    u = ctypes.windll.user32
    get_l = getattr(u, "GetWindowLongPtrW", u.GetWindowLongW)
    set_l = getattr(u, "SetWindowLongPtrW", u.SetWindowLongW)
    cur = get_l(hwnd, GWL_EXSTYLE)
    if on:
        new = (cur | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
    else:
        new = (cur & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW
    set_l(hwnd, GWL_EXSTYLE, new)


def hide_console() -> bool:
    """Ẩn hẳn cửa sổ console. WS_EX_TOOLWINDOW bỏ nó khỏi taskbar lẫn alt-tab —
    nếu chỉ SW_HIDE thì Windows vẫn giữ một entry taskbar."""
    hwnd = console_hwnd()
    if not hwnd:
        return False
    try:
        u = ctypes.windll.user32
        _set_exstyle(hwnd, on=True)
        u.ShowWindow(hwnd, SW_HIDE)
        return True
    except Exception:
        return False


def show_console() -> bool:
    """Bỏ cờ toolwindow rồi hiện lại cửa sổ console và đưa lên trước."""
    hwnd = console_hwnd()
    if not hwnd:
        return False
    try:
        u = ctypes.windll.user32
        _set_exstyle(hwnd, on=False)
        u.ShowWindow(hwnd, SW_SHOW)
        u.ShowWindow(hwnd, SW_RESTORE)
        u.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


class TrayIcon:
    """Icon khay hệ thống + menu chuột phải.

    Đối tượng này sở hữu một cửa sổ ẩn (HWND_MESSAGE) làm nơi nhận callback từ shell.
    `pump_once()` phải được gọi định kỳ trên CÙNG thread đã tạo nó — main thread.
    """

    def __init__(self, tip: str, icon_path: Path | None = None) -> None:
        self.tip = tip[:127]
        self.icon_path = icon_path
        self.hwnd = 0
        self.hicon = 0
        self._wndproc_ref = None      # giữ tham chiếu, WNDPROC bị GC là crash
        self._nid: _NOTIFYICONDATAW | None = None
        self._added = False
        self._class_name = "9routerPatcherTrayWnd"

    # ------------------------------------------------------------ lifecycle
    def start(self) -> bool:
        """Tạo cửa sổ ẩn + thêm icon. False nếu bất kỳ bước nào fail (app vẫn chạy)."""
        if not _IS_WIN:
            return False
        try:
            k32, u32 = ctypes.windll.kernel32, ctypes.windll.user32
            hinst = k32.GetModuleHandleW(None)

            self._wndproc_ref = _WNDPROC(self._wndproc)
            wc = _WNDCLASSW()
            wc.lpfnWndProc = ctypes.cast(self._wndproc_ref, ctypes.c_void_p)
            wc.hInstance = hinst
            wc.lpszClassName = self._class_name
            if not u32.RegisterClassW(ctypes.byref(wc)):
                # 1410 = class đã tồn tại (instance thứ hai) — vẫn dùng lại được
                if k32.GetLastError() != 1410:
                    return False

            self.hwnd = u32.CreateWindowExW(
                0, self._class_name, self._class_name, 0,
                0, 0, 0, 0, HWND_MESSAGE, 0, hinst, 0,
            )
            if not self.hwnd:
                return False
            return self._add_icon()
        except Exception:
            self.hwnd = 0
            return False

    def _add_icon(self) -> bool:
        u32 = ctypes.windll.user32
        self.hicon = self._load_icon()
        nid = _NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self.hicon
        nid.szTip = self.tip
        self._nid = nid
        self._added = bool(ctypes.windll.shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)))
        return self._added

    def _load_icon(self) -> int:
        u32 = ctypes.windll.user32
        if self.icon_path and self.icon_path.is_file():
            h = u32.LoadImageW(None, str(self.icon_path), IMAGE_ICON, 0, 0,
                               LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if h:
                return h
        # Fallback: icon mặc định của Windows, không cần file nào.
        try:
            return u32.LoadIconW(None, ctypes.c_wchar_p(IDI_APPLICATION))
        except Exception:
            return 0

    def stop(self) -> None:
        """Gỡ icon khỏi khay và hủy cửa sổ ẩn."""
        if not _IS_WIN or not self.hwnd:
            return
        try:
            u32 = ctypes.windll.user32
            if self._added and self._nid is not None:
                u32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
                self._added = False
            if self.hicon:
                u32.DestroyIcon(self.hicon)
                self.hicon = 0
            u32.DestroyWindow(self.hwnd)
        except Exception:
            pass
        self.hwnd = 0

    # ------------------------------------------------------------ pump
    def pump_once(self) -> None:
        """Xử lý mọi message đang chờ. Gọi định kỳ từ main thread."""
        if not _IS_WIN or not self.hwnd:
            return
        u32 = ctypes.windll.user32
        msg = wintypes.MSG()
        while u32.PeekMessageW(ctypes.byref(msg), 0, 0, 0, PM_REMOVE):
            u32.TranslateMessage(ctypes.byref(msg))
            u32.DispatchMessageW(ctypes.byref(msg))

    # ------------------------------------------------------------ menu
    def _show_menu(self) -> int:
        """Popup menu tại con trỏ; trả về ID lệnh đã chọn (TPM_RETURNCMD), 0 nếu hủy."""
        u32 = ctypes.windll.user32
        hmenu = u32.CreatePopupMenu()
        if not hmenu:
            return 0
        try:
            u32.AppendMenuW(hmenu, MF_STRING, ID_BROWSER, "Mở Dashboard")
            u32.AppendMenuW(hmenu, MF_STRING, ID_LOGS, "Mở System Logs")
            u32.AppendMenuW(hmenu, MF_STRING, ID_SHOW, "Hiện Console")
            u32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
            u32.AppendMenuW(hmenu, MF_STRING, ID_QUIT, "Thoát")
            pt = wintypes.POINT()
            u32.GetCursorPos(ctypes.byref(pt))
            # SetForegroundWindow là bắt buộc: nếu không menu sẽ không tự đóng khi
            # bấm ra ngoài (hành vi kinh điển của TrackPopupMenu trên Windows).
            u32.SetForegroundWindow(self.hwnd)
            return int(u32.TrackPopupMenu(
                hmenu, TPM_RIGHTBUTTON | TPM_RETURNCMD, pt.x, pt.y, 0, self.hwnd, None))
        finally:
            u32.DestroyMenu(hmenu)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        u32 = ctypes.windll.user32
        if msg == WM_TRAY:
            event = lparam & 0xFFFF
            if event in (WM_RBUTTONUP, WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                cmd = self._show_menu()
                if cmd:
                    self.on_menu(cmd)
        elif msg == WM_CLOSE:
            u32.DestroyWindow(hwnd)
            return 0
        return u32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # ------------------------------------------------------------ callbacks
    def on_menu(self, cmd_id: int) -> None:
        """Điều phối lệnh menu. Tách khỏi Win32 để test được không cần shell."""
        cb = self.callbacks.get(cmd_id)
        if cb is not None:
            cb()

    callbacks: dict[int, Callable[[], None]] = {}


def dispatch(callbacks: dict[int, Callable[[], None]], cmd_id: int) -> bool:
    """Gọi callback theo id menu. Trả về True nếu có callback được gọi."""
    cb = callbacks.get(cmd_id)
    if cb is None:
        return False
    cb()
    return True
