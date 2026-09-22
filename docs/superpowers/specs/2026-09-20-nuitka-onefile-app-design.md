# Nuitka Onefile App (dashboard-only) — Design Spec

> Spec này là kết quả của quá trình brainstorming (Architectural path) + 3 spike
> đã chạy thành công. Executor đọc spec này cùng plan.

**Ngày:** 2026-09-20
**Trạng thái:** Đã chốt với user, chờ duyệt spec trước khi viết plan.

## 1. Mục tiêu

Đóng gói 9router Patch Manager thành **một file `.exe` duy nhất**, double-click là chạy
dashboard, chia sẻ cho người khác dùng **không đưa source `.py`**, đồng thời làm cho việc
đọc ngược logic patch là khó nhất có thể trong giới hạn công cụ mã nguồn mở.

### Quyết định đã chốt (không thương lượng lại trong plan)

| # | Quyết định | Giá trị đã chốt |
|---|-----------|-----------------|
| 1 | Mục tiêu | **Cả hai**: 1 file chạy ngay + chống dịch ngược |
| 2 | `patches.toml` | Cách 1: **nhúng cứng vào binary**, không để file rời |
| 3 | Công cụ build | Phương án 1: **Nuitka onefile** (biên dịch C thật), loại PyInstaller |
| 4 | Phạm vi exe | Phạm vi 1 rút gọn: **dashboard-only**, không CLI `locate` |
| 5 | Máy người nhận | Phương án 1: đã có Node + `npm i -g 9router` + headroom; exe chỉ quản lý |
| 6 | Bảo vệ `patches.toml` | **Mã hóa AES-GCM**, key giấu trong code biên dịch, giải mã trong RAM, không ghi đĩa |

## 2. Kết quả spike (bằng chứng thực nghiệm, không phải giả định)

Chạy ngày 2026-09-20 trên máy dev (Windows 11, Python 3.14.3, MSVC 14.51, Nuitka 4.2.1):

| Spike | Lệnh build | Kết quả |
|-------|-----------|---------|
| S1. Nuitka 4.2.1 hỗ trợ Python 3.14 | `pip download nuitka` → check `setup.py` classifiers | ✅ `Programming Language :: Python :: 3.14` có mặt |
| S2. `__file__` trong onefile | probe in `__file__`, `sys.argv[0]`, `sys.executable` | `__file__` → `%TEMP%\onefile_<pid>_<rand>\spike_probe.py`; `sys.argv[0]` → đường dẫn exe thật; `sys.frozen` là `None` (**không dùng để detect**) |
| S3. `--include-data-files` | `data.txt=data.txt` → exe chạy từ cwd khác | ✅ file resolve cạnh `__file__` trong temp dir |
| S4. `cryptography` (AESGCM) qua Nuitka | encrypt→decrypt trong exe | ✅ `CRYPTO-OK`, exit 0 |
| S5. FastAPI+uvicorn+Jinja2 onefile (SAME dir, không `--deployment`) | build `app_spike.py` import `lib_mod` + `Jinja2Templates` cùng dir, `--include-data-dir=tpl=tpl`; HTTP GET `/` từ tiến trình khác | ✅ **200 + template render `HELLO-9router-MARKER`** — FastAPI stack chạy 1 file bình thường |
| S6. MSVC toolchain | check `VC/Tools/MSVC/14.51.36231` | ✅ `cl 14.5`, không cần cài thêm |

> **Corrigendum S5 lần 1:** bản probe đầu (`probe_app.py` import `main` từ `D:\Codes\9router`
> khác thư mục) fail `ModuleNotFoundError: No module named 'fastapi'`. NGUYÊN NHÂN THẬT:
> Nuitka follow import tĩnh từ file build, không tìm được `main.py` ở ngoài cwd; **KHÔNG**
> phải do thiếu `--deployment`. Probe S5 đúng (cùng dir) đã chứng minh `--deployment` là
> mặc định và KHÔNG cần thiết. Build plan dùng cú pháp chuẩn không có `--no-deployment`.

**Kết luận từ spike:** Nuitka onefile mặc định đã đủ deployment (thấy site-packages,
Jinja2 template nhúng qua `--include-data-dir`). Chỉ cần build target nằm CÙNG thư mục
với source import (đây là tự nhiên: `app.py` ở root repo import `main.py`/`engine.py`/
`updater.py` cùng root). Bước verify trong plan phải chạy exe thật + HTTP GET `/` từ
tiến trình khác.

## 3. Kiến trúc

