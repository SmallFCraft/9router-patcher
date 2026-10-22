"""Automated build script for 9router Patch Manager binary executable.

Quy trình:
1. Mã hóa patches.toml -> assets/patches.enc
2. Chạy Nuitka biên dịch app.py thành dist/9router-patch.exe
3. Stage: dist/9router-patch-v<ver>.exe + dist/version.json (upload 2 file này)
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import config
import version

ROOT = Path(__file__).resolve().parent


def sha256_file(path: Path) -> str:
    """Băm SHA256 theo khối 1 MB — file ~18 MB, đọc một lần bằng read_bytes() cũng được
    nhưng khối giữ RAM phẳng nếu sau này artifact phình to."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_version_manifest(dist: Path, artifact: Path) -> tuple[Path, dict]:
    """Sinh `version.json` để upload — người dùng không phải tự chạy certutil.

    Hosting: exe + version.json CÙNG CẤP trong dist/ (không có files/).
    Cảnh báo: mọi exe đã phát hành compile cứng UPDATE_BASE_URL/version.json.
    Bỏ bản này khỏi hosting = các máy cũ mất update vĩnh viễn.

    Trả về (path, manifest) — sha256 tính một lần, caller dùng lại từ manifest.
    """
    manifest = {
        "version": version.APP_VERSION,
        "url": f"{config.UPDATE_BASE_URL}/{artifact.name}",
        "sha256": sha256_file(artifact),
        "changelog": version.APP_CHANGELOG,
    }
    payload = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    out = dist / "version.json"
    out.write_text(payload, encoding="utf-8")
    return out, manifest


def win_file_version() -> str:
    """APP_VERSION → chuỗi 4 số cho Nuitka --file-version/--product-version.
    3 số thì pad .0 (2.2.4 → 2.2.4.0); 4 số giữ nguyên (2.2.4.1)."""
    parts = version.APP_VERSION.split(".")
    return version.APP_VERSION + (".0" if len(parts) == 3 else "")


def prune_stale_artifacts(folder: Path, keep: set[str],
                          pattern: str) -> int:
    """Xóa artifact cũ khớp `pattern` trong `folder` — chỉ giữ bản vừa build.
    Pattern bắt buộc: mỗi thư mục staging có phạm vi dọn riêng (dist/ phải chỉ khớp exe,
    không quét cả version.json / kilo-models.json / build-report.xml).
    Không dọn trước khi build: build lỗi vẫn còn bản cũ để upload.
    """
    removed = 0
    for f in folder.glob(pattern):
        if f.is_file() and f.name not in keep:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    return removed


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
        "wsproto",        # websocket protocol phụ, app không có websocket
        "yaml",           # chỉ cho starlette OpenAPI response
        "rich",           # pydantic lazy import in đẹp schema debug, app không dùng
        "pygments",       # rich kéo theo: 321 C source files (47% tổng khối lượng biên dịch!)
    ])
    # Máy 16 cores => cấp N-2 jobs để compile/check C files song song
    jobs = max(1, (os.cpu_count() or 4) - 2)
    fver = win_file_version()
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
        f"--file-version={fver}",
        f"--product-version={fver}",
        "--product-name=9router Patch Manager",
        "--company-name=9router-patcher",
        f"--report={output_dir / 'build-report.xml'}",
    ]
    if fast:
        # Dev mode: skip ~50s zstd compression; exe phình lên ~97MB nhưng build nhanh
        cmd.append("--onefile-no-compression")
    if ico_source.is_file():
        cmd.append(f"--windows-icon-from-ico={ico_source}")
        # Icon tray đọc từ file lúc chạy (tray._load_icon): không đóng gói thì
        # frozen mode tìm không thấy assets/app.ico → tray hiện icon Windows mặc định.
        cmd.append(f"--include-data-files={ico_source}=assets/app.ico")
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
    """Exe của lần build trước còn chạy sẽ giữ file: Nuitka onefile bootstrap unlink fail
    WinError 5 ở dist/, copy staging Permission denied ở dist/. Kill theo ĐÚNG image
    name — KHÔNG dùng /T (tree-kill giết cả process tree: router node.exe + headroom
    là con detached của exe → mỗi lần build tắt luôn 9router), cũng KHÔNG dùng
    /FI "WINDOWTITLE ..." (nó khớp cả cửa sổ File Explorer và kill luôn explorer.exe,
    đã xảy ra thật 2026-09-21). /F /IM đủ: parent+child onefile cùng image name."""
    if not exe.is_file() or not _exe_locked(exe):
        return
    subprocess.run(["taskkill", "/F", "/IM", exe.name],
                   shell=False, capture_output=True, text=True)
    # onefile parent+child giải phóng handle KHÔNG đồng bộ: taskkill xong mà file vẫn
    # "locked" thêm vài chục ms (repro build 2.2.6: probe ngay -> vẫn giữ -> copy fail
    # -> prune dọn bản cũ rồi giữ bản lỗi). Chờ rồi mới phán quyết.
    for _ in range(20):                  # tổng ~2s
        if not _exe_locked(exe):
            print(f"  đã tắt {exe.name} còn chạy (đang giữ file để build)")
            return
        time.sleep(0.1)
    print(f"  CẢNH BÁO: {exe.name} vẫn bị giữ — build sẽ lỗi WinError 5. "
          f"Đóng app rồi chạy lại.")


