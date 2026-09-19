"""Web UI tests: TestClient with engine + probes + updater faked — no build, network, npm."""
import re
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest
from fastapi.testclient import TestClient

import engine
import main

REAL_PATCHES = engine.load_patches()
OWN = f"http://{main.HOST}:{main.PORT}"          # the only origin the guard may accept
EVIL = f"http://evil.example:{main.PORT}"        # DNS-rebinding page, resolves to 127.0.0.1


def boom(*a, **k):
    raise OSError("hermetic test: no network")


def _state(p, state):
    applied = ["f0.js"] if state in ("applied", "partial") else []
    clean = ["f1.js"] if state in ("clean", "partial") else []
    return engine.PatchState(patch=p, state=state, applied_files=applied, clean_files=clean)


@pytest.fixture()
def web(monkeypatch, tmp_path):
    """Hermetic app: engine faked, every socket/urllib call raises, updater is a stub."""
    rec = {"apply": [], "revert": [], "locks": [], "steps": [], "router": []}
    monkeypatch.setattr(engine, "build_dir", lambda: Path("D:/hermetic/build"))
    monkeypatch.setattr(engine, "load_patches", lambda: REAL_PATCHES)
    monkeypatch.setattr(engine, "scan",
                        lambda build, patches, **kw: [_state(p, "applied") for p in patches])
    monkeypatch.setattr(engine, "apply",
                        lambda *a, **k: rec["apply"].append((a, k)) or [])
    monkeypatch.setattr(engine, "revert",
                        lambda *a, **k: rec["revert"].append((a, k)) or [])
    monkeypatch.setattr(main.socket, "create_connection", boom)
    monkeypatch.setattr(main.urllib.request, "urlopen", boom)
    monkeypatch.setattr(main, "updater", SimpleNamespace(
        current_version=lambda: "0.5.65",
        latest_version=lambda: "0.5.65",
        find_locks=lambda target=None: rec["locks"],
        run_update=lambda *a, **k: rec["steps"],
        stop_router_stack=lambda emit: rec["router"].append("stop") or True,
        start_router_stack=lambda emit: rec["router"].append("start") or True,
        stop_headroom=lambda emit: True,
        start_headroom=lambda emit: True,
    ))
    monkeypatch.setattr(main, "HISTORY_FILE", tmp_path / "update-history.jsonl")
    main.LOCK_CACHE.update(locks=[], error=None, probed_at=0.0)
    main.LAST_UPDATE_STEPS = []
    main.JOB = main.JobState()
    main.ACTION = main.ActionState()
    main.OP_SLOT = main.OpSlot()
    main.SNAP = {}      # snapshot GET / là process-global: mỗi test phải fill lại từ đầu
    # default base_url is http://testserver, which would let a wrong-Host request look right
    rec["client"] = TestClient(main.app, base_url=OWN, follow_redirects=False)
    main._forget_versions()      # version cache is process-global; don't leak between tests
    return rec


def set_scan(monkeypatch, mapping):
    monkeypatch.setattr(engine, "scan",
                        lambda build, patches, **kw: [_state(p, mapping.get(p.id, "applied"))
                                                      for p in patches])


# ---------- GET / ----------

def test_index_lists_all_9_patches(web):
    r = web["client"].get("/")
    assert r.status_code == 200
    for p in REAL_PATCHES:
        assert p.id in r.text
    assert "applied" in r.text


def test_index_probes_fail_degrades_gracefully(web):
    """Fixture makes every probe's socket/urllib call raise — page still renders."""
    r = web["client"].get("/")
    assert r.status_code == 200
    assert "down" in r.text          # router + headroom fallback
    assert "unknown" in r.text       # hung-requests fallback
    assert "0.5.65" in r.text        # versions from stubbed updater


def test_index_dead_anchor_banner(web, monkeypatch):
    set_scan(monkeypatch, {"gauge-guard": "dead-anchor"})
    r = web["client"].get("/")
    assert r.status_code == 200
    assert "dead-anchor" in r.text
    # `"banner" in text` is vacuous: base.html's <style> always defines a .banner class
    assert 'class="banner"' in r.text
    assert "gauge-guard" in r.text.split('class="banner"')[1].split("</p>")[0]


