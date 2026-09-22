# Design Spec: 9router Target Version Alignment & Realtime Self-Update Notification

- **Date:** 2026-09-22
- **Status:** Approved
- **Scope:** Bounded (engine, boot doctor, web routes, Jinja templates, self-update client)

---

## 1. Problem Statement

1. **Version Drift in 9router Upstream**:
   - `patches.toml` contains 34 minified find/replace byte-exact patches designed and verified against 9router `v0.5.81`.
   - When upstream releases a newer version (e.g. `v0.5.85`), the minifier renames internal variables (e.g. `delete h["anthropic-dangerous-direct-browser-access"]` becomes unknown because `h` is changed to another single letter).
   - If a user runs a newer 9router (`v0.5.85`), applying patches will fail with `dead-anchor`, leaving 9router unpatched (causing SSE hangs, request timeouts, and proxy failure).
   - If a user runs an older 9router (e.g. `v0.5.79`), patches may partially apply or fail.
   - On `/update`, the only main action is `npm i -g 9router@latest`, which blindly pulls the newest breaking version.

2. **Self-Update UX in Web Dashboard**:
   - The daemon thread downloads the new executable in the background.
   - When downloaded (`phase == "ready"`), there is no prompt across the dashboard (`/`, `/update`, `/logs`) alerting the user to restart now or later.
   - There is no web endpoint to trigger `self_update.restart_self()` immediately from the browser.

---

## 2. Technical Architecture & Design

### Subsystem A: 9router Target Version Alignment

1. **Single Source of Configuration**:
   - In `config.py`: define `TARGET_9ROUTER_VERSION = "0.5.81"`.
   - In `patches.toml`: top-level metadata:
     ```toml
     target_version = "0.5.81"
     ```
   - In `engine.py`: add helper `target_version() -> str` that reads from `config.TARGET_9ROUTER_VERSION`.

2. **Compatibility Analysis (`updater.check_router_compatibility() -> dict`)**:
   - Compares installed 9router `local_version` against `TARGET_9ROUTER_VERSION`:
     - `compatible` (bool): `local_version == TARGET_9ROUTER_VERSION`.
     - `relation` (str): `"match"`, `"older"` (local < target), or `"newer"` (local > target).
     - `target_version`: `"0.5.81"`.
     - `local_version`: e.g. `"0.5.81"`, `"0.5.79"`, `"0.5.85"`, or `"unknown"`.

3. **Safe Align Actions in `updater.py`**:
   - `install_target_router(on_output=None) -> tuple[bool, str]`:
     Runs `npm install -g 9router@0.5.81` (explicit pinned target, NOT `@latest`).
   - `run_update(emit=None, target_pin=None, ...)`:
     If `target_pin` is provided (e.g. `"0.5.81"`), runs `npm i -g 9router@0.5.81` instead of `@latest`.

4. **Console Boot Doctor (`boot_doctor.py`)**:
   - Step `[2/5]` (Check 9router global):
     - If missing: prompt to install `9router@0.5.81`.
     - If `local < 0.5.81`: prompt `9router v{local} cũ hơn bản hỗ trợ (v0.5.81). Nâng cấp lên v0.5.81? (Y/n) [Y]`.
     - If `local > 0.5.81`: warning `[ CẢNH BÁO ] 9router v{local} mới hơn bản vá v0.5.81 — vào Dashboard để căn chỉnh về v0.5.81`.
   - Step `[3/5]` (Check & apply patches):
     - If `local > 0.5.81`: skip auto-apply, emit warning `[ BỎ QUA ] Version 9router không tương thích với bộ patch`.

5. **Web Dashboard (`/` and `templates/index.html`)**:
   - If incompatible (`local != target`):
     - Display warning banner:
       - If `older`: `9router đang ở v{local}, bản patch yêu cầu v{target}. [Nâng cấp lên v{target}]`.
       - If `newer`: `9router v{local} mới hơn bản hỗ trợ (v{target}). Việc áp dụng patch bị khóa để bảo vệ proxy. [Cài đặt lại v{target}]`.
     - Disable `Apply tất cả` and group `Apply` buttons when `local > target` (or provide override confirmation).
     - Route `POST /apply`: reject with 409 if `local > target` unless explicitly forced.
     - New route `POST /router/align-target`: runs `npm install -g 9router@{target}` with progress stream / toast.

6. **Update Page (`templates/update.html`)**:
   - Displays 3 badges in version flow: Local vs Target (`v0.5.81`) vs npm latest.
   - Primary action: **"Cài đặt phiên bản tương thích: `9router@0.5.81` và áp dụng patch"** (100% safe, no dead anchors).
   - Secondary action (collapsible/developer): **"Thử nâng cấp lên bản mới nhất `9router@latest`"** (runs with dry-run gate).

---

### Subsystem B: Realtime Update Notification Popup across All Pages

1. **Backend Endpoints**:
   - `GET /api/self-update/status`:
     Returns JSON `{"ok": True, "phase": su["phase"], "applied_version": su["applied_version"], "has_update": su["has_update"], "remote_version": su["remote_version"], "changelog": su["changelog"]}`.
   - `POST /update/self/restart`:
     Enforces Origin/Referer (localhost only).
     Spawns new detached process and exits via `self_update.restart_self()`.

2. **Frontend UI/UX Modal (`templates/base.html`)**:
   - Pixel-art styled dialog `#self-update-modal`:
     - Header: `🚀 ĐÃ CẬP NHẬT PHIÊN BẢN MỚI`
     - Body: `Đã tải thành công 9router Patch Manager v{applied_version}. Bạn có muốn khởi động lại ngay để áp dụng không?`
     - Actions:
       - `[ Khởi động lại ngay ]`: sends `POST /update/self/restart`, shows toast, stops polling.
       - `[ Để sau ]`: closes modal, stores dismissed version in `sessionStorage` (`_9r_dismissed_update`).
   - Poll worker in `base.html`: polls `/api/self-update/status` every 15s. If `phase === "ready"` and version not dismissed in `sessionStorage`, displays `#self-update-modal`.

---

## 3. Verification & Testing

- Unit tests for version comparison and compatibility in `tests/test_updater.py`.
- Unit tests for boot doctor target prompt in `tests/test_boot_doctor.py`.
- Integration tests for `POST /update/self/restart` and `GET /api/self-update/status` in `tests/test_web.py`.
- Regression tests for template rendering in `templates/index.html`, `templates/update.html`, and `templates/base.html`.
