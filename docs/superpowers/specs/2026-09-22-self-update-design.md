# Self-update cho 9router Patch Manager — Thiết kế

Ngày: 2026-09-22
Trạng thái: chờ duyệt

## Mục tiêu

Cho file `9router-patch.exe` tự biết mình lỗi thời và tự thay chính nó bằng bản mới
tải từ hosting riêng, mà không cần người dùng tải lại thủ công.

Hai đường vào cùng một cơ chế:

1. **Chạy nền** — thread nền kiểm tra mỗi 5 phút; có bản mới thì tải về và hoán đổi
   file ngay, không hỏi, không ngắt router đang chạy.
2. **Thủ công** — nút "Kiểm tra ngay" trên trang `/update` chạy đúng pipeline đó
   theo yêu cầu người dùng.

Công tắc bật/tắt tính năng chạy nền nằm trên trang `/update`, lưu bền vào
`%APPDATA%\9router-patch\settings.json`.

## Ngoài phạm vi

- Không tự build từ source. Người dùng build sẵn và upload file lên hosting.
- Không tự khởi động lại tiến trình sau khi cập nhật.
- Không ký số (code signing) file exe. Xác thực bằng SHA256 trong `version.json`.
- Không delta update. Tải nguyên file ~18 MB.

## Bố cục hosting

```
https://phmyhu1710.dev/9router-patcher/
├── version.json
└── files/
    ├── 9router-patcher-v2.0.0.exe
    └── 9router-patcher-v2.0.1.exe
```

`version.json`:

```json
{
  "version": "2.0.1",
  "url": "https://phmyhu1710.dev/9router-patcher/files/9router-patcher-v2.0.1.exe",
  "sha256": "<64 hex ký tự>",
  "changelog": "Sửa anchor ua-messages, thêm flag --force"
}
```

`sha256` và `changelog` là tùy chọn. Thiếu `sha256` thì bỏ qua bước xác thực
(ghi log cảnh báo). Thiếu `url` thì coi như `version.json` hỏng, bỏ qua chu kỳ đó.

File exe tên có version để không đè bản cũ trên hosting — rollback chỉ là sửa
`version.json` trỏ về file cũ.

## Cấu hình

File mới `config.py`:

```python
UPDATE_BASE_URL = "https://phmyhu1710.dev/9router-patcher"
VERSION_CHECK_URL = f"{UPDATE_BASE_URL}/version.json"
AUTO_UPDATE_INTERVAL_SECONDS = 300
```

URL cố định trong code. Người dùng không nhập URL từ giao diện.

## Module mới: `self_update.py`

Toàn bộ vòng đời cập nhật nằm trong module này. Không đụng `updater.py` — module đó
lo pipeline npm/`9router`, đây là pipeline của chính file exe.

### API

```python
check_update() -> dict | None
    # GET version.json (timeout 5s). Trả None nếu lỗi mạng/JSON hỏng.
    # Trả {"version", "url", "sha256", "changelog", "has_update": bool}

download_and_swap(meta: dict, emit=None) -> dict
    # Tải về, xác thực SHA256, hoán đổi file. Trả {"ok": bool, "error": str}

state() -> dict
    # Snapshot trạng thái cho UI: {checked_at, remote_version, has_update,
    #                              phase, error, applied_version}

set_enabled(on: bool) -> None   /   is_enabled() -> bool
    # Đọc/ghi settings.json
```

`phase` nhận một trong: `"idle"`, `"checking"`, `"downloading"`, `"ready"`,
`"error"`, `"dev-mode"`.

### So sánh phiên bản

Tách theo `.`, ép `int`, so sánh tuple. Khác độ dài thì pad bằng 0 để `"2.0"` bằng
`"2.0.0"`. Chuỗi không parse được thành số → coi là không có bản mới, ghi log.

### Tải và xác thực

1. Tải về `<thư_mục_exe>/9router-patch.new` theo từng khối 64 KB.
2. Băm SHA256 trong lúc ghi, so với `meta["sha256"]`.
3. Lệch → xóa file tạm, trả lỗi. Khớp hoặc không khai báo → sang bước hoán đổi.

Ghi vào thư mục chứa exe, không ghi `%TEMP%` — file tạm phải nằm cùng ổ đĩa với
đích để `os.replace` là thao tác rename, không phải copy xuyên ổ.

### Hoán đổi file

Đã đo trực tiếp trên Windows 11: `os.replace()` **thành công** trên file exe đang
chạy. Windows chặn xóa file đang chạy, nhưng cho phép đổi tên. Nhờ vậy không cần
batch script, không cần khởi động lại, không cần cơ chế `pending-flag`.

```
os.replace(exe, exe_dir / f"9router-patch.old-{int(time.time())}")
os.replace(exe_dir / "9router-patch.new", exe)
```

Tên file cũ có hậu tố timestamp vì tiến trình đang chạy vẫn giữ handle trên nó —
lần cập nhật sau trong cùng phiên chạy sẽ không đè được tên cố định.

Không chạy được khi `app_paths.is_frozen()` là False (chế độ dev): đặt `phase` =
`"dev-mode"`, không tải, không hoán đổi.

### Dọn rác lúc khởi động

`cleanup_old_files()` gọi một lần lúc boot: xóa mọi `9router-patch.old-*` và
`9router-patch.new` còn sót. Bỏ qua `PermissionError` (file cũ của tiến trình
khác đang chạy) — lần khởi động sau dọn tiếp.

## Lưu cài đặt