def build(fast: bool = False) -> int:
    # Console Windows mặc định cp1252/cp437: in tiếng Việt sẽ crash UnicodeEncodeError
    # giữa lúc build. Ép UTF-8 một lần ở đây thay vì vá từng câu print.
    try:
        for stream in (sys.stdout, sys.stderr):
            stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    dist = ROOT / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    _release_dist_exe(dist / "9router-patch.exe")

    # Nuitka ép CLCACHE_NODIRECT=1 (SconsCaching.enableClcache) khi biến chưa có. No-direct
    # mode chạy `cl /EP` trên TỪNG file để băm preprocessed source TRƯỚC khi tra cache — đo
    # 2026-10-19: 360 file warm-cache vẫn tốn 170s dù 360/360 hit. Direct mode dùng manifest
    # (băm include file thay vì preprocess toàn bộ) → cùng 360/360 hit chỉ còn 94s. Đặt =0
    # trước khi spawn Nuitka; Nuitka tôn trọng biến đã có (chỉ set khi "not in os.environ").
    # Đánh đổi: manifest bỏ sót include đổi nội dung mà không đổi mtime/size (hiếm); xóa
    # %LOCALAPPDATA%\Nuitka\Nuitka\Cache\clcache nếu nghi cache trả object cũ.
    os.environ.setdefault("CLCACHE_NODIRECT", "0")
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
        versioned = dist / f"9router-patch-v{version.APP_VERSION}.exe"

        copied = False
        try:
            _release_dist_exe(versioned)
            shutil.copyfile(exe, versioned)
            copied = True
        except OSError as e:
            print(f"CẢNH BÁO: không copy được artifact có version: {e}")

        if not copied:
            print(f"LỖI: {versioned.name} chưa được stage — không upload được bản này. "
                  f"Fix lock rồi chạy lại build.")
            return 1
        # Chỉ dọn sau khi build THÀNH CÔNG: giữ đúng bản vừa build + version.json.
        manifest_path, manifest = write_version_manifest(dist, versioned)
        removed = prune_stale_artifacts(dist, {versioned.name, exe.name},
                                        pattern="9router-patch*.exe")
        if removed:
            print(f"Đã dọn {removed} exe cũ trong dist/")
        # Layout mới bỏ hẳn thư mục files/ — 2 file upload nằm thẳng trong dist/.
        legacy = dist / "files"
        if legacy.is_dir():
            shutil.rmtree(legacy, ignore_errors=True)

        size_mb = exe.stat().st_size / (1024 * 1024)
        print(f"\nEncrypt: {t1 - t0:.1f}s | Nuitka: {t2 - t1:.1f}s | Total: {t2 - t0:.1f}s")
        print(f"SUCCESS: Built {exe} ({size_mb:.1f} MB)")
        vsize = versioned.stat().st_size / (1024 * 1024)
        print(f"Upload 2 file trong dist/ lên {config.UPDATE_BASE_URL}")
        print(f"  1. {versioned.name} ({vsize:.1f} MB)")
        print(f"  2. {manifest_path.name}")
        print(f"SHA256: {manifest['sha256']}")
        return 0
    print(f"\nError: Expected output {exe} was not created!")
    return 1


if __name__ == "__main__":
    raise SystemExit(build(fast="--fast" in sys.argv[1:]))