def test_sse_group_has_exactly_one_action(web):
    html = web["client"].get("/").text
    # one apply + one revert control for the whole 4-patch group, never per-patch
    assert html.count('name="ids" value="sse-hang"') == 1
    assert html.count('name="group" value="sse-hang"') == 1
    # 26 groups total (incl. sse-hang, nonstream-sse-retry, claude-system-hoist,
    # errbody-html-title, responses-thinking-history-400)
    assert html.count('name="group" value=') == 26


def test_index_has_apply_all_form(web):
    html = web["client"].get("/").text
    assert html.count('action="/apply"') >= 7  # 6 per-group + 1 apply-all


# ---------- CSRF on POST ----------

def test_post_apply_without_origin_403(web):
    r = web["client"].post("/apply", data={})
    assert r.status_code == 403
    assert web["apply"] == []


def test_post_apply_foreign_origin_403(web):
    r = web["client"].post("/apply", data={}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert web["apply"] == []


def test_post_apply_own_origin_redirects_and_applies(web):
    r = web["client"].post("/apply", data={}, headers={"Origin": OWN})
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert len(web["apply"]) == 1
    args, kwargs = web["apply"][0]
    assert kwargs["ids"] is None and len(args) == 2


def test_post_apply_referer_counts_as_own_origin(web):
    r = web["client"].post("/apply", data={}, headers={"Referer": OWN + "/"})
    assert r.status_code == 303


def test_post_apply_selected_ids(web):
    r = web["client"].post("/apply", data={"ids": ["sse-hang"]}, headers={"Origin": OWN})
    assert r.status_code == 303
    assert web["apply"][0][1]["ids"] == ["sse-hang"]


def test_post_update_without_origin_403(web):
    r = web["client"].post("/update")
    assert r.status_code == 403


# ---------- revert ----------

def test_post_revert_requires_group(web):
    r = web["client"].post("/revert", data={}, headers={"Origin": OWN})
    assert r.status_code == 422
    assert web["revert"] == []


def test_post_revert_group_own_origin(web):
    r = web["client"].post("/revert", data={"group": "sse-hang"}, headers={"Origin": OWN})
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert web["revert"][0][1]["group"] == "sse-hang"


def test_post_revert_patch_error_is_error_page_not_500(web, monkeypatch):
    def bad(*a, **k):
        raise engine.PatchError("dead anchor, nothing written: gauge-guard")
    monkeypatch.setattr(engine, "revert", bad)
    r = web["client"].post("/revert", data={"group": "sse-hang"}, headers={"Origin": OWN})
    assert r.status_code < 500
    assert "dead anchor" in r.text


def test_post_apply_patch_error_is_error_page_not_500(web, monkeypatch):
    def bad(*a, **k):
        raise engine.PatchError("node --check failed")
    monkeypatch.setattr(engine, "apply", bad)
    r = web["client"].post("/apply", data={}, headers={"Origin": OWN})
    assert r.status_code < 500
    assert "node --check" in r.text


# ---------- /update ----------

def test_get_update_page(web):
    main.LOCK_CACHE.update(
        locks=[SimpleNamespace(pid=15168, name="node.exe", path=r"E:\Apps\x")],
        error="", probed_at=1.0)
    r = web["client"].get("/update")
    assert r.status_code == 200
    assert "0.5.65" in r.text
    assert "15168" in r.text and "node.exe" in r.text


def test_get_update_never_probes_on_load(web, monkeypatch):
    """handle64 takes ~37.6s: the page must render instantly from cache, probe only on demand."""
    calls = []
    monkeypatch.setattr(main.updater, "find_locks", lambda *a, **k: calls.append(1) or [])
    r = web["client"].get("/update")
    assert r.status_code == 200
    assert calls == []                               # no probe during render
    assert PROBE_WARNING in r.text                   # honest warning until a probe has run
    assert NO_LOCK_CLAIM not in r.text


def test_update_locks_endpoint_probes_and_caches(web):
    web["locks"] = [SimpleNamespace(pid=15168, name="node.exe", path=r"E:\Apps\x")]
    r = web["client"].get("/update/locks")
    assert r.status_code == 200
    body = r.json()
    assert body["locks"][0]["pid"] == 15168 and body["error"] == ""
    assert main.LOCK_CACHE["probed_at"] > 0
    assert "15168" in web["client"].get("/update").text     # cache feeds the next render


def test_get_update_probe_param_runs_probe(web):
    web["locks"] = []
    html = web["client"].get("/update", params={"probe": "1"}).text
    assert NO_LOCK_CLAIM in html                     # probe ran clean -> positive claim


def test_update_start_runs_background_job_and_streams(web, monkeypatch):
    def fake_run(emit=None, autostop=False, skip_gate=False):
        assert autostop is True
        emit({"type": "step-start", "title": "t", "hint": ""})
        emit({"type": "line", "text": "hello"})
        emit({"type": "step-end", "ok": True, "title": "t"})
        return [SimpleNamespace(title="t", ok=True, log="l")]
    monkeypatch.setattr(main.updater, "run_update", fake_run)
    r = web["client"].post("/update/start", data={"autostop": "1"}, headers={"Origin": OWN})
    assert r.status_code == 202 and r.json()["ok"] is True
    deadline = time.time() + 5
    while not main.JOB.done and time.time() < deadline:
        time.sleep(0.02)
    assert main.JOB.done and not main.JOB.running
    assert main.JOB.events[-1] == {"type": "done", "ok": True}
    assert len(main.LAST_UPDATE_STEPS) == 1          # next GET /update renders the timeline
    assert main.LOCK_CACHE["probed_at"] == 0.0       # job invalidates the lock cache
    stream = web["client"].get("/update/stream")
    assert "text/event-stream" in stream.headers["content-type"]
    assert "hello" in stream.text and '"done"' in stream.text


def test_update_start_second_job_conflict_409(web):
    main.OP_SLOT.acquire("job")
    r = web["client"].post("/update/start", data={}, headers={"Origin": OWN})
    assert r.status_code == 409
    main.OP_SLOT.release()


def test_update_start_without_origin_403(web):
    assert web["client"].post("/update/start", data={}).status_code == 403
    assert main.JOB.running is False


def test_post_update_passes_autostop_and_invalidates_lock_cache(web, monkeypatch):
    seen = {}
    monkeypatch.setattr(main.updater, "run_update", lambda *a, **k: seen.update(k) or [])
    main.LOCK_CACHE.update(probed_at=5.0)
    r = web["client"].post("/update", data={"autostop": "1", "skip_gate": "1"},
                           headers={"Origin": OWN})
    assert r.status_code == 200
    assert seen["autostop"] is True
    assert seen["skip_gate"] is True
    assert main.LOCK_CACHE["probed_at"] == 0.0


def test_post_update_start_passes_skip_gate(web, monkeypatch):
    seen = {}
    monkeypatch.setattr(main.updater, "run_update", lambda *a, **k: seen.update(k) or [])
    r = web["client"].post("/update/start", data={"skip_gate": "1"}, headers={"Origin": OWN})
    assert r.status_code == 202
    deadline = time.time() + 5
    while not main.JOB.done and time.time() < deadline:
        time.sleep(0.02)
    assert seen["skip_gate"] is True


# ---------- history: mỗi lần chạy được ghi lại để xem/copy lại ----------

def test_router_toggle_requires_origin(web):
    assert web["client"].post("/router/stop", data={}).status_code == 403
    assert web["client"].post("/router/start", data={},
                              headers={"Origin": "http://evil.example"}).status_code == 403
    assert web["router"] == []


def test_router_stop_redirects_and_runs_sequential(web):
    r = web["client"].post("/router/stop", data={}, headers={"Origin": OWN})
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert web["router"] == ["stop"]
    r = web["client"].post("/router/start", data={}, headers={"Origin": OWN})
    assert r.status_code == 303
    assert web["router"] == ["stop", "start"]


def test_sync_toggle_refused_while_update_job_runs(web):
    """BỎ QUA nút JS vẫn POST /router/stop: khi job update đang chạy, route sync phải từ
    chối, không phải chạy taskkill đè lên npm đang cài (2026-09-07 code review)."""
    main.OP_SLOT.acquire("job")
    try:
        r = web["client"].post("/router/stop", data={}, headers={"Origin": OWN})
        assert r.status_code == 409
        assert web["router"] == []                    # nothing was killed
    finally:
        main.OP_SLOT.release()


def test_sync_post_update_refused_while_action_runs(web):
    """POST /update (no-JS twin của /update/start) cũng phải tôn trọng slot: stack đang
    tắt/bật thì không được npm đè lên."""
    main.OP_SLOT.acquire("action")
    try:
        r = web["client"].post("/update", data={"autostop": "0"}, headers={"Origin": OWN})
        assert r.status_code == 409
        assert web["steps"] == []                     # run_update never ran
    finally:
        main.OP_SLOT.release()


def test_headroom_toggle_runs_alone(web):
    r = web["client"].post("/headroom/stop", data={}, headers={"Origin": OWN})
    assert r.status_code == 303
    assert web["router"] == []                       # headroom alone must not toggle router


# ---------- async stack actions: fetch (Accept: application/json) -> 202 + background ----

def _await_action(timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with main.ACTION.lock:               # Lock không reentrant — một lần duy nhất
            if main.ACTION.done:
                return main.ACTION.name, main.ACTION.ok, list(main.ACTION.lines)
        time.sleep(0.02)
    raise AssertionError("action không kết thúc đúng hạn")


def test_router_stop_json_202_runs_background(web):
    r = web["client"].post("/router/stop", data={},
                           headers={"Origin": OWN, "Accept": "application/json"})
    assert r.status_code == 202
    assert r.json()["ok"] is True
    name, ok, _ = _await_action()
    assert web["router"] == ["stop"]                 # stub chạy trong thread nền
    assert name == "router-stop" and ok is True


def test_router_start_json_202_runs_background(web):
    r = web["client"].post("/router/start", data={},
                           headers={"Origin": OWN, "Accept": "application/json"})
    assert r.status_code == 202
    name, ok, _ = _await_action()
    assert web["router"] == ["start"]
    assert name == "router-start" and ok is True


def test_action_status_reports_state_and_probes(web, monkeypatch):
    monkeypatch.setattr(main, "probe_router", lambda: {"up": False, "hung": "unknown"})
    monkeypatch.setattr(main, "probe_headroom", lambda: {"up": True, "detail": "ok"})
    r = web["client"].get("/action/status")
    assert r.status_code == 200
    body = r.json()
    assert body["running"] is False and body["done"] is True and body["ok"] is None
    assert body["router"] == {"up": False, "hung": "unknown"}
    assert body["headroom"] == {"up": True, "detail": "ok"}


def test_action_status_polls_while_running(web, monkeypatch):
    release = threading.Event()

    def slow(emit):
        release.wait(2)
    monkeypatch.setattr(main.updater, "stop_router_stack", slow)
    r = web["client"].post("/router/stop", data={},
                           headers={"Origin": OWN, "Accept": "application/json"})
    assert r.status_code == 202
    body = web["client"].get("/action/status").json()
    assert body["running"] is True and body["done"] is False
    assert body["name"] == "router-stop"
    release.set()
    _await_action()


def test_action_conflict_returns_409(web, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(main.updater, "start_router_stack", lambda emit: release.wait(2))
    r = web["client"].post("/router/start", data={},
                           headers={"Origin": OWN, "Accept": "application/json"})
    assert r.status_code == 202
    r2 = web["client"].post("/headroom/stop", data={},
                            headers={"Origin": OWN, "Accept": "application/json"})
    assert r2.status_code == 409
    release.set()
    _await_action()


def test_action_refuses_while_update_job_runs(web):
    main.OP_SLOT.acquire("job")
    try:
        r = web["client"].post("/router/stop", data={},
                               headers={"Origin": OWN, "Accept": "application/json"})
        assert r.status_code == 409
    finally:
        main.OP_SLOT.release()
    assert web["router"] == []


def test_update_start_refuses_while_action_runs(web, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(main.updater, "stop_router_stack", lambda emit: release.wait(2))
    r = web["client"].post("/router/stop", data={},
                           headers={"Origin": OWN, "Accept": "application/json"})
    assert r.status_code == 202
    r2 = web["client"].post("/update/start", data={"autostop": "0"}, headers={"Origin": OWN})
    assert r2.status_code == 409
    release.set()
    _await_action()


def test_action_failure_recorded_not_raised(web, monkeypatch):
    def bad(emit):
        raise RuntimeError("kaput")
    monkeypatch.setattr(main.updater, "start_router_stack", bad)
    r = web["client"].post("/router/start", data={},
                           headers={"Origin": OWN, "Accept": "application/json"})
    assert r.status_code == 202
    _, ok, lines = _await_action()
    assert ok is False
    assert any("kaput" in x for x in lines)


def test_action_collects_emit_lines(web, monkeypatch):
    def with_lines(emit):
        emit({"type": "line", "text": "đã tắt 9router pid 1 (:20128)"})
        return True
    monkeypatch.setattr(main.updater, "stop_router_stack", with_lines)
    r = web["client"].post("/router/stop", data={},
                           headers={"Origin": OWN, "Accept": "application/json"})
    assert r.status_code == 202
    _, ok, lines = _await_action()
    assert ok is True
    assert any("pid 1" in x for x in lines)
    assert any("pid 1" in x for x in web["client"].get("/action/status").json()["lines"])


def test_action_false_result_is_not_ok(web, monkeypatch):
    """_launch_port_cmd trả False khi bind timeout — status phải thấy thất bại, không phải ok."""
    monkeypatch.setattr(main.updater, "start_headroom", lambda emit: False)
    r = web["client"].post("/headroom/start", data={},
                           headers={"Origin": OWN, "Accept": "application/json"})
    assert r.status_code == 202
    _, ok, _ = _await_action()
    assert ok is False


def test_dashboard_has_service_markers(web):
    """JS cập nhật chip realtime cần mốc dữ liệu ổn định trong markup."""
    html = web["client"].get("/").text
    assert 'data-service="router"' in html
    assert 'data-service="headroom"' in html
    assert "data-hung" in html
    assert "data-hrdetail" in html


def test_dashboard_shows_toggle_buttons(web):
    html = web["client"].get("/").text
    assert 'action="/router/start"' in html          # fixture: probes fail -> DOWN -> Bật
    assert 'action="/headroom/start"' in html


def test_streamed_job_is_recorded_to_history(web, monkeypatch):
    def fake_run(emit=None, autostop=False, skip_gate=False):
        emit({"type": "line", "text": "hi"})
        return [SimpleNamespace(title="1. x", ok=True, log="done")]
    monkeypatch.setattr(main.updater, "run_update", fake_run)
    forget = []
    monkeypatch.setattr(main, "_forget_versions", lambda: forget.append(1))
    web["client"].post("/update/start", data={"autostop": "0"}, headers={"Origin": OWN})
    deadline = time.time() + 5
    while not main.JOB.done and time.time() < deadline:
        time.sleep(0.02)
    assert main.HISTORY_FILE.exists()
    assert forget                               # job phải xóa cache version (npm đổi bản)
    html = web["client"].get("/update").text
    assert "Lịch sử chạy" in html and "autostop: tắt" in html
    assert "Copy log" in html


def test_sync_post_update_also_records_history(web):
    web["steps"] = [SimpleNamespace(title="1. npm i -g 9router@latest", ok=True, log="ok")]
    web["client"].post("/update", data={}, headers={"Origin": OWN})
    assert main.HISTORY_FILE.exists()
    hist = main._load_history()
    assert len(hist) == 1 and hist[0]["ok"] is True
    assert hist[0]["steps"][0]["title"].startswith("1.")


def test_post_update_renders_steps(web):
    web["steps"] = [SimpleNamespace(title="npm i -g 9router@latest", ok=True,
                                     log="added 1 package in 30s")]
    r = web["client"].post("/update", headers={"Origin": OWN})
    assert r.status_code == 200
    assert "npm i -g 9router@latest" in r.text
    assert "added 1 package in 30s" in r.text
    assert "<pre>" in r.text


def test_pages_render_when_updater_module_missing(web, monkeypatch):
    """updater.py is written in parallel; UI must boot when it is absent."""
    monkeypatch.setattr(main, "updater", None)
    assert web["client"].get("/").status_code == 200
    assert web["client"].get("/update").status_code == 200


# ---------- /update lock states: found / probed-empty / not-probed ----------

NO_LOCK_CLAIM = "Không có process nào đang giữ thư mục cài đặt"
PROBE_WARNING = "chưa dò được lock"


def render_update(**ctx):
    """Render update.html through the app's own Jinja env.

    lock_error="" (probe ran, no error) is the contract main.py must pass to earn the
    positive claim; GET /update does not pass it yet, so no route reaches that state.
    """
    return main.templates.get_template("update.html").render(ctx)


def test_update_lock_table_makes_neither_claim_nor_warning(web):
    main.LOCK_CACHE.update(
        locks=[SimpleNamespace(pid=15168, name="node.exe", path=r"E:\Apps\x")],
        error="", probed_at=1.0)
    html = web["client"].get("/update").text
    assert "15168" in html                       # state 1: the table speaks for itself
    assert NO_LOCK_CLAIM not in html
    assert PROBE_WARNING not in html


def test_update_claims_no_locks_only_when_probe_reported_success(web):
    html = render_update(locks=[], lock_error="")  # state 2
    assert NO_LOCK_CLAIM in html
    assert PROBE_WARNING not in html


def test_get_update_warns_when_lock_probe_raised(web, monkeypatch):
    """find_locks blows up (handle64 timeout); main.py swallows it into locks=[]."""
    monkeypatch.setattr(main.updater, "find_locks", boom)
    html = web["client"].get("/update").text     # state 3, no error text available
    assert PROBE_WARNING in html
    assert "EBUSY" in html
    assert NO_LOCK_CLAIM not in html
    assert '<p class="banner">' in html          # warning, not a plain note


def test_update_warning_shows_probe_error_text_when_given(web):
    html = render_update(locks=[], lock_error="handle64 timed out after 37.6s")  # state 3
    assert PROBE_WARNING in html
    assert "handle64 timed out after 37.6s" in html
    assert NO_LOCK_CLAIM not in html


def test_post_update_never_claims_no_locks(web):
    """POST runs no lock probe at all; step 3's log is the only lock report there."""
    web["steps"] = [SimpleNamespace(title="lock probe", ok=True, log="no handles")]
    html = web["client"].post("/update", headers={"Origin": OWN}).text
    assert NO_LOCK_CLAIM not in html
    assert PROBE_WARNING in html


# ---------- FINDING 1 (Critical): no docs/openapi for a mutating local tool ----------

def test_docs_redoc_openapi_disabled(web):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert web["client"].get(path).status_code == 404


# ---------- FINDING 2 (Important): DNS rebinding / wrong scheme / wrong Host ----------

def test_evil_origin_and_evil_host_post_403(web):
    """DNS-rebinding page at evil.example resolving to 127.0.0.1: guard must not
    trust the client-supplied Host header as the comparator."""
    h = {"Origin": EVIL, "Host": f"evil.example:{main.PORT}"}
    assert web["client"].post("/apply", data={}, headers=h).status_code == 403
    assert web["client"].post("/update", headers=h).status_code == 403
    assert web["client"].post("/revert", data={"group": "sse-hang"}, headers=h).status_code == 403
    assert web["apply"] == [] and web["revert"] == []


def test_evil_host_get_403(web):
    r = web["client"].get("/", headers={"Host": f"evil.example:{main.PORT}"})
    assert r.status_code == 403


def test_localhost_origin_and_host_accepted(web):
    """Both names of the bound address are our own origin: 127.0.0.1 and localhost."""
    h = {"Origin": f"http://localhost:{main.PORT}", "Host": f"localhost:{main.PORT}"}
    r = web["client"].post("/apply", data={}, headers=h)
    assert r.status_code == 303
    assert len(web["apply"]) == 1


def test_https_origin_over_plain_http_403(web):
    r = web["client"].post("/apply", data={},
                           headers={"Origin": f"https://{main.HOST}:{main.PORT}"})
    assert r.status_code == 403
    assert web["apply"] == []


# ---------- FINDING 3 (Important): `npm root -g` probe memoised (0.4s each spawn) ----------

def test_dashboard_runs_local_version_probe_once_per_page_session(web, monkeypatch):
    calls = []
    monkeypatch.setattr(main.updater, "current_version",
                        lambda: calls.append(1) or "0.5.65")
    assert web["client"].get("/").status_code == 200
    assert web["client"].get("/").status_code == 200
    assert len(calls) == 1               # second render reuses the cached lookup


def test_dashboard_runs_build_scan_once_per_page_session(web, monkeypatch):
    """LCP fix: GET / render từ snapshot — quét build chỉ chạy khi snapshot lạnh/stale."""
    calls = []
    def counting(build, patches, **kw):
        calls.append(1)
        return [_state(p, "applied") for p in patches]
    monkeypatch.setattr(engine, "scan", counting)
    assert web["client"].get("/").status_code == 200
    assert web["client"].get("/").status_code == 200
    assert len(calls) == 1               # second render reuses the snapshot


def test_local_version_probe_failure_is_not_pinned_and_retried(web, monkeypatch):
    """Cached failure must not pin 'unknown' for the process lifetime."""
    state = {"fail": True}

    def flaky():
        if state["fail"]:
            raise OSError("no network")
        return "0.5.65"
    monkeypatch.setattr(main.updater, "current_version", flaky)
    r1 = web["client"].get("/")
    assert r1.status_code == 200 and "unknown" in r1.text
    state["fail"] = False
    r2 = web["client"].get("/")
    assert r2.status_code == 200 and "0.5.65" in r2.text


# ---------- FINDING 4 (Important): EBUSY / corrupt patches.toml = page, not 500 ----------

def test_post_apply_oserror_is_error_page_not_500(web, monkeypatch):
    def bad(*a, **k):
        raise PermissionError("access denied writing node_modules")
    monkeypatch.setattr(engine, "apply", bad)
    r = web["client"].post("/apply", data={}, headers={"Origin": OWN})
    assert r.status_code < 500
    assert "access denied" in r.text


def test_post_revert_oserror_is_error_page_not_500(web, monkeypatch):
    def bad(*a, **k):
        raise OSError("winerror 32: file in use")
    monkeypatch.setattr(engine, "revert", bad)
    r = web["client"].post("/revert", data={"group": "sse-hang"}, headers={"Origin": OWN})
    assert r.status_code < 500
    assert "winerror 32" in r.text


def test_post_apply_corrupt_patches_file_is_error_page_not_500(web, monkeypatch):
    def bad(*a, **k):
        raise ValueError("bad TOML in patches.toml")
    monkeypatch.setattr(engine, "load_patches", bad)
    r = web["client"].post("/apply", data={}, headers={"Origin": OWN})
    assert r.status_code < 500
    assert "bad TOML" in r.text


# ---------- FINDING 5 (Minor): banner element only when a patch really is dead ----------

def test_index_has_no_banner_element_when_nothing_wrong(web):
    html = web["client"].get("/").text
    assert '<p class="banner">' not in html


def test_error_page_has_heading_and_working_way_back(web, monkeypatch):
    """base.html standalone (the refusal page) must not be a dead end."""
    monkeypatch.setattr(engine, "apply", boom)
    r = web["client"].post("/apply", data={}, headers={"Origin": OWN})
    assert r.status_code == 409
    assert '<p class="banner">' in r.text
    assert "<h1></h1>" not in r.text
    assert 'href="/"' in r.text


def test_no_dead_anchors_on_any_page(web, monkeypatch):
    monkeypatch.setattr(engine, "apply", boom)
    pages = [web["client"].get("/").text,
             web["client"].get("/update").text,
             web["client"].post("/apply", data={}, headers={"Origin": OWN}).text]
    hrefs = {h for html in pages for h in re.findall(r'href="([^"#]+)"', html)}
    assert hrefs
    for h in hrefs:
        assert web["client"].get(h).status_code != 404, h
