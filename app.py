"""9router Patch Manager Desktop Application Launcher.

Target entry point cho Nuitka onefile executable.
Khởi động uvicorn server trên 127.0.0.1:20129 và tự động mở trình duyệt web.
"""
from __future__ import annotations

import ctypes
import socket
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from pathlib import Path

import uvicorn
import boot_doctor
import tray
from boot_doctor import log_boot
from main import HOST, PORT, app, get_log_config


def _fatal(msg: str) -> None:
    """Không có console nào để in traceback khi exe khởi động hỏng: hộp thoại là
    cách duy nhất user thấy lỗi, nếu không mọi lỗi boot đều thành 'bấm không thấy gì'."""
    try:
        ctypes.windll.user32.MessageBoxW(None, msg, "9router Patcher Manager", 0x10)
    except Exception:
        pass


def _hide_console() -> None:
    """Ẩn hẳn cửa sổ console — delegate sang tray.hide_console()."""
    tray.hide_console()


def _console_banner(url: str) -> None:
    """Khối hướng dẫn một lần lúc khởi động — in lại mỗi lệnh chỉ làm console trôi."""
    print("=" * 62)
    print("  [Enter] Mở Dashboard   [L] Xem Logs   [H] Ẩn Console   [Q] Thoát")
    print(f"  Dashboard: {url}      Logs: {url}/logs")
    print("=" * 62)


def _handle_console_command(cmd: str, url: str) -> bool:
    """Xử lý lệnh console tương tác (Enter, L, H, Q).

    Trả về True nếu ứng dụng tiếp tục chạy, False nếu yêu cầu thoát (Q).
    """
    key = cmd.strip().lower()
    if key == "":
        webbrowser.open(url)
    elif key == "l":
        webbrowser.open(f"{url}/logs")
    elif key == "h":
        _hide_console()
    elif key == "q":
        return False
    return True


def _read_console_line(prompt: str, stop: threading.Event) -> str | None:
    """Đọc một dòng console, trả None ngay khi `stop` được set.

    Khi chạy console thật (isatty): dùng msvcrt để poll non-blocking, nhờ vậy
    menu khay hệ thống 'Thoát' thoát app ngay lập tức mà không phải đợi gõ Enter.
    Khi chạy pipe/detached (!isatty): dùng readline chuẩn.
    """
    sys.stdout.write(prompt)
    sys.stdout.flush()

    if not (sys.stdin and sys.stdin.isatty()):
        try:
            line = sys.stdin.readline()
            return line.rstrip("\r\n") if line else None
        except Exception:
            return None

    try:
        import msvcrt
    except ImportError:
        try:
            return input()
        except (EOFError, KeyboardInterrupt):
            return None

    chars: list[str] = []
    while not stop.is_set():
        if not msvcrt.kbhit():
            time.sleep(0.05)
            continue
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):     # mã tiền tố phím đặc biệt: nuốt phím thứ hai
            msvcrt.getwch()
            continue
        if ch in ("\r", "\n"):
            sys.stdout.write("\n")
            return "".join(chars)
        if ch == "\x08":
            if chars:
                chars.pop()
                sys.stdout.write("\b \b")
            continue
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch >= " ":
            chars.append(ch)
            sys.stdout.write(ch)
    return None


def _port_busy(host: str, port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


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

    # Console mặc định (cp437/cp1252) không in được tiếng Việt — ép UTF-8,
    # lỗi ký tự thay bằng "?" thay vì làm sập app.
    try:
        for stream in (sys.stdout, sys.stderr):
            stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    # 1. Chạy preflight startup doctor trước khi server khởi động
    try:
        ok = boot_doctor.run_doctor(interactive=True)
        if not ok:
            return
    except Exception as e:
        boot_doctor.log_boot(f"Lỗi khi chạy boot_doctor: {e}")

    # 2. Nếu port 20129 đã có tiến trình khác lắng nghe: mở trình duyệt và thoát
    if _port_busy(HOST, PORT):
        print(f"Port {PORT} đã được sử dụng. Mở dashboard trong trình duyệt...")
        webbrowser.open(url)
        return

    # 3. Chạy uvicorn trong thread nền với shutdown event
    config = uvicorn.Config(
        app,
        host=HOST,
        port=PORT,
        log_config=get_log_config(),
    )
    server = uvicorn.Server(config)
    shutdown_event = threading.Event()

    def run_server():
        server.run()
        shutdown_event.set()

    server_thread = threading.Thread(target=run_server, name="uvicorn-server", daemon=True)
    server_thread.start()

    # 4. Tự động mở browser khi server sẵn sàng
    threading.Thread(
        target=_open_browser_when_ready,
        args=(url,),
        name="browser-launcher",
        daemon=True,
    ).start()

    # 5. Giữ console tương tác trên main thread + tray icon khi ẩn
    tray.set_console_title("9router Patcher Manager")
    _console_banner(url)
    tray_icon = None
    if tray.available():
        tray_icon = tray.TrayIcon("9router Patcher Manager",
                                  icon_path=(Path(__file__).parent / "assets" / "app.ico"))
        tray_icon.callbacks = {
            tray.ID_BROWSER: lambda: webbrowser.open(url),
            tray.ID_LOGS: lambda: webbrowser.open(f"{url}/logs"),
            tray.ID_SHOW: tray.show_console,
            tray.ID_QUIT: lambda: shutdown_event.set(),
        }
        try:
            if tray_icon.start():
                log_boot("Tray icon sẵn sàng — ấn [H] để ẩn console về khay.")
            else:
                tray_icon = None
        except Exception:
            tray_icon = None

    try:
        has_stdin = bool(sys.stdin)
        while not shutdown_event.is_set():
            if has_stdin:
                try:
                    cmd = _read_console_line("9router > ", shutdown_event)
                except KeyboardInterrupt:
                    break
                if cmd is None:
                    # stdin đóng (EOF) hoặc shutdown_event đã set — thoát loop
                    break
                state = _handle_console_command(cmd, url)
                if state is False:
                    break
                # "9router > " chỉ nhắc lệnh; hướng dẫn đã in một lần ở banner trên.
            else:
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        if tray_icon is not None:
            tray_icon.stop()
        server.should_exit = True
        server_thread.join(timeout=3.0)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # exe attach-mode: stdout rỗng, không hộp thoại = lỗi im lặng
        _fatal(f"Không thể khởi động 9router Patcher Manager:\n\n{e}\n\n"
               f"{traceback.format_exc()}")
        raise
