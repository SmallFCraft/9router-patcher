# 9router Target Version Alignment & Realtime Self-Update Notification Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Lock patch engine to a tested target 9router version (`0.5.81`), offer flexible align/upgrade/downgrade flows instead of breaking anchors, and provide a global realtime notification popup with immediate restart when a new executable update is downloaded.

**Architecture:** 
- Declare `TARGET_9ROUTER_VERSION = "0.5.81"` in `config.py` and top of `patches.toml`.
- Add compatibility analyzer in `updater.py` and align command (`npm i -g 9router@0.5.81`).
- Prevent destructive `apply` in `boot_doctor.py` and `main.py` when running a newer unanchored 9router (`> 0.5.81`).
- Expose `GET /api/self-update/status` and `POST /update/self/restart`.
- Display popup modal in `templates/base.html` across all pages when update is ready, and update `templates/index.html` + `templates/update.html` with clear version alignment actions.

**Tech Stack:** Python 3.11+, FastAPI, Jinja2, vanilla JS, Nuitka.

**Spec:** `docs/superpowers/specs/2026-09-22-target-version-lock-and-update-popup-design.md`

## Global Constraints

- Never expose, log, or commit `.env` contents, API keys, or provider secrets.
- Never read or modify the proxy's `data.sqlite` directly from the dashboard.
- Local dashboard only accepts `127.0.0.1:20129`. Mutating POST requests enforce local `Origin`/`Referer`.
- UI privacy: dashboard never renders patch `find`/`replace`/`why`, internal file paths (`server/*`, build tree), or lock file paths.
- All tests must pass: `python -m pytest tests/ -q`.

---

### Task 1: Configuration and Target Version in `config.py`, `patches.toml`, `engine.py`

**Files:**
- Modify: `config.py`
- Modify: `patches.toml:1-20`
- Modify: `engine.py:30-70`
- Test: `tests/test_engine.py`

**Interfaces:**
- Produces: `config.TARGET_9ROUTER_VERSION: str = "0.5.81"`
- Produces: `engine.target_version() -> str`

- [ ] **Step 1: Write test in `tests/test_engine.py`**

```python
def test_target_version_configured_and_matches_patches_toml():
    import config, engine
    assert hasattr(config, "TARGET_9ROUTER_VERSION")
    assert config.TARGET_9ROUTER_VERSION == "0.5.81"
    assert engine.target_version() == "0.5.81"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_engine.py -k "test_target_version_configured" -q`
Expected: FAIL (missing `TARGET_9ROUTER_VERSION`).

- [ ] **Step 3: Implement `TARGET_9ROUTER_VERSION` in `config.py`, `patches.toml`, `engine.py`**

In `config.py`:
```python
TARGET_9ROUTER_VERSION = "0.5.81"
```

In `patches.toml`:
```toml
target_version = "0.5.81"
```

In `engine.py`:
```python
def target_version() -> str:
    import config
    return getattr(config, "TARGET_9ROUTER_VERSION", "0.5.81")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_engine.py -k "test_target_version_configured" -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add config.py patches.toml engine.py tests/test_engine.py
git commit -m "feat: define TARGET_9ROUTER_VERSION in config and engine"
```

---

### Task 2: Compatibility Check & Pinned Target Install in `updater.py`

**Files:**
- Modify: `updater.py`
- Test: `tests/test_updater.py`

**Interfaces:**
- Produces: `updater.check_router_compatibility(local_ver: str | None = None) -> dict`
- Produces: `updater.install_target_router(on_output=None) -> tuple[bool, str]`

- [ ] **Step 1: Write tests in `tests/test_updater.py`**

```python
def test_check_router_compatibility():
    import updater
    # Match
    res = updater.check_router_compatibility("0.5.81")
    assert res["compatible"] is True
    assert res["relation"] == "match"

    # Older
    res = updater.check_router_compatibility("0.5.79")
    assert res["compatible"] is False
    assert res["relation"] == "older"

    # Newer
    res = updater.check_router_compatibility("0.5.85")
    assert res["compatible"] is False
    assert res["relation"] == "newer"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_updater.py -k "test_check_router_compatibility" -q`
