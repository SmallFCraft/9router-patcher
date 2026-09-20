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