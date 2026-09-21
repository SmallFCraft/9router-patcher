"""9router Patch Manager Desktop Application Launcher.

Target entry point cho Nuitka onefile executable.
Khởi động uvicorn server trên 127.0.0.1:20129 và tự động mở trình duyệt web.
"""
from __future__ import annotations

import socket
import threading
import time
import traceback
import urllib.request
import webbrowser

import uvicorn
from main import HOST, PORT, app, get_log_config


def _fatal(msg: str) -> None:
    """Exe chạy --windows-console-mode=attach: không có console nào để in traceback.
    Không có hộp thoại này thì mọi lỗi lúc khởi động đều biến thành 'bấm không thấy gì'."""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, msg, "9router Patch Manager", 0x10)
    except Exception:
        pass


def _port_busy(port: int) -> bool:
    """True nếu đã có process LISTENING trên 127.0.0.1:port (instance khác)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        s.close()


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

    # Nếu port 20129 đã có app chạy sẵn: mở thẳng browser tới dashboard rồi thoát êm,
    # tránh uvicorn đụng Errno 10048 chết im lặng không hiện gì cho user.
    if _port_busy(PORT):
        webbrowser.open(url)
        return

    # Mở browser trên luồng riêng sau khi uvicorn lắng nghe port
    threading.Thread(
        target=_open_browser_when_ready,
        args=(url,),
        name="browser-launcher",
        daemon=True,
    ).start()

    try:
        uvicorn.run(
            app,
            host=HOST,
            port=PORT,
            log_config=get_log_config(),
        )
    except Exception as e:
        _fatal(f"Không thể khởi động server:\n\n{e}\n\n{traceback.format_exc()}")
        raise


if __name__ == "__main__":
    main()