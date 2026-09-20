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
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def get_nuitka_cmd(output_dir: Path) -> list[str]:
    enc_source = ROOT / "assets" / "patches.enc"
    tpl_source = ROOT / "templates"
    ico_source = ROOT / "assets" / "app.ico"
    cmd = [
        sys.executable,
        "-m",
        "nuitka",
        "--onefile",
        "--assume-yes-for-downloads",
        "--windows-console-mode=force",
        f"--include-data-dir={tpl_source}=templates",
        f"--include-data-files={enc_source}=patches.enc",
        f"--output-dir={output_dir}",
        "--output-filename=9router-patch.exe",
    ]
    if ico_source.is_file():
        cmd.append(f"--windows-icon-from-ico={ico_source}")
    cmd.append("app.py")
    return cmd


def build() -> int:
    dist = ROOT / "dist"
    dist.mkdir(parents=True, exist_ok=True)

    print("== Step 1: Encrypting patches.toml ==")
    enc_script = ROOT / "tools" / "make_patches_blob.py"
    r = subprocess.run([sys.executable, str(enc_script)])
    if r.returncode != 0:
        print("Error: Encrypting patches failed!")
        return r.returncode

    print("\n== Step 2: Compiling with Nuitka (this may take several minutes) ==")
    cmd = get_nuitka_cmd(dist)
    print("Running:", " ".join(cmd))
    r = subprocess.run(cmd, cwd=str(ROOT))
    if r.returncode != 0:
        print(f"Error: Nuitka build failed with exit code {r.returncode}")
        return r.returncode

    exe = dist / "9router-patch.exe"
    if exe.is_file():
        size_mb = exe.stat().st_size / (1024 * 1024)
        print(f"\nSUCCESS: Built {exe} ({size_mb:.1f} MB)")
        return 0
    print(f"\nError: Expected output {exe} was not created!")
    return 1


if __name__ == "__main__":
    raise SystemExit(build())