```
┌─────────────────────────────────────────────────┐
│ 9router-patch.exe (Nuitka onefile, ~15-25 MB)   │
│  ┌──────────┐  ┌──────────┐  ┌───────────────┐  │
│  │ main.py  │  │engine.py │  │ updater.py    │  │
│  │ ( Pascal │  │ (byte-ex │  │ (pipeline +   │  │
│  │ compiled)│  │ compiled)│  │  stack mgmt)  │  │
│  └──────────┘  └──────────┘  └───────────────┘  │
│  ┌──────────────────────────────────────────┐   │
│  │ patches.enc (AES-GCM, giải mã trong RAM) │   │
│  │ templates/*.html (data files)            │   │
│  └──────────────────────────────────────────┘   │
└─────────────────────────────────────────────────┘
        │ quản lý (không chứa)          ┌──────────────────┐
        ├──────────────────────────────▶│ npm 9router      │
        │ node/custom-server.js         │ router :20128    │
        ├──────────────────────────────▶│ headroom.exe     │
        │ relaunch verbatim cmdline     │ headroom :8787   │
        └──────────────────────────────▶└──────────────────┘
```

### 3.1 Entry point: `app.py` MỚI (file duy nhất thêm vào root)

`main.py` giữ nguyên cho dev (`python main.py` vẫn chạy). Nuitka build target là `app.py`:

```python
"""Launcher cho bản exe: mở dashboard trên 127.0.0.1:20129."""
import webbrowser
from main import app, HOST, PORT  # noqa: E402 — import sau docstring là cố ý

if __name__ == "__main__":
    import uvicorn
    webbrowser.open(f"http://{HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT)
```

`_log_config()` trong `main.py` (dict logging config) được reuse nguyên — plan KHÔNG viết
lại, chỉ import. Nếu import trực tiếp khó vì nó nằm trong block `__main__`, Task 1 di
chuyển nó ra module-level (hàm `_log_config()`, tên giữ nguyên) — đây là refactor an
toàn duy nhất chạm vào `main.py`.

Console mode: `--windows-console-mode=attach` — double-click không hiện console đen;
chạy từ terminal vẫn thấy log uvicorn. KHÔNG dùng `disable` (mất log chẩn đoán khi exe lỗi).

### 3.2 Mã hóa `patches.toml` (AES-256-GCM)

- Build-time (`tools/make_patches_blob.py`): đọc `patches.toml` (41 KB) → AES-GCM encrypt
  với key 32 byte random → ghi `assets/patches.enc` (nonce 12 byte prepend, ~41 KB).
- Runtime (`engine.py`): `load_patches()` thử đọc `patches.enc` GIẢI MÃ TRONG RAM trước;
  nếu không có file enc (dev chạy source) thì fallback đọc `patches.toml` như cũ.
  Tests hiện tại không đổi — chúng vẫn đi đường fallback.
- Key sống trong source đã biên dịch C (không trong file rời, không trong env).
  Mức bảo vệ trung thực: chống đọc Notepad/temp-dir, chống sửa patch lậu; KHÔNG chống
  được người dump RAM — spec này nói thẳng điều đó trong README, không hứa quá.

Thư viện: `cryptography` (đã có trên máy dev; thêm vào `requirements.txt` MỚI cùng
`nuitka`, `fastapi`, `uvicorn`, `jinja2` — repo hiện chưa có file này, pip install
đang làm thủ công theo README).

### 3.3 Đường dẫn: 3 loại, 3 cách resolve (kết quả spike S2)

| Loại | Ví dụ | Resolve trong exe | Ghi chú |
|------|-------|-------------------|---------|
| Read-only bundle | `templates/*.html`, `patches.enc` | cạnh `__file__` (temp dir onefile) — giữ code `HERE`/`__file__` nguyên | KHÔNG đụng gì |
| Writable app-data | `logs/`, `router-stack.json`, `update-history.jsonl` | `%APPDATA%/9router-patch/` — Task 2 thêm `APP_DATA` helper | Thay `RESTART_LOG_DIR`, `HISTORY_FILE` |
| Ngoài exe (giữ nguyên) | global npm install, backups `../9router-backups`, `handle64.exe` | `npm root -g`, `HERE.parent`, PATH — giữ nguyên | Backups vẫn ngoài exe để user tự backup/restore thủ công |

`BACKUP_ROOT = HERE.parent / "9router-backups"` trong exe sẽ resolve ra thư mục cha của
temp dir — SAI. Task 2 sửa thành: frozen (`__compiled__` in globals) → `Path(sys.argv[0]).parent.parent / "9router-backups"` (cạnh exe); dev → giữ nguyên.
Detect frozen bằng `"__compiled__" in globals()` (đã verify trong spike S2), KHÔNG dùng
`sys.frozen` (là `None` dưới Nuitka).

### 3.4 Tương thích updater (verified, không sửa code phát hiện)

