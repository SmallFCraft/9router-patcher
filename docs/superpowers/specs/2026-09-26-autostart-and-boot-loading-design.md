# Design Spec: Auto Start Windows & Console Download Loading

**Ngày:** 2026-09-26  
**Mục tiêu:**
1. Thêm tính năng khởi động cùng Windows (Auto Start) kèm nút gạt bật/tắt trên Web UI, tự ẩn về khay hệ thống (tray) khi Windows khởi động.
2. Nâng cấp console terminal (`boot_doctor.py`) khi mở file exe: hiển thị spinner animation và tiến trình tải exe (% / MB tải về) trực quan, tránh cảm giác treo/đơ máy.

---

## 1. Tính năng 1: Khởi động cùng Windows (Auto Start)

### 1.1. Cơ chế Registry Windows
- Tạo module mới `autostart.py`:
  - Dùng thư viện chuẩn `winreg` (stdlib, không thêm dependency).
  - Vị trí registry: `HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Run`.
  - Tên value: `"9RouterPatchManager"`.
  - Giá trị value: `"\"<path_to_exe>\" --tray"`.
  - Ghi vào `HKCU` nên **không cần quyền Administrator / UAC prompt**.
- Hàm cốt lõi:
  - `is_supported() -> bool`: Kiểm tra môi trường đóng gói qua `app_paths.is_frozen()` và `sys.platform == "win32"`. Nếu đang chạy source `.py` thì trả về `False`.
  - `is_enabled() -> bool`: Đọc từ registry key xem value có tồn tại và trỏ đúng đường dẫn không. Nếu chạy dev `.py` thì trả `False`.
  - `set_enabled(on: bool) -> tuple[bool, str]`:
    - Nếu `on=True`: Mở key với `KEY_SET_VALUE`, ghi đường dẫn exe hiện tại kèm tham số `--tray`.
    - Nếu `on=False`: Xóa value khỏi key nếu tồn tại (bỏ qua `FileNotFoundError`).
    - Dev mode: Trả về `(False, "Chỉ khả dụng trên bản build exe")`.

### 1.2. Hỗ trợ tham số `--tray` trong `app.py`
- Cập nhật `_parse_args()` trong `app.py`:
  - Thêm argument `--tray`: `parser.add_argument("--tray", action="store_true", help="Khởi động ẩn vào khay hệ thống")`.
- Luồng khởi động khi có `--tray`:
  - Sau khi `boot_doctor.run_doctor()` hoàn tất và uvicorn khởi động, nếu `args.tray` được bật và `tray.available()`:
    - Gọi ngay `_hide_console()`.
    - Bỏ qua `_open_browser_when_ready` — không mở browser. Chỉ để icon khay.

### 1.3. Web UI & Backend Endpoint
- **Endpoint (`main.py`)**:
  - `POST /settings/auto-start`, áp dụng `CSRF` dependency (`same_origin`).
  - Request body form: `enabled="1" | "0"`.
  - Gọi `autostart.set_enabled(on)`.
  - Response JSON: `{"ok": bool, "auto_start": bool, "supported": bool, "error": str | None}`.
- **Route `GET /update`**:
  - Bổ sung vào context truyền sang `templates/update.html`:
    - `"auto_start_enabled": autostart.is_enabled()`
    - `"auto_start_supported": autostart.is_supported()`
- **Giao diện `templates/update.html`**:
  - Đặt dưới switch tự động cập nhật nền:
    ```html
    <div style="margin-bottom:.8rem">
      <label class="switch">
        <input type="checkbox" id="auto-start-toggle"
               {% if auto_start_enabled %}checked{% endif %}
               {% if not auto_start_supported %}disabled{% endif %}>
        Khởi động cùng Windows (chạy ẩn khay hệ thống)
      </label>
      {% if not auto_start_supported %}
        <span class="small muted" style="margin-left:.4rem">(Chỉ hỗ trợ trên bản exe)</span>
      {% endif %}
    </div>
    ```
  - Javascript:
    - Bắt sự kiện `change` của `#auto-start-toggle`.
    - Gửi fetch `POST /settings/auto-start`.
    - Thông báo toast: `"Đã bật khởi động cùng Windows"` / `"Đã tắt khởi động cùng Windows"`.