Expected: FAIL (`check_router_compatibility` not found).

- [ ] **Step 3: Implement compatibility helper and target install in `updater.py`**

```python
def check_router_compatibility(local_ver: str | None = None) -> dict:
    target = engine.target_version()
    current = local_ver or (current_version() if engine.install_dir().exists() else "unknown")
    if current == "unknown":
        return {"compatible": False, "relation": "unknown", "local": current, "target": target}
    import self_update
    curr_t = self_update.parse_version(current)
    targ_t = self_update.parse_version(target)
    if curr_t == targ_t:
        relation = "match"
    elif curr_t < targ_t:
        relation = "older"
    else:
        relation = "newer"
    return {
        "compatible": relation == "match",
        "relation": relation,
        "local": current,
        "target": target,
    }


def install_target_router(on_output=None) -> tuple[bool, str]:
    target = engine.target_version()
    cmd_base = _npm_cli()
    if not cmd_base:
        return False, "Không tìm thấy npm"
    cmd = cmd_base + ["install", "-g", f"9router@{target}"]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, text=True, bufsize=1, creationflags=SILENT_FLAGS)
        if proc.stdout:
            for line in iter(proc.stdout.readline, ""):
                c = line.strip()
                if c and on_output:
                    on_output(c)
            proc.stdout.close()
        proc.wait(timeout=300)
        return proc.returncode == 0, f"Cài đặt 9router@{target} {'thành công' if proc.returncode == 0 else 'thất bại'}"
    except Exception as e:
        return False, str(e)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_updater.py -k "test_check_router_compatibility" -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add updater.py tests/test_updater.py
git commit -m "feat(updater): add router compatibility analyzer and install_target_router"
```

---

### Task 3: Align Boot Doctor with Target 9router Version in `boot_doctor.py`

**Files:**
- Modify: `boot_doctor.py`
- Test: `tests/test_boot_doctor.py`

**Interfaces:**
- Consumes: `updater.check_router_compatibility`, `updater.install_target_router`
- Updates: `check_9router()`, `check_and_apply_patches()`

- [ ] **Step 1: Write tests in `tests/test_boot_doctor.py`**

```python
def test_check_and_apply_patches_skips_when_router_newer_than_target(monkeypatch):
    import boot_doctor, updater
    monkeypatch.setattr(updater, "check_router_compatibility",
                        lambda: {"compatible": False, "relation": "newer", "local": "0.5.85", "target": "0.5.81"})
    ok, msg = boot_doctor.check_and_apply_patches()
    assert ok is False
    assert "mới hơn bản vá" in msg
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_boot_doctor.py -k "skips_when_router_newer" -q`
Expected: FAIL

- [ ] **Step 3: Update `boot_doctor.py`**

- In `install_9router()`: target `f"9router@{engine.target_version()}"`.
- In `check_9router()`:
  - If `compat["relation"] == "older"`: prompt to upgrade to `target_version`.
  - If `compat["relation"] == "newer"`: warn that 9router is newer than target.
- In `check_and_apply_patches()`:
  - If `compat["relation"] == "newer"`: return `False, f"9router v{compat['local']} mới hơn bản vá (v{compat['target']}) — bỏ qua apply để tránh lỗi"`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_boot_doctor.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add boot_doctor.py tests/test_boot_doctor.py