`%APPDATA%\9router-patch\settings.json` qua `app_paths.get_app_data_dir()`:

```json
{ "auto_update": true }
```

Mặc định `true` khi file chưa tồn tại hoặc đọc lỗi. Ghi bằng
`Path.write_text()` vào file tạm rồi `os.replace` để không hỏng file khi ghi dở.

## Thread nền

Thread daemon riêng `_auto_update_worker()`, khởi động trong `_lifespan` cạnh
`_snap_worker` hiện có. Không gộp vào `_snap_worker`: vòng lặp đó bị bỏ qua khi
`CLIENT_IDLE_AFTER` không có ai xem web, mà yêu cầu là chạy nền bất kể có người xem
hay không.

```python
while not stop.is_set():
    if is_enabled():
        meta = check_update()
        if meta and meta["has_update"]:
            download_and_swap(meta)
    stop.wait(AUTO_UPDATE_INTERVAL_SECONDS)
```

- `stop` là `threading.Event`, set lúc shutdown — thoát sạch, không cần chờ hết 300s.
- Một `threading.Lock` không-chặn trong `self_update` chặn hai lần tải chồng nhau
  (nút thủ công bấm đúng lúc thread nền đang chạy).
- Không jitter. Một máy cá nhân, không phải fleet.
- Mọi exception bắt tại chỗ, ghi `boot_doctor.log_boot`, tick sau thử lại. Thread
  không bao giờ chết.

## Giao diện web

Thêm panel vào `templates/update.html`, đặt trên panel "Chạy update" hiện có:

```
┌─ 9router Patch Manager ──────────────────────────────┐
│  v2.0.0          →          v2.0.1    [đã tải]       │
│                                                       │
│  [x] Tự động kiểm tra bản mới mỗi 5 phút (chạy nền)   │
│                                                       │
│  [ Kiểm tra ngay ]                                    │
└───────────────────────────────────────────────────────┘
```

Dùng lại `.switch` và `.badge` có sẵn trong `base.html`. Không thêm CSS mới.

Trạng thái hiển thị theo `phase`:

| phase | badge |
|---|---|
| `idle` + không có bản mới | `ok` — đã mới nhất |
| `idle` + có bản mới | `info` — có bản mới vX.Y.Z |
| `downloading` | `warn` — đang tải… |
| `ready` | `ok` — đã tải vX.Y.Z, áp dụng từ lần mở tới |
| `error` | `bad` — kèm text lỗi |
| `dev-mode` | `muted` — chạy từ source, bỏ qua |

### Route mới

```python
POST /update/self        (CSRF)   # kiểm tra + tải + hoán đổi ngay
POST /settings/auto-update (CSRF) # {"enabled": true|false}
```

Cả hai trả JSON. `/update/self` chiếm `OP_SLOT` để không chạy song song với job
npm update.

`update_page()` truyền thêm `self_update=self_update.state()` vào context.

## Sửa `build_app.py`

Ngoài 5 flag version đã thêm, sau khi build xong copy artifact để upload:

```python
shutil.copyfile(out_exe, out_exe.with_name(f"9router-patcher-v{version.APP_VERSION}.exe"))
```

Tên cài đặt cục bộ vẫn là `9router-patch.exe` — chỉ tên file để upload có version.
Không đổi `--output-filename`, tránh đụng vào đường dẫn mà cơ chế hoán đổi dựa vào.

## Kiểm thử

`tests/test_self_update.py` (mới):

- So sánh phiên bản: `2.0.0 < 2.0.1`, `2.0` == `2.0.0`, chuỗi rác không crash.
- `check_update()` parse `version.json` hợp lệ / thiếu `url` / JSON hỏng / lỗi mạng → `None`.
- `download_and_swap()` với HTTP giả: SHA256 khớp → hoán đổi; lệch → xóa file tạm,
  `ok=False`, exe gốc không đổi.
- `download_and_swap()` khi không frozen → không đụng file, `phase="dev-mode"`.
- `cleanup_old_files()` xóa file `.old-*`, bỏ qua `PermissionError`.
- `set_enabled(False)` rồi đọc lại → `False`. File thiếu → mặc định `True`.
- Worker: `is_enabled()` trả False → không gọi `check_update` (mock đếm).

`tests/test_web.py` (mở rộng):

- `POST /settings/auto-update` đổi switch, trả JSON.
- `POST /settings/auto-update` thiếu Origin → 403 (đã có `CSRF`).
- `POST /update/self` khi `OP_SLOT` đang bận → 409.
- Trang `/update` render panel mới với version hiện tại.

Test gọi mạng thật đều bị chặn như `test_web.py` hiện tại — không có HTTP thật
trong suite.

## Rủi ro đã biết

- **Hosting shared chết hoặc chậm** — `check_update()` timeout 5s, thất bại im lặng,
  tick sau thử lại. Không ảnh hưởng chức năng chính.
- **`version.json` bị sửa trái phép** — kẻ tấn công đổi `url` trỏ sang file exe độc.
  Giảm thiểu: SHA256 trong cùng `version.json` không bảo vệ được (cùng nguồn bị sửa).
  Rủi ro chấp nhận: công cụ nội bộ, chạy localhost, hosting do người dùng kiểm soát.
  Nâng cấp khi cần: ký `version.json` bằng khóa riêng.
- **Tải dở dang** — file `.new` sót lại bị lần khởi động sau dọn. Không bao giờ được
  hoán đổi khi chưa qua bước xác thực.
