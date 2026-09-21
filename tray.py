"""Windows system tray icon cho 9router Patcher Manager.

Ẩn console là ẩn HẲN (ShowWindow SW_HIDE + WS_EX_TOOLWINDOW: mất khỏi taskbar và
alt-tab), và để lại một icon ở khay hệ thống. Chuột phải icon mở menu:
Mở Dashboard / Mở System Logs / Hiện Console / Thoát.

Message loop chạy trên MỘT THREAD RIÊNG: console loop của app chặn ở
input("9router > "), nên nếu pump trên main thread thì menu khay không bao giờ
được dispatch.

Thuần ctypes — không thêm dependency. ponytail: Windows-only; non-Windows hoặc khi
không tạo được cửa sổ ẩn thì available() trả False và mọi hàm thành no-op. Thêm
backend GTK/AppIndicator nếu bao giờ build cho Linux.
"""

from __future__ import annotations

import ctypes
import sys
import threading
import uuid
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
GA_ROOT = 2
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000

TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0002, 0x0100
MF_STRING, MF_SEPARATOR = 0x0000, 0x0800

ID_BROWSER, ID_SHOW, ID_LOGS, ID_QUIT = 1, 2, 3, 4

IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x0010, 0x0040
IDI_APPLICATION = 32512

# CreateWindowExW với hwndParent = HWND_MESSAGE tạo "message-only window".
# PHẢI là wintypes.HWND(-3), không phải int -3: ctypes ép int thành c_int 32-bit,
# giá trị âm ấy thành handle rác và CreateWindowExW fail WinError 1400.
HWND_MESSAGE = wintypes.HWND(-3)

_IS_WIN = sys.platform == "win32"
_PROTOTYPES_SET = False


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


_WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)


class _WNDCLASSW(ctypes.Structure):
    # lpfnWndProc PHẢI là _WNDPROC, không phải c_void_p: gán c_void_p vào
    # WNDCLASSW khiến RegisterClassW deref con trỏ hàm rác → access violation
    # (crash 0xC0000005, bug 2026-09-21).
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


def _ensure_prototypes() -> None:
    """Khai báo argtypes/restype một lần. Không có bước này ctypes ép mọi
    HWND/LPARAM về c_int 32-bit → handle rác (WinError 1400) và OverflowError
    trong WNDPROC trên Python 64-bit."""
    global _PROTOTYPES_SET
    if _PROTOTYPES_SET or not _IS_WIN:
        return
    u32 = ctypes.windll.user32
    u32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASSW)]
    u32.RegisterClassW.restype = wintypes.ATOM
    u32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p,
    ]
    u32.CreateWindowExW.restype = wintypes.HWND
    u32.DefWindowProcW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    u32.DefWindowProcW.restype = ctypes.c_ssize_t
    u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    u32.ShowWindow.restype = wintypes.BOOL
    u32.SetForegroundWindow.argtypes = [wintypes.HWND]
    u32.SetForegroundWindow.restype = wintypes.BOOL
    ctypes.windll.shell32.Shell_NotifyIconW.argtypes = [
        wintypes.DWORD, ctypes.POINTER(_NOTIFYICONDATAW)]
    ctypes.windll.shell32.Shell_NotifyIconW.restype = wintypes.BOOL
    _PROTOTYPES_SET = True


def available() -> bool:
    """True nếu hệ thống hỗ trợ khay hệ thống Windows (shell32 + user32).

    TrayIcon hoàn toàn độc lập với việc app có console hay không: nó sở hữu một
    cửa sổ ẩn (HWND_MESSAGE). Có console thì lệnh 'Ẩn/Hiện Console' điều khiển thêm
    console đó, không có thì menu Dashboard/Logs/Thoát vẫn dùng tốt."""
    return _IS_WIN