- `_npm_cli()`: `[node, npm-cli.js]` qua `shutil.which` — không dính `sys.executable` ✅
- `_default_router_cmd()`: `node custom-server.js` — không dính ✅
- `restart_processes()`: relaunch cmdline đã capture (node/python gốc của máy nhận) — exe
  KHÔNG tự restart chính nó ✅
- `_default_headroom_cmd()`: `Path(sys.executable).parent / "Scripts" / "headroom.exe"`.
  Dưới Nuitka `sys.executable` → `%TEMP%\onefile_*\python.exe` — SAI. Task 2 sửa:
  thử `shutil.which("headroom")` / `headroom.exe` TRƯỚC, fallback logic cũ sau.
  (Đã verify: `headroom.exe` là MZ shim thật 108 KB, `--help` chạy độc lập, PATH có nó.)
- `handle64.exe`: `shutil.which` + fallback `E:\Apps\Tools\handle64.exe` — giữ nguyên;
  máy nhận thiếu thì lock-probe báo lỗi Chihuahua (đúng quyết định 5: exe chỉ quản lý).
- `updater.py` KHÔNG spawn `python -m engine` ở đâu (đã grep toàn repo: chỉ còn trong
  `.ps1` và comment) — dashboard-only không gãy pipeline ✅
- `9router-patch.ps1` giữ nguyên cho dev, không dùng để phát hành exe.

## 4. Build spec (lệnh chuẩn)

```bat
pip install -r requirements.txt
python tools\make_patches_blob.py
python -m nuitka --onefile ^
  --assume-yes-for-downloads ^
  --windows-console-mode=attach ^
  --include-data-dir=templates=templates ^
  --include-data-files=assets\patches.enc=patches.enc ^
  --output-dir=build ^
  --output-filename=9router-patch.exe ^
  app.py
```

Dùng cú pháp chuẩn KHÔNG có `--no-deployment` / `--no-package-data` (spike S5 đúng chứng
minh `--deployment` là mặc định, FastAPI stack boot bình thường cùng dir).
Build environment: Python 3.14.3 + Nuitka 4.2.1 + MSVC 14.51 — máy dev đã đủ, không cài thêm.

`.gitignore` thêm: `assets/patches.enc` (chứa ciphertext từ source — regenerate mỗi build,
không commit), `build/`, `*.exe`.

## 5. Verify (định nghĩa "xong" — exe thật, không phải unit test)

1. `dist\9router-patch.exe` double-click → browser tự mở `http://127.0.0.1:20129`, dashboard render.
2. HTTP GET `/` từ tiến trình khác → 200, có chữ `9router` (y hệt probe S5 đã làm).
3. Apply 1 patch từ UI → build npm bị sửa, `node --check` pass, backup nằm cạnh exe.
4. `%TEMP%\onefile_*` KHÔNG chứa `patches.toml` plaintext (chỉ `patches.enc`).
5. `python -m pytest tests/ -q` trên source vẫn xanh (không regression dev workflow).
6. Máy nhận (có Node+9router+headroom, không Python): exe chạy, update pipeline end-to-end.

## 6. Phạm vi LOẠI (nói rõ để plan không lan)

- Không CLI `locate` trong exe (user chốt dashboard-only; `engine.locate` vẫn sống trong source cho dev).
- Không tự cài Node/npm/9router/headroom (máy nhận có sẵn; thiếu thì báo lỗi Chihuahua).
- Không auto-update cho exe (đổi patch = build lại + gửi file mới; spec không hứa cơ chế update).
- Không chống dump-RAM (ghi rõ trong README, không hứa quá).
- Không installer/MSI, không code-sign cert (SmartScreen sẽ warn — ghi rõ trong README phát hành).
- Không đụng `engine.scan/apply/revert` core, không đụng `patches.toml` format.
- `docs/` đang bị `.gitignore` — spec + plan KHÔNG commit (giữ trong working tree để đọc).

## 7. Rủi ro đã biết

| Rủi ro | Giảm thiểu |
|--------|-----------|
| Nuitka onefile giải nén ra `%TEMP%` — user tưởng "1 file" nhưng runtime bung nhiều file | Nói rõ trong README; `patches.enc` mã hóa nên temp dir không lộ patch |
| SmartScreen warn (không sign) | Ghi rõ trong README phát hành; user bấm More info → Run anyway |
| Build lâu (~5-10 phút FastAPI stack) + exe ~15-25 MB | Chấp nhận; build 1 lần mỗi release patch |
| Nuitka version sau break Python mới | Ghim `nuitka==4.2.1` trong `requirements.txt` |
| Key AES trong binary bị trích bằng strings debt | Chấp nhận mức bảo vệ "chống sửa lậu", không hứa "chống NSA"; đã nói thẳng ở §3.2 |
