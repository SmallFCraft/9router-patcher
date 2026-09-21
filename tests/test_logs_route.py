"""GET /logs + GET /api/logs: system logs page renders, API returns JSON, no leaks."""
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest
from fastapi.testclient import TestClient

import engine
import main

REAL_PATCHES = engine.load_patches()
OWN = f"http://{main.HOST}:{main.PORT}"


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
    main.SNAP = {}
    rec["client"] = TestClient(main.app, base_url=OWN, follow_redirects=False)
    main._forget_versions()
    return rec


def test_logs_route_renders_and_returns_200(web):
    client = web["client"]
    r = client.get("/logs")
    assert r.status_code == 200
    assert "Nhật ký hệ thống" in r.text
    assert "Sao chép toàn bộ log gửi Dev" in r.text


def test_logs_api_returns_json(web):
    client = web["client"]
    r = client.get("/api/logs")
    assert r.status_code == 200
    data = r.json()
    assert "boot" in data
    assert "history" in data


def test_logs_never_leaks_patch_payloads_or_secrets(web):
    client = web["client"]
    r = client.get("/logs")
    for p in engine.load_patches():
        assert p.find not in r.text
        assert p.replace not in r.text


def test_gather_logs_splits_router_and_headroom(monkeypatch, tmp_path):
    """Router dài cần tab riêng: file router-*.log không còn chảy vào App chung."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "router-a.log").write_text("R1\n", encoding="utf-8")
    (log_dir / "headroom-a.log").write_text("H1\n", encoding="utf-8")
    (log_dir / "app.log").write_text("A1\n", encoding="utf-8")
    monkeypatch.setattr(main.app_paths, "get_log_dir", lambda: log_dir)
    ctx = main._gather_logs()
    assert "R1" in "".join(ctx["router"])
    assert "H1" in "".join(ctx["headroom"])
    assert "A1" in "".join(ctx["app"])
    blob = "".join(ctx["app"])
    assert "R1" not in blob and "H1" not in blob


def test_logs_page_has_five_tabs_search_and_scroll(web, tmp_path):
    """Regression UI: 5 tab + ô search + thanh cuộn riêng + tô đậm lỗi."""
    html = web["client"].get("/logs").text
    for tab in ('data-tab="doctor"', 'data-tab="update"', 'data-tab="app"',
                'data-tab="router"', 'data-tab="headroom"'):
        assert tab in html
    assert 'id="log-search"' in html
    assert "logscroll" in html  # CSS thanh cuộn mỗi tab


def test_log_line_kind_marks_errors_and_warnings():
    """Dòng ERROR/FATAL/FAIL đậm đỏ, WARN/CẢNH BÁO vàng, dòng thường trung tính."""
    for bad in ("[proxy] ERROR: upstream timeout", "Traceback (most recent call last):",
                "update FAIL", "EBUSY: resource busy", "WinError 32"):
        assert main.log_line_kind(bad) == "err", bad
    for warn in ("WARN: retrying", "CẢNH BÁO: patches lệch"):
        assert main.log_line_kind(warn) == "warn", warn
    assert main.log_line_kind("INFO: Uvicorn running") == ""
    assert main.log_line_kind("15:51:49 [ OK ] Node.js v22") == ""


def test_sanitize_log_line_redacts_paths_and_secrets():
    evil = (r"D:\npm-global\node_modules\9router\server\foo.js "
            "api_key=sk-abc123 secret=hunter2 "
            "C:\\Users\\me\\.env Bearer TOKENXYZ123")
    clean = main.sanitize_log_line(evil)
    assert "node_modules" not in clean
    assert "server/" not in clean and "server\\" not in clean
    assert "sk-abc123" not in clean
    assert "hunter2" not in clean
    assert ".env" not in clean
    assert "TOKENXYZ123" not in clean
    assert "[redacted]" in clean


def test_logs_page_never_shows_lock_file_paths(web, monkeypatch):
    lock = SimpleNamespace(pid=15168, name="node.exe", path=r"E:\Apps\secret\x")
    monkeypatch.setattr(main.updater, "find_locks",
                        lambda target=None: [lock])
    html = web["client"].get("/logs").text
    # lock paths are machine-private; never even a segment of one -> redacted
    assert r"E:\Apps\secret\x" not in html
    assert "secret" not in html and "Apps" not in html


def test_logs_page_redacts_app_log_paths_and_secrets(web, tmp_path):
    """Log the aggressive string: page must show the redaction, not the raw secret."""
    import main as _m
    evil = (r"D:\npm-global\node_modules\9router\server\foo.js "
            "api_key=sk-abc123 secret=hunter2 "
            r"C:\Users\me\.env Bearer TOKENXYZ123 ghp_abcdef123456")
    (main.app_paths.get_log_dir() / "router-x.log").write_text(evil,
                                                               encoding="utf-8")
    html = web["client"].get("/logs").text
    for bad in ("node_modules", r"server\foo.js", "sk-abc123", "hunter2",
                ".env", "TOKENXYZ123", "ghp_abcdef123456"):
        assert bad not in html, bad
    assert "[redacted]" in html


def test_logs_api_never_leaks_patch_payloads(web):
    body = web["client"].get("/api/logs").json()
    blob = str(body)
    for p in engine.load_patches():
        assert p.find not in blob
        assert p.replace not in blob
