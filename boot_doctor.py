"""Startup preflight doctor and environment preparation for 9router Patch Manager."""
from __future__ import annotations

import collections
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import app_paths
import engine
try:
    import updater
except ImportError:
    updater = None

_BOOT_LOGS = collections.deque(maxlen=500)
SILENT_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

def log_boot(msg: str) -> None:
    timestamp = time.strftime("%H:%M:%S")
    line = f"[{timestamp}] {msg}"
    _BOOT_LOGS.append(line)
    try:
        log_file = app_paths.get_log_dir() / "boot.log"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def get_boot_logs() -> list[str]:
    return list(_BOOT_LOGS)

def check_node() -> tuple[bool, str]:
    node = shutil.which("node")
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not (node and npm):
        msg = "Thiếu Node.js hoặc npm. Vui lòng cài đặt bản LTS từ https://nodejs.org/"
        log_boot(f"ERROR: {msg}")
        return False, msg
    try:
        r = subprocess.run([node, "--version"], capture_output=True, text=True,
                           timeout=5, creationflags=SILENT_FLAGS)
        ver = r.stdout.strip()
        msg = f"Node.js {ver} & npm sẵn sàng"
        log_boot(f"OK: {msg}")
        return True, msg
    except Exception as e:
        msg = f"Không kiểm tra được phiên bản Node.js: {e}"
        log_boot(f"ERROR: {msg}")
        return False, msg

def check_9router() -> tuple[bool, str]:
    try:
        idir = engine.install_dir()
        ver = updater.current_version() if updater else "unknown"
        msg = f"9router v{ver} đã cài đặt tại {idir.name}"
        log_boot(f"OK: {msg}")
        return True, msg
    except Exception:
        msg = "Chưa phát hiện gói 9router toàn cục"
        log_boot(f"WARN: {msg}")
        return False, msg

def install_9router(on_output=None) -> tuple[bool, str]:
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm:
        return False, "Không tìm thấy npm để cài đặt"
    log_boot("Bắt đầu cài đặt 9router toàn cục qua npm...")
    cmd = [npm, "install", "-g", "9router@latest"]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1, creationflags=SILENT_FLAGS)
        if proc.stdout:
            for line in iter(proc.stdout.readline, ""):
                cleaned = line.strip()
                if cleaned:
                    log_boot(f"npm: {cleaned}")
                    if on_output:
                        on_output(cleaned)
            proc.stdout.close()
        try:
            proc.wait(timeout=300)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
            err = "npm install quá thời gian 300s — đã hủy tiến trình"
            log_boot(f"ERROR: {err}")
            return False, err
        if proc.returncode == 0:
            log_boot("Cài đặt 9router@latest thành công")
            return True, "Cài đặt 9router@latest thành công"
        return False, f"npm install thoát với mã lỗi {proc.returncode}"
    except Exception as e:
        err = f"Lỗi trong quá trình cài đặt 9router: {e}"
        log_boot(f"ERROR: {err}")
        return False, err

def check_and_apply_patches(on_output=None) -> tuple[bool, str]:
    try:
        build_path = engine.build_dir()
        if not build_path.exists():
            return False, "Thư mục build 9router không tồn tại"
        patches = engine.load_patches()
        states = engine.scan(build_path, patches)
        unapplied = [s.patch.id for s in states if s.state != "applied"]
        if not unapplied:
            msg = f"{len(patches)}/{len(patches)} Patches đã áp dụng"
            log_boot(f"OK: {msg}")
            return True, msg
        log_boot(f"Phát hiện {len(unapplied)} patch chưa áp dụng. Bắt đầu auto-apply...")
        if on_output:
            on_output(f"Đang tự động áp dụng {len(unapplied)} patch...")
        changed = engine.apply(build_path, patches)
        msg = f"Đã áp dụng thành công {len(patches)} patches ({len(changed)} files thay đổi)"
        log_boot(f"OK: {msg}")
        return True, msg
    except Exception as e:
        err = f"Lỗi khi kiểm tra/áp dụng patch: {e}"
        log_boot(f"ERROR: {err}")
        return False, err

def ensure_router_stack(on_output=None) -> tuple[bool, str]:
    if not updater:
        return True, "updater module không có sẵn"
    try:
        r_up = updater.pid_on_port(20128) is not None
        h_up = updater.pid_on_port(8787) is not None
        if r_up and h_up:
            msg = "Router stack đang chạy (port 20128 & 8787)"
            log_boot(f"OK: {msg}")
            return True, msg
        log_boot("Khởi động router stack trong nền...")
        if on_output:
            on_output("Khởi động proxy router trong nền...")
        ok = updater.start_router_stack(lambda ev: log_boot(str(ev.get("text", ""))))
        return ok, "Đã khởi động router stack" if ok else "Không thể khởi động router stack"
    except Exception as e:
        err = f"Lỗi khởi động router stack: {e}"
        log_boot(f"ERROR: {err}")
        return False, err

def run_doctor(interactive: bool = True) -> bool:
    print("=" * 60)
    print(" 9router Patcher Manager")
    print("=" * 60)

    # 1. Node.js & npm
    print("[1/4] Kiểm tra Node.js & npm...", end=" ", flush=True)
    ok, msg = check_node()
    if ok:
        print("[ OK ]")
    else:
        print("[ THIẾU ]")
        print(f"\n  -> {msg}\n")
        if interactive:
            input("Nhấn Enter để thoát...")
        return False

    # 2. 9router global package
    print("[2/4] Kiểm tra 9router toàn cục...", end=" ", flush=True)
    ok, msg = check_9router()
    if ok:
        print("[ OK ]")
    else:
        print("[ THIẾU ]")
        if interactive:
            ans = input("\n? 9router chưa được cài đặt. Cài đặt toàn cục qua npm? (Y/n) [Y]: ").strip().lower()
            if ans in ("", "y", "yes"):
                print("  > npm install -g 9router@latest...")
                i_ok, i_msg = install_9router(on_output=lambda line: print(f"    {line[:70]}", end="\r", flush=True))
                print()
                if not i_ok:
                    print(f"  [!] {i_msg}")
                    input("Nhấn Enter để tiếp tục (chế độ xem)...")
                else:
                    print("  [ OK ] Cài đặt 9router hoàn tất!")
            else:
                print("  Bỏ qua cài đặt 9router.")
        else:
            return False

    # 3. Patch Status
    print("[3/4] Kiểm tra patches tối ưu...", end=" ", flush=True)
    p_ok, p_msg = check_and_apply_patches()
    if p_ok:
        print("[ OK ]")
    else:
        print(f"[ CẢNH BÁO ] ({p_msg})")

    # 4. Proxy Router Stack
    print("[4/4] Khởi động Proxy Router Stack...", end=" ", flush=True)
    s_ok, s_msg = ensure_router_stack()
    if s_ok:
        print("[ OK ]")
    else:
        print(f"[ CẢNH BÁO ] ({s_msg})")

    print("\n✓ Hoàn tất chuẩn bị! Đang khởi động Web Dashboard...\n")
    return True