git commit -m "feat(doctor): align 9router installation and patch checks to target version"
```

---

### Task 4: Self-Update API Routes (`GET status` & `POST restart`) in `main.py`

**Files:**
- Modify: `main.py`
- Test: `tests/test_web.py`

**Interfaces:**
- Route: `GET /api/self-update/status` -> `JSON {"ok": True, "phase": str, "applied_version": str, ...}`
- Route: `POST /update/self/restart` -> restarts app if frozen, returns 200

- [ ] **Step 1: Write tests in `tests/test_web.py`**

```python
def test_self_update_status_api(web, monkeypatch):
    import self_update
    monkeypatch.setattr(self_update, "state", lambda: {
        "phase": "ready", "applied_version": "2.1.7", "has_update": False,
        "remote_version": "2.1.7", "changelog": "fix stuff"
    })
    r = web["client"].get("/api/self-update/status")
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    assert j["phase"] == "ready"
    assert j["applied_version"] == "2.1.7"


def test_self_update_restart_route_enforces_csrf_and_calls_restart(web, monkeypatch):
    import self_update
    called = []
    monkeypatch.setattr(self_update, "restart_self", lambda: called.append(True))
    # No origin -> 403
    assert web["client"].post("/update/self/restart").status_code == 403
    # With origin -> 200 and calls restart_self
    r = web["client"].post("/update/self/restart", headers={"Origin": "http://127.0.0.1:20129"})
    assert r.status_code == 200
    assert len(called) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_web.py -k "self_update_status or self_update_restart" -q`
Expected: FAIL (404).

- [ ] **Step 3: Implement endpoints in `main.py`**

```python
@app.get("/api/self-update/status", include_in_schema=False)
def self_update_status():
    st = self_update.state()
    return JSONResponse({"ok": True, **st})


@app.post("/update/self/restart", dependencies=CSRF, include_in_schema=False)
def self_update_restart():
    # Spawns detached restart thread so the HTTP 200 returns before process exits
    threading.Thread(target=lambda: (time.sleep(0.5), self_update.restart_self()), daemon=True).start()
    return JSONResponse({"ok": True, "message": "Đang khởi động lại ứng dụng..."})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_web.py -k "self_update_status or self_update_restart" -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_web.py
git commit -m "feat(web): add self-update status API and restart endpoint"
```

---

### Task 5: 9router Target Alignment Route and Apply Lock in `main.py`

**Files:**
- Modify: `main.py`
- Test: `tests/test_web.py`

**Interfaces:**
- Route: `POST /router/align-target` -> runs `install_target_router()`
- Route: `POST /apply` -> blocks if `local_version > target_version` unless `force=1`

- [ ] **Step 1: Write tests in `tests/test_web.py`**

```python
def test_apply_blocked_when_router_is_newer_than_target(web, monkeypatch):
    import updater
    monkeypatch.setattr(updater, "check_router_compatibility",
                        lambda: {"compatible": False, "relation": "newer", "local": "0.5.85", "target": "0.5.81"})
    r = web["client"].post("/apply", headers={"Origin": "http://127.0.0.1:20129"})
    assert r.status_code == 409
    assert "mới hơn bản hỗ trợ" in r.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_web.py -k "test_apply_blocked_when_router_is_newer" -q`
Expected: FAIL.

- [ ] **Step 3: Implement in `main.py`**

- In `apply()`: check `compat = updater.check_router_compatibility()`. If `compat["relation"] == "newer"` and not `request.query_params.get("force")`: return `_error(request, f"9router v{compat['local']} mới hơn bản hỗ trợ (v{compat['target']}). Hãy hạ cấp về v{compat['target']} trước.")`.
- Add route `@app.post("/router/align-target", dependencies=CSRF)`: calls `updater.install_target_router()`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_web.py -k "test_apply_blocked_when_router_is_newer" -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_web.py
git commit -m "feat(web): guard /apply against newer 9router versions and add /router/align-target"
```

---

### Task 6: Realtime Update Notification Popup in `templates/base.html`

**Files:**
- Modify: `templates/base.html`
- Test: `tests/test_web.py`

**Interfaces:**
- Visual modal: `#self-update-modal` with restart and dismiss actions.
- Client logic: 15s poll on `/api/self-update/status`, `sessionStorage` dismiss memory.

