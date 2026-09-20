"""Black-box verification of the compiled 9router-patch.exe binary.

Skips unless dist/9router-patch.exe exists (built by `python build_app.py`).
The dashboard binds a fixed 127.0.0.1:20129, so this test must own that port:
skip if something is already listening there rather than fight the dev instance.
"""
import os
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXE_PATH = ROOT / "dist" / "9router-patch.exe"
DASH_URL = "http://127.0.0.1:20129/"
READY_TIMEOUT = 60.0


def _port_busy() -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", 20129)) == 0


@pytest.mark.skipif(not EXE_PATH.is_file(), reason="dist/9router-patch.exe not built yet")
@pytest.mark.skipif(_port_busy(), reason="port 20129 already in use (dev dashboard running)")
def test_compiled_binary_boots_serves_html_and_hides_plaintext():
    """The compiled binary boots uvicorn, answers HTTP, and never unpacks plaintext patches."""
    proc = subprocess.Popen([str(EXE_PATH)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    html = ""
    try:
        deadline = time.time() + READY_TIMEOUT
        while time.time() < deadline:
            time.sleep(0.5)
            try:
                with urllib.request.urlopen(DASH_URL, timeout=1.0) as resp:
                    if resp.status == 200:
                        html = resp.read().decode("utf-8")
                        break
            except Exception:
                continue
        assert html, f"server did not answer on {DASH_URL} within {READY_TIMEOUT}s"
        assert "9router" in html
        assert "Bảng điều khiển" in html, "dashboard heading missing from compiled output"

        # The onefile payload is unpacked to %TEMP%\onefile_*: patches.enc is expected there,
        # plaintext patches.toml must never be.
        temp_dir = Path(os.environ.get("TEMP", r"C:\Windows\Temp"))
        found_enc = False
        for od in temp_dir.glob("onefile_*"):
            if (od / "patches.enc").is_file():
                found_enc = True
                assert not (od / "patches.toml").is_file(), f"plaintext patches.toml leaked in {od}"
        assert found_enc, "expected patches.enc in the onefile temp directory"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
