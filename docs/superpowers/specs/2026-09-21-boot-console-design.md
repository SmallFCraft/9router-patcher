# Boot Doctor Console & System Logs Design

**Date:** 2026-09-21  
**Status:** Approved  
**Topic:** Unified startup console terminal doctor with interactive A-to-Z setup, lifecycle console preservation, and dedicated `/logs` UI for troubleshooting.

---

## 1. Context & Motivation

When the compiled binary `dist/9router-patch.exe` is launched, it previously ran silently (`--windows-console-mode=attach`). If prerequisites like Node.js, npm, or global `9router` were absent, users experienced silent failures or unexplained errors with no feedback.

This design introduces:
1. A **Native Windows Console Terminal Doctor** that executes immediately at process launch (`app.py`), walks through an A-to-Z preflight checklist, diagnoses missing dependencies, interactively prompts to install global `9router` when missing, automatically applies pending patches, ensures services run, and reports completion before launching the browser.
2. An **interactive post-boot console controller** that stays open to display live status and lets users choose between opening the dashboard (`[Enter]`), opening the logs page (`[L]`), hiding the console window (`[H]`), or gracefully stopping the app (`[Q]`/`Ctrl+C`).
3. A **dedicated `/logs` web dashboard** aggregating startup doctor logs, update history (`update-history.jsonl`), runtime service logs, and recent exceptions with privacy filtering and a one-click "Copy for Dev" button.

---

## 2. Global Constraints & Privacy Rules

- **Host & Port binding:** Dashboard remains strictly on `127.0.0.1:20129`. Refuses external traffic.
- **Privacy rules (Inherited from CLAUDE.md):**
  - Logs UI must never render patch `find`/`replace`/`why` payloads.
  - Logs UI must never expose internal build filesystem paths (`server/*`, `node_modules/...`).
  - Logs UI must never expose lock file paths (only PID and executable name).
  - Logs UI must never expose `.env`, API keys, or read/modify `data.sqlite`.
- **Packaging:**
  - Nuitka binary compilation changes `--windows-console-mode=attach` to `--windows-console-mode=force` to guarantee console visibility upon double-click.
  - Zero new runtime external dependencies: Python standard library only (`ctypes`, `subprocess`, `urllib.request`, `shutil`).

---

## 3. Architecture & Subsystems

### Subsystem 1: Boot Doctor Module (`boot_doctor.py`)

Pure, modular helper responsible for preflight inspection and console UX.

**Inspection steps (Ordered):**
1. **Node.js & npm probe:**
   - Checks `shutil.which("node")` and `shutil.which("npm.cmd")` / `shutil.which("npm")`.
   - Checks version with `node --version`.
   - If missing: prints actionable diagnostic with nodejs.org LTS download link, pauses console (`input()`), and exits cleanly.
2. **9router Global Package probe:**
   - Probes `engine.install_dir()` and `updater.current_version()`.
   - If missing: prompts user `9router chưa được cài đặt. Cài đặt toàn cục qua npm ngay? (Y/n) [Y]: `.
   - If accepted: executes `npm install -g 9router@latest` while streaming output to the console.
   - If declined: warns user that patching engine will be disabled, continues to dashboard in view-only mode.
3. **Lock / EBUSY probe:**
   - Inspects cached locks or runs quick check. Warns if active locks might interfere.
4. **Auto-Patch Engine check:**
   - Inspects current patch state across `patches.toml`.
   - If unapplied or pending patches are detected: automatically calls `engine.apply()` for all patches, printing progress item by item.
   - If all 33/33 patches are already applied: prints `[OK] 33/33 Patches applied` instantly (Quick Preflight pass in <1s).
5. **Router Stack check:**
   - Probes port `20128` (router) and port `8787` (headroom).
   - If not listening: triggers background launch via `updater.start_router_stack()`.

**Ring Buffer Logging:**
- All diagnostic and setup events are recorded into an in-memory ring buffer (up to 500 lines) and persisted to `%APPDATA%/9router-patch/logs/boot.log`.

---

### Subsystem 2: Console Controller & Launcher (`app.py`)

1. **Preflight Execution:** Calls `boot_doctor.run_doctor()`.
2. **Web Server Boot:** Launches `uvicorn` in a dedicated daemon thread.
3. **Quick-pass / Auto-open:**
   - Once server answers on `127.0.0.1:20129`, opens default web browser.
4. **Console Interactive Loop (Stay Open):**
   - Keeps console open with instructions:
     ```text
     [Enter] Mở Dashboard  |  [L] Xem System Logs  |  [H] Ẩn Console  |  [Q] Thoát app
     ```
   - `[Enter]`: Invokes `webbrowser.open("http://127.0.0.1:20129")`.
   - `[L]`: Invokes `webbrowser.open("http://127.0.0.1:20129/logs")`.
   - `[H]`: Calls Windows API `ctypes.windll.user32.ShowWindow(ctypes.windll.kernel32.GetConsoleWindow(), 0)` to hide console into background.
   - `[Q]` or `Ctrl+C`: Gracefully shuts down uvicorn, releases port 20129, and exits.

---

### Subsystem 3: System Logs Dashboard (`main.py` + `templates/logs.html`)

1. **New Route `GET /logs`:**
   - Renders `templates/logs.html`.
   - Aggregates:
     - Boot Doctor logs (from memory/`boot.log`).
     - Update history runs (from `update-history.jsonl`).
     - App & Service logs (from `%APPDATA%/9router-patch/logs/`).
2. **New API Route `GET /api/logs`:**
   - Returns structured JSON of all logs for client-side live streaming or refreshing.
3. **UI Features:**
   - Matches the 8-bit cartoon pixel theme (`base.html`).
   - Copy button per log section.
   - **"Sao chép toàn bộ log gửi Dev"** (Copy All for Dev) button: compiles sanitized system diagnostics, versions, OS environment, and errors to clipboard.
   - Nav bar in `base.html` updated with link to `/logs`.

---

## 4. Test Strategy

1. **Unit tests in `tests/test_boot_doctor.py`:**
   - Node/npm probe success and missing branches.
   - 9router missing branch: simulated user input 'Y' triggers install, 'n' skips.
   - Auto-apply triggers when patches are clean.
2. **Web route tests in `tests/test_logs_route.py`:**
   - `GET /logs` returns 200 with HTML containing log blocks.
   - Privacy test: `GET /logs` never leaks `find`/`replace` keys, internal paths, or `.env`.
3. **E2E verification in `tests/test_e2e_binary.py`:**
   - Verifies compiled executable with `--windows-console-mode=force` boots, responds, and remains healthy.
