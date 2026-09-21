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
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def get_nuitka_cmd(output_dir: Path, fast: bool = False) -> list[str]:
    enc_source = ROOT / "assets" / "patches.enc"
    tpl_source = ROOT / "templates"
    ico_source = ROOT / "assets" / "app.ico"
    # Module chỉ uvicorn[standard]/fastapi/pydantic kéo theo, app không tham chiếu.
    # Đã test trong process (sys.modules[m] = None) — nhưng đó KHÔNG đủ: Nuitka
    # deployment mode biến module bị loại thành ImportError CỨNG, nên try/except
    # quanh importlib.import_module KHÔNG cứu được. Chỉ loại module mà consumer
    # import bằng câu lệnh `import X` trần nằm trong try/except (uvicorn làm vậy).
    #   - loại được: import trần + try/except
    #   - KHÔNG loại được: importlib.import_module() (fastapi.responses dùng cho
    #     orjson/ujson) => exe chết ngay khi boot, xem tests/test_e2e_binary.py
    #   - giữ lại: click (uvicorn.main import eager, không try/except)
    skip = ",".join([
        "tzdata",         # 604 data file trong exe, app không validate IANA tz
        "watchfiles",     # chỉ cho --reload
        "httptools",      # http protocol tuỳ chọn (default h11)
        "websockets",     # ws protocol tuỳ chọn, app không có websocket
        "yaml",           # chỉ cho starlette OpenAPI response
    ])
    # Máy 16 cores => cấp N-2 jobs để compile/check C files song song
    jobs = max(1, (os.cpu_count() or 4) - 2)
    cmd = [
        sys.executable,
        "-m",
        "nuitka",
        "--onefile",
        "--assume-yes-for-downloads",
        "--windows-console-mode=force",
        f"--jobs={jobs}",
        "--nofollow-import-to=" + skip,
        f"--include-data-dir={tpl_source}=templates",
        f"--include-data-files={enc_source}=patches.enc",
        f"--output-dir={output_dir}",
        "--output-filename=9router-patch.exe",
        f"--report={output_dir / 'build-report.xml'}",
    ]
    if fast:
        # Dev mode: skip ~50s zstd compression; exe phình lên ~97MB nhưng build nhanh
        cmd.append("--onefile-no-compression")
    if ico_source.is_file():
        cmd.append(f"--windows-icon-from-ico={ico_source}")
    cmd.append("app.py")
    return cmd


def _exe_locked(exe: Path) -> bool:
    """True nếu KHÔNG mở được file để ghi — tức exe đang chạy giữ file."""
    try:
        with open(exe, "r+b"):
            return False
    except OSError:
        return True


def _release_dist_exe(exe: Path) -> None:
    """Exe của lần build trước còn chạy sẽ giữ file, Nuitka onefile bootstrap unlink fail
    WinError 5. Kill theo ĐÚNG image name — KHÔNG dùng /FI "WINDOWTITLE ..." (nó khớp cả
    cửa sổ File Explorer và kill luôn explorer.exe, đã xảy ra thật 2026-09-21)."""
    if not exe.is_file() or not _exe_locked(exe):
        return
    subprocess.run(["taskkill", "/F", "/T", "/IM", exe.name],
                   shell=False, capture_output=True, text=True)
    if not _exe_locked(exe):
        print(f"  đã tắt {exe.name} còn chạy (đang giữ file để build)")
    else:
        print(f"  CẢNH BÁO: {exe.name} vẫn bị giữ — build sẽ lỗi WinError 5. "
              f"Đóng app rồi chạy lại.")


def build(fast: bool = False) -> int:
    dist = ROOT / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    _release_dist_exe(dist / "9router-patch.exe")
    t0 = time.monotonic()

    print("== Step 1: Encrypting patches.toml ==")
    enc_script = ROOT / "tools" / "make_patches_blob.py"
    r = subprocess.run([sys.executable, str(enc_script)])
    if r.returncode != 0:
        print("Error: Encrypting patches failed!")
        return r.returncode
    t1 = time.monotonic()

    mode_str = " (FAST dev mode: no zstd compression)" if fast else ""
    print(f"\n== Step 2: Compiling with Nuitka{mode_str} ==")
    cmd = get_nuitka_cmd(dist, fast=fast)
    print("Running:", " ".join(cmd))
    r = subprocess.run(cmd, cwd=str(ROOT))
    if r.returncode != 0:
        print(f"Error: Nuitka build failed with exit code {r.returncode}")
        return r.returncode
    t2 = time.monotonic()

    exe = dist / "9router-patch.exe"
    if exe.is_file():
        size_mb = exe.stat().st_size / (1024 * 1024)
        print(f"\nEncrypt: {t1 - t0:.1f}s | Nuitka: {t2 - t1:.1f}s | Total: {t2 - t0:.1f}s")
        print(f"SUCCESS: Built {exe} ({size_mb:.1f} MB)")
        return 0
    print(f"\nError: Expected output {exe} was not created!")
    return 1


if __name__ == "__main__":
    raise SystemExit(build(fast="--fast" in sys.argv[1:]))