---

## 2. Tính năng 2: Hiệu ứng Loading Console khi mở file EXE (`boot_doctor.py`)

### 2.1. Nâng cấp `self_update.py`
- Bổ sung tham số `on_progress: Callable[[int, int], None] | None = None` vào `download_and_swap(meta, current_exe=None, on_progress=None)`:
  - Khi mở kết nối HTTP `urllib.request.urlopen`, đọc header `Content-Length` (nếu có) để xác định `total_bytes`.
  - Trong vòng lặp `while True: chunk = resp.read(65536)`:
    - Tích lũy `downloaded_bytes += len(chunk)`.
    - Nếu có `on_progress`: gọi `on_progress(downloaded_bytes, total_bytes)`.

### 2.2. Nâng cấp `console_ui.py`
- Bổ sung hàm `download_run(label: str, downloader: Callable, log_fn=None) -> tuple[bool, str]`:
  - Tái sử dụng bảng mã icon braille spinner `["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]`.
  - Nếu `stdout.isatty()` (TTY console thật):
    - Vẽ 1 dòng động cập nhật bằng `\r`:
      `│ ⠋ Đang tải bản cập nhật... [=======>     ] 54% (9.8MB / 18.2MB)`
    - Tự động co giãn theo `width()`.
    - Trường hợp không có `total_bytes` (`Content-Length` thiếu): hiển thị `(9.8MB đã tải)`.
  - Nếu không phải TTY (pipe / redirect file log):
    - Không dùng ký tự `\r`.
    - In mốc tiến trình sạch sẽ mỗi 25%: `25%`, `50%`, `75%`, `100%`.
  - Kết thúc in dấu `✓` màu xanh nếu thành công hoặc `✗` màu đỏ nếu thất bại.

### 2.3. Tích hợp vào `boot_doctor.py`
- Tại Bước `[1/5] Kiểm tra bản cập nhật`:
  - Khi phát hiện `meta["has_update"] == True`:
    - Thay vì gọi trực tiếp `self_update.download_and_swap(meta)` trong im lặng, chuyển sang bọc qua `console_ui.download_run()`:
      ```python
      def _do_download(on_progress=None):
          return self_update.download_and_swap(meta, on_progress=on_progress)
      
      res = console_ui.download_run(f"Tải bản cập nhật v{meta['version']}", _do_download, log_boot)
      ```
    - Nếu thành công: in `console_ui.step_end(...)` và gọi `restart_self()`.

---

## 3. Kế hoạch kiểm thử (Test Plan)

1. `tests/test_autostart.py` (Mới):
   - Test `is_supported()` trả về `False` khi `is_frozen() == False`.
   - Test `is_enabled()` đọc đúng từ fake `winreg`.
   - Test `set_enabled(True)` ghi đúng key registry kèm flag `--tray`.
   - Test `set_enabled(False)` xóa key thành công mà không gây lỗi nếu key chưa từng tồn tại.
2. `tests/test_web.py`:
   - Test `POST /settings/auto-start` bật/tắt và trả về kết quả JSON hợp lệ.
   - Test `GET /update` render đúng thuộc tính `auto_start_enabled` và `auto_start_supported`.
3. `tests/test_console_ui.py`:
   - Test `download_run()` render progress bar và spinner đúng trên TTY.
   - Test `download_run()` trên môi trường non-TTY không in escape `\r` và log sạch.
4. `tests/test_self_update.py`:
   - Test `download_and_swap()` gọi đúng callback `on_progress` với số bytes tải thực tế.
5. `tests/test_boot_doctor.py`:
   - Test bước 1 gọi `download_run` khi có bản cập nhật mới.

---

## 4. Tác động & Tương thích
- Không vi phạm các ràng buộc bảo mật (không log key/token, không đụng `data.sqlite`).
- Hoàn toàn dùng standard library của Python trên Windows (`winreg`, `ctypes`, `urllib`), không tăng kích thước build Nuitka hay thêm package ngoài.