def console_hwnd() -> int:
    """HWND của cửa sổ console đang chạy, 0 nếu không có."""
    if not _IS_WIN:
        return 0
    try:
        return int(ctypes.windll.kernel32.GetConsoleWindow())
    except Exception:
        return 0


def _console_windows() -> list[int]:
    """HWND console + cửa sổ gốc (GA_ROOT). Windows Terminal bọc console trong
    một frame cha — chỉ ẩn PseudoConsoleWindow thì frame ngoài vẫn nằm taskbar."""
    hwnd = console_hwnd()
    if not hwnd:
        return []
    wins = [hwnd]
    try:
        root = int(ctypes.windll.user32.GetAncestor(hwnd, GA_ROOT))
        if root and root != hwnd:
            wins.append(root)
    except Exception:
        pass
    return wins


def set_console_title(title: str) -> bool:
    """Đặt title cửa sổ console. False khi không có console / non-Windows."""
    if not _IS_WIN or not console_hwnd():
        return False
    try:
        return bool(ctypes.windll.kernel32.SetConsoleTitleW(title))
    except Exception:
        return False


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
    """Ẩn HẲN mọi cửa sổ console (mất khỏi taskbar lẫn alt-tab)."""
    wins = _console_windows()
    if not wins:
        return False
    try:
        _ensure_prototypes()
        u = ctypes.windll.user32
        for hwnd in wins:
            _set_exstyle(hwnd, on=True)
            u.ShowWindow(hwnd, SW_HIDE)
        return True
    except Exception:
        return False


def show_console() -> bool:
    """Hiện lại cửa sổ console và đưa lên trước."""
    wins = _console_windows()
    if not wins:
        return False
    try:
        _ensure_prototypes()
        u = ctypes.windll.user32
        for hwnd in wins:
            _set_exstyle(hwnd, on=False)
            u.ShowWindow(hwnd, SW_SHOW)
            u.ShowWindow(hwnd, SW_RESTORE)
        u.SetForegroundWindow(wins[0])
        return True
    except Exception:
        return False