- [ ] **Step 1: Write test for modal presence in `tests/test_web.py`**

```python
def test_base_template_has_self_update_modal(web):
    html = web["client"].get("/").text
    assert 'id="self-update-modal"' in html
    assert 'id="btn-self-update-restart"' in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_web.py -k "test_base_template_has_self_update_modal" -q`
Expected: FAIL.

- [ ] **Step 3: Add modal and polling script to `templates/base.html`**

Add CSS and HTML for `#self-update-modal`, and client script:
```javascript
// Poll /api/self-update/status every 15s
// If phase === 'ready', show modal if not dismissed for this version in sessionStorage
// On click restart: POST /update/self/restart
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_web.py -k "test_base_template_has_self_update_modal" -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add templates/base.html tests/test_web.py
git commit -m "feat(ui): add realtime update notification popup across all pages"
```

---

### Task 7: Update `templates/index.html` with Target Version Banner & Apply Guard

**Files:**
- Modify: `templates/index.html`
- Test: `tests/test_web.py`

**Interfaces:**
- In `index.html`: show warning banner and disable apply buttons if `local_version > target_version`.
- Action: "Cài đặt lại v{target_version}" button triggering `/router/align-target`.

- [ ] **Step 1: Write test in `tests/test_web.py`**

```python
def test_index_disables_apply_and_shows_warning_when_router_newer(web, monkeypatch):
    import updater
    monkeypatch.setattr(updater, "check_router_compatibility",
                        lambda: {"compatible": False, "relation": "newer", "local": "0.5.85", "target": "0.5.81"})
    html = web["client"].get("/").text
    assert "mới hơn bản hỗ trợ" in html
    assert "disabled" in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_web.py -k "test_index_disables_apply" -q`
Expected: FAIL.

- [ ] **Step 3: Update `templates/index.html`**

- In banner section: check `compat.relation`.
- In `.toolbar`: if `compat.relation == "newer"`, disable the submit button and show tooltip.
- Add align target button to the banner.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_web.py -k "test_index_disables_apply" -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add templates/index.html tests/test_web.py
git commit -m "feat(ui): display target version mismatch banner and guard apply buttons on index"
```

---

### Task 8: Update `templates/update.html` with Safe Target Align Option

**Files:**
- Modify: `templates/update.html`
- Test: `tests/test_web.py`

**Interfaces:**
- Version flow shows: Local -> Target (`v0.5.81`) -> npm latest.
- Primary action: Install safe target `9router@0.5.81`.
- Secondary action: Try latest `9router@latest`.

- [ ] **Step 1: Write test in `tests/test_web.py`**

```python
def test_update_page_has_safe_target_install_option(web):
    html = web["client"].get("/update").text
    assert "0.5.81" in html
    assert "Cài đặt phiên bản tương thích" in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_web.py -k "test_update_page_has_safe_target" -q`
Expected: FAIL.

- [ ] **Step 3: Implement in `templates/update.html`**

Update `verflow` to show 3 boxes (Local, Bản chuẩn v0.5.81, npm latest), and add primary target-install button alongside latest update option.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_web.py -k "test_update_page_has_safe_target" -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add templates/update.html tests/test_web.py
git commit -m "feat(ui): add target version flow and safe target install option on /update"
```

---

### Task 9: Full Verification & Release Binary Build

**Files:**
- All touched files

- [ ] **Step 1: Run full test suite**

Run: `python -m pytest tests/ -q`
Expected: all tests pass (0 failures).

- [ ] **Step 2: Build release executable with clean build-env**

Run: `.venv/Scripts/python build_app.py`
Expected: Exe successfully generated under `dist/files/`.

- [ ] **Step 3: Verify built binary version flag and staging**

Run: `./dist/9router-patch.exe --version`
Expected: `9router-patch.exe 2.1.6` (or next version).
Check `dist/version.json` has matching SHA256.