class TrayIcon:
    """Icon khay hệ thống + menu chuột phải.

    start() tạo message-only window, thêm icon, rồi chạy message loop trên một
    daemon thread riêng — main thread của app chặn ở input() nên không pump được.
    stop() gỡ icon và join thread.
    """

    def __init__(self, tip: str, icon_path: Path | None = None) -> None:
        self.tip = tip[:127]
        self.icon_path = icon_path
        self.hwnd = 0
        self.hicon = 0
        self.callbacks: dict[int, Callable[[], None]] = {}
        self._nid: _NOTIFYICONDATAW | None = None
        self._added = False
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._ok = False
        # Class name unique mỗi instance: window class giữ con trỏ WNDPROC của
        # chính instance này. Dùng tên cố định thì instance thứ hai reuse atom cũ
        # nhưng callback của instance một đã bị GC → access violation (bug 2026-09-21).
        self._class_name = f"9routerPatcherTrayWnd_{uuid.uuid4().hex[:8]}"

    # ------------------------------------------------------------ lifecycle
    def start(self) -> bool:
        """Tạo cửa sổ ẩn + thêm icon, loop chạy trên thread nền riêng.

        Window/Message/Icon đều sống trên thread đó. False nếu fail (app vẫn chạy).
        Gọi hai lần là lỗi lập trình — start() lần hai trả False."""
        if not _IS_WIN or self._thread is not None:
            return False
        self._ready.clear()
        self._ok = False
        self._thread = threading.Thread(
            target=self._thread_main, name="tray-loop", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5.0)
        if not self._ok:
            self._thread = None
        return self._ok

    def _thread_main(self) -> None:
        """Toàn bộ vòng đời tray (register → window → icon → loop → cleanup)."""
        # giữ tham chiếu trong frame này, WNDPROC bị GC khi thread còn sống là crash
        _wndproc_ref = None
        try:
            _ensure_prototypes()
            k32, u32 = ctypes.windll.kernel32, ctypes.windll.user32
            hinst = k32.GetModuleHandleW(None)

            _wndproc_ref = _WNDPROC(self._wndproc)
            wc = _WNDCLASSW()
            wc.style = 0
            wc.lpfnWndProc = _wndproc_ref
            wc.cbClsExtra = 0
            wc.cbWndExtra = 0
            wc.hInstance = hinst
            wc.hIcon = None
            wc.hCursor = None
            wc.hbrBackground = None
            wc.lpszMenuName = None
            wc.lpszClassName = self._class_name
            if not u32.RegisterClassW(ctypes.byref(wc)):
                return

            self.hwnd = int(u32.CreateWindowExW(
                0, self._class_name, self._class_name, 0,
                0, 0, 0, 0, HWND_MESSAGE, None, hinst, None,
            ))
            if not self.hwnd:
                return
            if not self._add_icon():
                u32.DestroyWindow(self.hwnd)
                self.hwnd = 0
                return
            self._ok = True
        finally:
            self._ready.set()
        if not self._ok:
            return
        self._message_loop()    # dọn icon + hủy window khi loop thoát

    def _add_icon(self) -> bool:
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
        """Gỡ icon khỏi khay và dừng loop nền."""
        if not _IS_WIN or not self.hwnd:
            return
        try:
            ctypes.windll.user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self.hwnd = 0

    # ------------------------------------------------------------ loop
    def _message_loop(self) -> None:
        """Blocking GetMessage loop trên thread riêng — menu khay phản hồi ngay
        cả khi main thread đang chặn ở input(). Dọn icon khi thoát."""
        u32 = ctypes.windll.user32
        msg = wintypes.MSG()
        while u32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
            u32.TranslateMessage(ctypes.byref(msg))
            u32.DispatchMessageW(ctypes.byref(msg))
        try:
            if self._added and self._nid is not None:
                ctypes.windll.shell32.Shell_NotifyIconW(
                    NIM_DELETE, ctypes.byref(self._nid))
                self._added = False
            if self.hicon:
                u32.DestroyIcon(self.hicon)
                self.hicon = 0
            u32.DestroyWindow(self.hwnd)
            # Trả class atom lại cho hệ thống — nếu không, mỗi start/stop để rác
            # một class đã đăng ký (và con trỏ WNDPROC của nó).
            u32.UnregisterClassW(self._class_name, ctypes.windll.kernel32.GetModuleHandleW(None))
        except Exception:
            pass

    def pump_once(self) -> None:
        """Giữ cho tương thích ngược — loop giờ chạy nền, không cần gọi nữa."""

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
        # Callback chạy trên C stack của Win32: một exception Python lọt ra ngoài
        # sẽ không unwind được và thành access violation (faulthandler báo AV ở
        # chính CreateWindowExW). Nuốt lỗi, luôn trả về qua DefWindowProcW.
        try:
            if msg == WM_TRAY:
                event = lparam & 0xFFFF
                if event in (WM_RBUTTONUP, WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    cmd = self._show_menu()
                    if cmd:
                        self.on_menu(cmd)
            elif msg == WM_CLOSE:
                u32.DestroyWindow(hwnd)
                u32.PostQuitMessage(0)
                return 0
        except Exception:
            pass
        try:
            return u32.DefWindowProcW(hwnd, msg, wparam, lparam)
        except Exception:
            return 0

    # ------------------------------------------------------------ callbacks
    def on_menu(self, cmd_id: int) -> None:
        """Điều phối lệnh menu. Tách khỏi Win32 để test được không cần shell."""
        cb = self.callbacks.get(cmd_id)
        if cb is not None:
            cb()


def dispatch(callbacks: dict[int, Callable[[], None]], cmd_id: int) -> bool:
    """Gọi callback theo id menu. Trả về True nếu có callback được gọi."""
    cb = callbacks.get(cmd_id)
    if cb is None:
        return False
    cb()
    return True
