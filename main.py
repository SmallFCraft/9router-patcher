"""9router patch manager UI — 2 pages + background update job with SSE console.

Local-only tool with write access to node_modules: binds 127.0.0.1:20129, refuses any request
not addressed to that fixed address, and every POST must carry our own Origin/Referer.
Never reads data.sqlite, never logs .env.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import signal
import socket
import sys
import threading
import time
import urllib.request
from collections import deque
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates

import app_paths
import boot_doctor
import engine
import self_update
import version

try:
    import updater                      # written in parallel; UI must boot without it
except ImportError:                     # pragma: no cover
    updater = None

HERE = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = 20129
PROBE_TIMEOUT = 0.5                     # loopback probes only (router + headroom); the version
                                        # probes leave the box and use updater's own timeouts
ROUTER_PORT = 20128
ROUTER_STATS = f"http://{HOST}:{ROUTER_PORT}/api/usage/stats"
HEADROOM_READYZ = f"http://{HOST}:8787/readyz"

# Fixed identity of this server. Never derived from the request's own Host header: a
# DNS-rebinding page on evil.example that resolves to 127.0.0.1 controls both Host and
# Origin, so comparing them to each other always passes.
OWN_HOSTS = frozenset({f"{HOST}:{PORT}", f"localhost:{PORT}"})
OWN_ORIGINS = frozenset({f"http://{h}" for h in OWN_HOSTS})

# Lock-probe cache: GET /update must render instantly (handle64 alone takes ~37.6s), so the
# probe runs only when asked — via ?probe=1, GET /update/locks (JS background probe), or the
# update pipeline itself. error None = chưa probe lần nào; "" = probe sạch; str = probe lỗi.
LOCK_CACHE: dict = {"locks": [], "error": None, "probed_at": 0.0}
LAST_UPDATE_STEPS: list = []            # kết quả job gần nhất để GET /update render lại

HISTORY_FILE = app_paths.get_history_file()   # 1 job = 1 dòng JSON, chọn theo thời gian
HISTORY_KEEP = 50


# ---------------------------------------------------------------- probes (all degrade)

def _get_json(url: str, token: str | None = None):
    req = urllib.request.Request(url)
    if token:
        req.add_header("x-9r-cli-token", token)
    with closing(urllib.request.urlopen(req, timeout=PROBE_TIMEOUT)) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


@lru_cache(maxsize=1)
def _local_version() -> str:
    """`updater.current_version()` -> `engine.install_dir()` -> `npm root -g` (~0.4s a spawn).

    lru_cache never stores exceptions, so a failed probe retries on the next page load
    instead of pinning "unknown" for the life of the process.
    """
    return updater.current_version()


def _forget_versions() -> None:
    _local_version.cache_clear()


def probe_versions() -> tuple[str, str]:
    out = []
    # latest_version stays uncached on purpose: it is one registry GET on updater's own
    # timeout, and caching it would hide a freshly published version until restart.
    for fn in (_local_version, lambda: updater.latest_version()):
        try:
            out.append(fn())
        except Exception:
            out.append("unknown")
    return out[0], out[1]


def _router_token() -> str | None:
    """CLI token 9router itself derives: sha256(machine-id + "9r-cli-auth" + cli-secret)[:16],
    sent as `x-9r-cli-token`. Same derivation as src/cli/api/client.js getCliToken()."""
    try:
        base = Path(os.environ.get("APPDATA", "")) / "9router"
        raw = (base / "machine-id").read_text(encoding="utf-8").strip()
        secret = (base / "auth" / "cli-secret").read_text(encoding="utf-8").strip()
        if not raw or not secret:
            return None
        return hashlib.sha256((raw + "9r-cli-auth" + secret).encode()).hexdigest()[:16]
    except OSError:
        return None


def probe_router() -> dict:
    try:
        with closing(socket.create_connection((HOST, ROUTER_PORT), timeout=PROBE_TIMEOUT)):
            up = True
    except Exception:
        return {"up": False, "hung": "unknown"}
    token = _router_token()
    if token is None:                      # auth token unavailable -> unknown, not a wrong 0
        return {"up": up, "hung": "unknown"}
    try:
        return {"up": up, "hung": len(_get_json(ROUTER_STATS, token).get("activeRequests", []))}
    except Exception:
        return {"up": up, "hung": "unknown"}


def _is_headroom_installed() -> bool:
    """True nếu updater có sẵn và máy có cài headroom."""
    try:
        return bool(updater and hasattr(updater, "headroom_status")
                    and updater.headroom_status().get("installed"))
    except Exception:
        return False


def probe_headroom() -> dict:
    """Trạng thái headroom: `installed=False` khi máy chưa cài (module tuỳ chọn).

    Phân biệt "chưa cài" với "cài rồi nhưng DOWN" — UI phải nói rõ cái nào, nếu
    không máy không dùng headroom sẽ thấy thẻ đỏ DOWN vô cớ."""
    installed = _is_headroom_installed()
    try:
        d = _get_json(HEADROOM_READYZ)
        # readyz trả lời nghĩa là headroom ĐANG CHẠY -> chắc chắn đã cài,
        # dù probe pythonw có hụt (frozen exe không thấy sibling pythonw).
        return {"up": True, "installed": True,
                "detail": d.get("status", "ok") if isinstance(d, dict) else "ok"}
    except Exception:
        return {"up": False, "installed": installed,
                "detail": "down" if installed else "chưa cài đặt"}


# ---------------------------------------------------------------- snapshot (GET / LCP)

# GET / used to pay, on every load: `npm root -g` spawn + engine.scan + registry GET +
# loopback probes — TTFB 1.5–2s measured (Lighthouse LCP 4.36s; seconds more on slow DNS),
# and with all CSS/JS inline LCP == TTFB. Pages now render from SNAP: the same data,
# computed off the request path. A daemon refresher (lifespan startup only — TestClient
# without a `with` never runs it, keeping the test suite hermetic and synchronous) keeps
# sections warm while a client is actually viewing; every mutation refreshes what it
# changed synchronously so a redirect target never shows stale badges/buttons.
SNAP: dict = {}
SNAP_LOCK = threading.Lock()
LAST_SNAP_CLIENT = 0.0                  # wall clock of the last page view
SNAP_WORKER_EVERY = 3.0                 # giây — tick nền; router+headroom probe mỗi tick
SCAN_TTL = 30.0                         # giây — ai sửa build ngoài tool cũng thấy trong ≤30s
VERSION_TTL = 60.0                      # giây — latest_version là 1 GET registry ngoài mạng
CLIENT_IDLE_AFTER = 120.0               # không ai xem trang -> worker ngủ, đỡ quét nền
PROBE_SYNC_MAX_AGE = 10.0               # GET / chỉ refresh đồng bộ khi mục quá cũ (idle return)
SCAN_SYNC_MAX_AGE = 120.0


def _snap_scan() -> dict:
    t0 = time.perf_counter()
    build_path, states, error = "", [], ""
    try:
        build = engine.build_dir()
        build_path = str(build)
        states = engine.scan(build, engine.load_patches())
    except Exception as e:              # no npm / no build: dashboard still renders
        error = str(e)
    return {"ok": not error, "error": error, "states": states, "build_path": build_path,
            "scan_seconds": round(time.perf_counter() - t0, 2), "at": time.time()}


def _snap_probes() -> dict:
    return {"ok": True, "router": probe_router(), "headroom": probe_headroom(),
            "at": time.time()}


def _snap_versions() -> dict:
    local, latest = probe_versions()
    return {"ok": "unknown" not in (local, latest), "local": local, "latest": latest,
            "at": time.time()}


def _snap_refresh(scan: bool = False, probes: bool = False, versions: bool = False) -> None:
    """Rebuild the requested sections in the caller's thread, then swap SNAP atomically.
    Section builders never raise, so this is safe inside a request or the update job."""
    global SNAP
    fresh: dict = {}
    if scan:
        fresh["scan"] = _snap_scan()
    if probes:
        fresh["probes"] = _snap_probes()
    if versions:
        fresh["versions"] = _snap_versions()
    if not fresh:
        return
    with SNAP_LOCK:
        SNAP = {**SNAP, **fresh}


def _snap_put_probes(router: dict, headroom: dict) -> None:
    """Feed live probe results back: /action/status probes every 1.5s during actions and
    refreshCards() swaps statgrid from server-rendered HTML — it must never swap stale."""
    global SNAP
    with SNAP_LOCK:
        SNAP = {**SNAP, "probes": {"ok": True, "router": router, "headroom": headroom,
                                   "at": time.time()}}


def _stale(section: dict | None, max_age: float) -> bool:
    return section is None or not section["ok"] or time.time() - section["at"] > max_age


def _render_snap() -> dict:
    """SNAP cho GET /: mục tốt + đủ mới -> dùng ngay (TTFB ~0); thiếu/hỏng/quá cũ ->
    refresh đồng bộ đúng mục đó — giữ semantics cũ "mỗi page load thử lại probe hỏng"."""
    with SNAP_LOCK:
        scan, probes, versions = SNAP.get("scan"), SNAP.get("probes"), SNAP.get("versions")
    need = {
        "scan": _stale(scan, SCAN_SYNC_MAX_AGE),
        "probes": _stale(probes, PROBE_SYNC_MAX_AGE),
        # version hỏng phải thử lại mỗi lần render, không được kẹt "unknown" mãi
        "versions": versions is None or not versions["ok"],
    }
    if any(need.values()):
        _snap_refresh(**need)
    return SNAP


def _fresh_versions() -> tuple[str, str]:
    """local/latest cho /update: như _render_snap nhưng chỉ đụng section versions."""
    with SNAP_LOCK:
        v = SNAP.get("versions")
    if v is None or not v["ok"]:
        _snap_refresh(versions=True)
        with SNAP_LOCK:
            v = SNAP.get("versions")
    if v is None:
        return "unknown", "unknown"
    return v["local"], v["latest"]


def _touch_client() -> None:
    global LAST_SNAP_CLIENT
    LAST_SNAP_CLIENT = time.time()


def _snap_worker() -> None:
    """Warm SNAP in the background while someone is actually viewing the pages; fills once
    at boot so the very first page load is fast too. The loop never dies."""
    try:
        _snap_refresh(scan=True, probes=True, versions=True)
    except Exception:
        pass
    while True:
        time.sleep(SNAP_WORKER_EVERY)
        try:
            if time.time() - LAST_SNAP_CLIENT > CLIENT_IDLE_AFTER:
                continue                # không ai xem — đỡ quét build + GET registry nền
            with SNAP_LOCK:
                scan, probes, versions = (SNAP.get("scan"), SNAP.get("probes"),
                                          SNAP.get("versions"))
            now = time.time()
            _snap_refresh(
                scan=scan is None or not scan["ok"] or now - scan["at"] > SCAN_TTL,
                probes=probes is None or now - probes["at"] > SNAP_WORKER_EVERY,
                # versions hỏng KHÔNG retry nền mỗi tick 3s (spawn npm + GET registry liên
                # tục khi mạng chết); request path (_render_snap/_fresh_versions) vẫn retry.
                versions=versions is not None and versions["ok"]
                and now - versions["at"] > VERSION_TTL,
            )
        except Exception:
            pass                        # tick sau thử lại


# ---------------------------------------------------------------- CSRF

def check_host(request: Request) -> None:
    """Every request must be addressed to this server's fixed address, not whatever
    Host the client chose — DNS-rebinding is exactly an attacker-controlled Host."""
    if (request.headers.get("host") or "").lower() not in OWN_HOSTS:
        raise HTTPException(status_code=403, detail="unknown host")


def same_origin(request: Request) -> None:
    """403 any POST whose Origin/Referer is not this same server (browser-only guard)."""
    sent = request.headers.get("origin") or request.headers.get("referer")
    if not sent:
        raise HTTPException(status_code=403, detail="cross-site POST rejected")
    sent = urlsplit(sent)
    if f"{sent.scheme}://{sent.netloc.lower()}" not in OWN_ORIGINS:
        raise HTTPException(status_code=403, detail="cross-site POST rejected")


CSRF = [Depends(same_origin)]

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Uvicorn runtime only: TestClient without a `with` block never runs this, so the
    refresher thread (real npm/network I/O) never exists inside the test suite."""
    threading.Thread(target=_snap_worker, name="snap-refresh", daemon=True).start()
    self_update.cleanup_old_files()
    threading.Thread(target=self_update.run_worker, args=(_UPDATE_STOP,),
                     name="auto-update-worker", daemon=True).start()
    try:
        yield
    finally:
        _UPDATE_STOP.set()


# No /docs, /redoc, /openapi.json: this app mutates node_modules and runs `npm i -g`;
# a machine-readable catalogue of those endpoints is pure liability for a local tool.
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=_lifespan, dependencies=[Depends(check_host)])
templates = Jinja2Templates(directory=str(HERE / "templates"))
templates.env.globals["app_version"] = version.APP_VERSION

# 8-bit cartridge-style favicon: chunky green body, hard black border, square pixels.
# viewBox 32x32 so it reads crisp at favicon sizes; no anti-aliased curves.
_FAVICON_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" shape-rendering="crispEdges">'
                '<rect width="32" height="32" fill="#0a0e27"/>'
                '<rect x="4" y="2" width="24" height="2" fill="#22c55e"/>'
                '<rect x="2" y="4" width="2" height="6" fill="#22c55e"/>'
                '<rect x="28" y="4" width="2" height="6" fill="#22c55e"/>'
                '<rect x="2" y="10" width="28" height="18" fill="#22c55e"/>'
                '<rect x="2" y="10" width="3" height="18" fill="#16a34a"/>'
                '<rect x="27" y="10" width="3" height="18" fill="#16a34a"/>'
                '<rect x="8" y="14" width="4" height="4" fill="#0a0e27"/>'
                '<rect x="20" y="14" width="4" height="4" fill="#0a0e27"/>'
                '<rect x="9" y="20" width="14" height="3" fill="#0a0e27"/>'
                '</svg>')


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(_FAVICON_SVG, media_type="image/svg+xml",
                    headers={"Cache-Control": "max-age=86400"})


@app.get("/font.css", include_in_schema=False)
def font_css():
    """VT323 pixel font (latin + vietnamese subsets) as one cached stylesheet.

    Inlined into every page it would add ~35 KB of base64 to each response; served once
    and cached forever instead. The template render is the only thing that reads it."""
    css = templates.env.get_template("_fonts.html").render()
    return Response(css, media_type="text/css",
                    headers={"Cache-Control": "public, max-age=31536000, immutable"})


def _error(request: Request, msg: str):
    """Patch refused: render the shell with a banner instead of a 500 traceback."""
    return templates.TemplateResponse(request, "base.html",
                                      {"banner": msg}, status_code=409)


@app.get("/logs", include_in_schema=False)
def logs_page(request: Request):
    ctx = _gather_logs()
    ctx["log_line_kind"] = log_line_kind
    ctx["headroom_installed"] = _is_headroom_installed()
    return templates.TemplateResponse(request, "logs.html", ctx)


@app.get("/api/logs", include_in_schema=False)
def logs_api():
    return JSONResponse(_gather_logs())


def _latest_backup() -> datetime | None:
    """Newest engine backup snapshot (names are UTC stamps); None = chưa backup lần nào."""
    try:
        stamps = []
        for d in engine.BACKUP_ROOT.iterdir():
            try:
                stamps.append(datetime.strptime(d.name, engine.BACKUP_STAMP)
                              .replace(tzinfo=timezone.utc))
            except ValueError:
                pass                       # thư mục lạ trong backups/ — bỏ qua
        return max(stamps, default=None)
    except OSError:
        return None


# ---------------------------------------------------------------- lock probe + update job

def _steps_dicts(steps) -> list[dict]:
    return [{"title": getattr(s, "title", ""), "ok": bool(getattr(s, "ok", False)),
             "log": getattr(s, "log", "")} for s in (steps or [])]


def _record_history(started_at: float, autostop: bool, steps) -> None:
    """Append one JSON line per run (trim to HISTORY_KEEP) — để xem lại/copy log các lần cũ."""
    rec = {"started_at": started_at,
           "time_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started_at)),
           "ok": all(bool(getattr(s, "ok", False)) for s in steps),
           "autostop": autostop, "steps": _steps_dicts(steps)}
    try:
        HISTORY_FILE.parent.mkdir(exist_ok=True)
        lines = HISTORY_FILE.read_text(encoding="utf-8").splitlines() \
            if HISTORY_FILE.exists() else []
        lines.append(json.dumps(rec, ensure_ascii=False))
        HISTORY_FILE.write_text("\n".join(lines[-HISTORY_KEEP:]) + "\n", encoding="utf-8")
    except OSError:
        pass                                # history là tiện ích — không được phá job


def _load_history() -> list[dict]:
    try:
        lines = HISTORY_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return list(reversed(out[-HISTORY_KEEP:]))      # mới nhất trước


def _probe_locks() -> None:
    """handle64 probe (~37.6s measured on this box) — chỉ chạy khi được yêu cầu tường minh."""
    try:
        locks = updater.find_locks()
        LOCK_CACHE.update(locks=locks, error="", probed_at=time.time())
    except Exception as e:          # timeout/missing binary must NOT read as "no locks"
        LOCK_CACHE.update(locks=[], error=str(e), probed_at=time.time())


class JobState:
    """Single-slot background update job. Events are buffered (not a queue) so an SSE client
    that attaches late still receives everything, and reconnects replay from an index."""

    def __init__(self):
        self.events: list[dict] = []
        self.cond = threading.Condition()
        self.running = False
        self.done = True


JOB = JobState()


class ActionState:
    """Single-slot background stack action (Tắt/Bật 9router + headroom). The old routes ran
    stop/start synchronously, so the browser could not paint the next page (LCP element =
    status chips) until `_wait_port` finished — measured LCP 33s on Bật tất cả. Now fetch
    gets 202 immediately and polls /action/status; plain form submits keep the sync path."""

    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.name = ""                  # router-stop / router-start / headroom-stop / headroom-start
        self.done = True
        self.ok: bool | None = None     # None = chưa xong; False = fn trả False hoặc raise
        self.lines: list[str] = []      # emit() log từ updater, capped


ACTION = ActionState()


class OpSlot:
    """Single-slot claim for whichever long-running op owns the stack right now.

    THE one check-and-set gate between the two async kinds of long-running work and the
    sync (no-JS) routes. It does not replace JOB/ACTION state — it is the mutual-exclusion
    primitive they share. `kind` is purely for /update's lock probe to know whether a job
    or a toggle is occupying the slot.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.kind = None

    def acquire(self, kind: str) -> bool:
        with self.lock:
            if self.kind is not None:
                return False
            self.kind = kind
            return True

    def release(self) -> None:
        with self.lock:
            self.kind = None


OP_SLOT = OpSlot()
_UPDATE_STOP = threading.Event()        # dừng auto-update worker lúc shutdown


# ---------------------------------------------------------------- pages

@app.get("/")
def index(request: Request):
    _touch_client()
    snap = _render_snap()
    scan, probes, versions = snap["scan"], snap["probes"], snap["versions"]
    states: list[engine.PatchState] = scan["states"]
    error, build_path = scan["error"], scan["build_path"]
    grouped: dict[str, list[engine.PatchState]] = {}
    for s in states:
        grouped.setdefault(s.patch.group, []).append(s)
    groups = [{
        "name": g,
        "members": ms,
        # group action is atomic, so the group carries one state, not four
        "state": ("dead-anchor" if any(x.state == "dead-anchor" for x in ms) else
                  "applied" if all(x.state == "applied" for x in ms) else
                  "clean" if all(x.state == "clean" for x in ms) else "partial"),
        "files": sorted({f for x in ms for f in x.applied_files}),
    } for g, ms in grouped.items()]
    counts = {k: sum(1 for s in states if s.state == k)
              for k in ("applied", "clean", "partial", "dead-anchor")}
    backup = _latest_backup()
    compat = {"compatible": True, "relation": "match", "local": versions["local"],
              "target": getattr(engine, "target_version", lambda: "0.5.81")()}
    try:
        compat_fn = getattr(updater, "check_router_compatibility", None)
        if callable(compat_fn):
            compat = compat_fn(versions["local"])
    except Exception:
        pass
    return templates.TemplateResponse(request, "index.html", {
        "groups": groups,
        "patch_count": len(states),
        "counts": counts,
        "dead": [s.patch.id for s in states if s.state == "dead-anchor"],
        "error": error,
        "router": probes["router"],
        "headroom": probes["headroom"],
        "local_version": versions["local"],
        "latest_version": versions["latest"],
        "compat": compat,
        "build_path": build_path,
        "scan_seconds": scan["scan_seconds"],
        "now_epoch": scan["at"],           # thời điểm quét thật — "vừa xong" tính từ đó
        "backup_epoch": backup.timestamp() if backup else None,
    })


@app.get("/update")
def update_page(request: Request, probe: str = ""):
    _touch_client()
    if probe == "1" and updater is not None:
        _probe_locks()                  # no-JS path: slow render on purpose (user asked)
    local, latest = _fresh_versions()
    ctx = {
        "local_version": local, "latest_version": latest,
        "target_version": engine.target_version(),
        "locks": LOCK_CACHE["locks"],
        "steps": LAST_UPDATE_STEPS,
        "history": _load_history(),
        "probed_at": LOCK_CACHE["probed_at"] or None,
        "self_update": self_update.state(),
        "auto_update_enabled": self_update.is_enabled(),
    }
    if LOCK_CACHE["error"] is not None:
        ctx["lock_error"] = LOCK_CACHE["error"]
    return templates.TemplateResponse(request, "update.html", ctx)


@app.get("/update/locks")
def update_locks():
    """JSON lock probe for the page's background refresh — same ~37.6s cost, off the render path."""
    _probe_locks()
    # Chỉ trả pid và process name cho client; filesystem path của lock là nội bộ máy
    locks = [{"pid": l.pid, "name": l.name} for l in LOCK_CACHE["locks"]]
    return JSONResponse({"locks": locks, "error": LOCK_CACHE["error"],
                         "probed_at": LOCK_CACHE["probed_at"],
                         "job_running": JOB.running})


@app.post("/settings/auto-update", dependencies=CSRF)
def set_auto_update(request: Request, enabled: Annotated[str, Form()] = "1"):
    """Bật/tắt tự động cập nhật exe nền. Lưu vào settings.json ngay, không cần reload."""
    on = enabled in ("1", "true", "True")
    self_update.set_enabled(on)
    return JSONResponse({"ok": True, "auto_update": on})


@app.post("/update/self", dependencies=CSRF)
def trigger_self_update(request: Request):
    """Kiểm tra + tải + hoán đổi exe ngay theo yêu cầu thủ công. Chiếm OP_SLOT."""
    if not OP_SLOT.acquire("job"):
        return JSONResponse({"ok": False, "error": "update/thao tác khác đang chạy"},
                            status_code=409)
    try:
        meta = self_update.check_update()
        if not meta:
            return JSONResponse({"ok": False, "error": "Không kết nối được máy chủ cập nhật",
                                 "state": self_update.state()})
        if not meta["has_update"]:
            return JSONResponse({"ok": True, "error": None, "message": "Đã là bản mới nhất",
                                 "state": self_update.state()})
        res = self_update.download_and_swap(meta)
        return JSONResponse({"ok": res["ok"], "error": res["error"],
                             "state": self_update.state()})
    finally:
        OP_SLOT.release()


@app.get("/api/self-update/status", include_in_schema=False)
def self_update_status():
    st = self_update.state()
    return JSONResponse({"ok": True, **st})


@app.post("/update/self/restart", dependencies=CSRF, include_in_schema=False)
def self_update_restart(background: BackgroundTasks):
    # BackgroundTasks chạy sau khi response 200 đã gửi — TestClient cũng đợi nó nên test thấy được call.
    # restart_self() kết thúc bằng sys.exit: ngoài main thread nó chỉ giết thread đó, process vẫn sống
    # (đã đo trực tiếp) — nên bắt SystemExit (chỉ frozen path mới raise, sau khi đã spawn exe mới)
    # rồi SIGINT để uvicorn shutdown gracefully, cùng cơ chế với route /shutdown.
    def _restart_after_response():
        try:
            self_update.restart_self()
        except SystemExit:
            os.kill(os.getpid(), signal.SIGINT)
    background.add_task(_restart_after_response)
    return JSONResponse({"ok": True, "message": "Đang khởi động lại ứng dụng..."})


@app.post("/update", dependencies=CSRF)
def update_run(request: Request, autostop: Annotated[str, Form()] = "",
               skip_gate: Annotated[str, Form()] = ""):
    if not OP_SLOT.acquire("job"):
        return JSONResponse({"ok": False, "error": "update/thao tác khác đang chạy"},
                            status_code=409)
    t0 = time.time()
    skip = skip_gate == "1"
    pin = None if skip else engine.target_version()
    try:
        steps = updater.run_update(autostop=autostop == "1", skip_gate=skip, target_pin=pin)
    except Exception as e:
        steps = [{"title": "update failed", "ok": False, "log": str(e)}]
    finally:
        OP_SLOT.release()
    globals()["LAST_UPDATE_STEPS"] = steps
    _record_history(t0, autostop == "1", steps)
    LOCK_CACHE.update(locks=[], error=None, probed_at=0.0)   # npm vừa đụng install — dò lại
    _forget_versions()                  # npm just moved the installed version
    _snap_refresh(scan=True, versions=True)   # install vừa đổi — snapshot mới trước khi render
    with SNAP_LOCK:
        versions = SNAP["versions"]
    return templates.TemplateResponse(request, "update.html", {
        "local_version": versions["local"], "latest_version": versions["latest"],
        "target_version": engine.target_version(),
        "locks": [], "steps": steps,
    })


# ---------------------------------------------------------------- background job + SSE

@app.post("/update/start", dependencies=CSRF)
def update_start(request: Request, autostop: Annotated[str, Form()] = "",
                 skip_gate: Annotated[str, Form()] = ""):
    if updater is None:
        return JSONResponse({"ok": False, "error": "updater module missing"}, status_code=503)
    if not OP_SLOT.acquire("job"):
        return JSONResponse({"ok": False, "error": "update/thao tác khác đang chạy"}, status_code=409)
    with JOB.cond:
        JOB.events = []
        JOB.running = True
        JOB.done = False
    started_at = time.time()
    LOCK_CACHE.update(locks=[], error=None, probed_at=0.0)

    def emit(ev: dict) -> None:
        with JOB.cond:
            JOB.events.append(ev)
            JOB.cond.notify_all()

    def run() -> None:
        global LAST_UPDATE_STEPS
        skip = skip_gate == "1"
        pin = None if skip else engine.target_version()
        try:
            steps = updater.run_update(emit=emit, autostop=autostop == "1",
                                       skip_gate=skip, target_pin=pin)
            LAST_UPDATE_STEPS = steps
            _record_history(started_at, autostop == "1", steps)
            _forget_versions()          # npm vừa đổi version — dashboard không được hiện bản cũ
            _snap_refresh(scan=True, versions=True)   # snapshot mới TRƯỚC khi báo done
            emit({"type": "done", "ok": all(s.ok for s in steps)})
        except Exception as e:          # noqa: BLE001 - job không được chết im lặng
            _forget_versions()
            _snap_refresh(scan=True, versions=True)
            emit({"type": "done", "ok": False, "error": str(e)})
        finally:
            with JOB.cond:
                JOB.done = True
                JOB.running = False
                JOB.cond.notify_all()
            OP_SLOT.release()

    threading.Thread(target=run, name="update-job", daemon=True).start()
    return JSONResponse({"ok": True}, status_code=202)


@app.get("/update/stream")
def update_stream():
    """SSE feed of the running job. Heartbeats keep proxies from closing an idle stream."""
    def gen():
        idx = 0
        while True:
            with JOB.cond:
                if idx >= len(JOB.events) and not JOB.done:
                    JOB.cond.wait(10)
                evs = JOB.events[idx:]
                idx = len(JOB.events)
                done = JOB.done
            for ev in evs:
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            if done:
                if not evs:
                    yield 'data: {"type": "done"}\n\n'
                return
            if not evs:
                yield ": ping\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- actions

def _guard_updater(request: Request):
    if updater is None:
        return _error(request, "updater module missing")
    return None


def _wants_json(request: Request) -> bool:
    """fetch() gửi Accept: application/json; form submit thường (no-JS fallback) thì không."""
    return "application/json" in (request.headers.get("accept") or "")


def _start_action(request: Request, name: str, fn) -> Response:
    """202 + chạy fn(emit) trong thread nền; tiến độ + kết quả xem qua GET /action/status."""
    if updater is None:
        return JSONResponse({"ok": False, "error": "updater module missing"}, status_code=503)
    if not OP_SLOT.acquire("action"):
        return JSONResponse({"ok": False, "error": "update/thao tác khác đang chạy"}, status_code=409)
    with ACTION.lock:
        ACTION.running, ACTION.name, ACTION.done, ACTION.ok = True, name, False, None
        ACTION.lines = []

    def emit(ev: dict) -> None:
        if ev.get("type") != "line":
            return
        with ACTION.lock:
            ACTION.lines.append(str(ev.get("text", "")))
            del ACTION.lines[:-40]

    def run():
        try:
            r = fn(emit)
            with ACTION.lock:
                ACTION.ok = r is not False
        except Exception as e:      # noqa: BLE001 - lỗi phải thấy được qua /action/status
            with ACTION.lock:
                ACTION.ok = False
                ACTION.lines.append(f"{type(e).__name__}: {e}")
        finally:
            with ACTION.lock:
                ACTION.done = True
                ACTION.running = False
            OP_SLOT.release()

    threading.Thread(target=run, name=f"action-{name}", daemon=True).start()
    return JSONResponse({"ok": True, "action": name}, status_code=202)


def _toggle(request: Request, name: str, fn) -> Response:
    """Hai hành vi một route: fetch -> 202 nền; form thường -> chạy xong mới redirect (giữ cũ).
    Cả hai đường đều phải claim OP_SLOT trước khi đụng stack — route sync từng bypass
    toàn bộ guard (2026-09-07 code review)."""
    if _wants_json(request):
        return _start_action(request, name, lambda emit: fn(emit))
    if err := _guard_updater(request):
        return err
    if not OP_SLOT.acquire("action"):
        return JSONResponse({"ok": False, "error": "update/thao tác khác đang chạy"},
                            status_code=409)
    try:
        fn(lambda ev: None)
    finally:
        OP_SLOT.release()
    _snap_refresh(probes=True)          # trạng thái vừa đổi thật — trang redirect phải thấy ngay
    return RedirectResponse("/", status_code=303)


@app.post("/router/stop", dependencies=CSRF)
def router_stop(request: Request):
    return _toggle(request, "router-stop", lambda emit: updater.stop_router_stack(emit))


@app.post("/router/start", dependencies=CSRF)
def router_start(request: Request):
    return _toggle(request, "router-start", lambda emit: updater.start_router_stack(emit))


@app.post("/headroom/stop", dependencies=CSRF)
def headroom_stop(request: Request):
    return _toggle(request, "headroom-stop", lambda emit: updater.stop_headroom(emit))


@app.post("/headroom/start", dependencies=CSRF)
def headroom_start(request: Request):
    return _toggle(request, "headroom-start", lambda emit: updater.start_headroom(emit))


@app.get("/action/status")
def action_status():
    """Live state cho nút Tắt/Bật: tiến độ action + probe từng service (≤0.5s mỗi cái)."""
    with ACTION.lock:
        snap = {"running": ACTION.running, "name": ACTION.name, "done": ACTION.done,
                "ok": ACTION.ok, "lines": list(ACTION.lines[-10:])}
    router, headroom = probe_router(), probe_headroom()
    _snap_put_probes(router, headroom)  # poll thấy trạng thái thật -> snapshot phải theo kịp
    snap["router"], snap["headroom"] = router, headroom
    return JSONResponse(snap)


@app.post("/router/align-target", dependencies=CSRF)
def router_align_target(request: Request):
    """Hạ cấp/cài 9router đúng bản target khi máy đang chạy bản mới hơn."""
    if updater is None:
        return JSONResponse({"ok": False, "error": "updater module missing"}, status_code=503)
    if not OP_SLOT.acquire("action"):
        return JSONResponse({"ok": False, "error": "update/thao tác khác đang chạy"},
                            status_code=409)
    try:
        ok, msg = updater.install_target_router()
    finally:
        OP_SLOT.release()
    _forget_versions()                  # npm vừa đổi bản cài — cache version phải dò lại
    return JSONResponse({"ok": ok, "message": msg})


def _states_or_none() -> list | None:
    """Scan rồi trả states, None nếu không quét được (no build / npm / toml hỏng):
    route phải giữ hành vi cũ thay vì chặn oan."""
    try:
        return engine.scan(engine.build_dir(), engine.load_patches())
    except Exception:
        return None


@app.post("/apply", dependencies=CSRF)
def apply(request: Request, ids: Annotated[list[str] | None, Form()] = None):
    """No ids -> apply everything. An id or group name pulls in its whole group (engine)."""
    try:
        compat_fn = getattr(updater, "check_router_compatibility", None)
        if callable(compat_fn) and not request.query_params.get("force"):
            compat = compat_fn()
            action = {"newer": "hạ cấp về", "older": "nâng cấp lên"}.get(compat.get("relation"))
            if action:
                return _error(request, f"9router v{compat['local']} khác bản vá "
                                       f"(v{compat['target']}). Hãy {action} v{compat['target']} trước.")
    except Exception:
        pass                            # không dò được bản cài -> giữ hành vi cũ (cho apply)
    if not ids:
        # Apply-all gửi từ F12 sau khi đã apply hết: từ chối sớm, không đụng build.
        # partial vẫn còn file clean để apply -> cho qua (engine apply idempotent).
        states = _states_or_none()
        if states is not None and not any(s.state in ("clean", "partial") for s in states):
            return _error(request, "Không còn patch clean nào để apply.")
    try:
        changed = engine.apply(engine.build_dir(), engine.load_patches(), ids=ids or None)
    except Exception as e:              # PatchError, but also EBUSY/PermissionError on
        return _error(request, str(e))  # node_modules and a corrupt patches.toml
    if changed:
        # Build đổi mà router cũ vẫn chạy trong RAM => patch vô dụng (đo 2026-09-23).
        try:
            restart_fn = getattr(updater, "restart_router_stack_if_up", None)
            if callable(restart_fn):
                restart_fn()
        except Exception:
            pass                        # apply đã xong — restart lỗi không được 500
    _snap_refresh(scan=True)            # build vừa đổi — dashboard không được render state cũ
    return RedirectResponse("/", status_code=303)


@app.post("/revert", dependencies=CSRF)
def revert(request: Request, group: Annotated[str, Form()]):
    """Group only (group="all" reverts every group atomically): reverting p6 alone
    while p8 stays breaks the runtime gauge."""
    if group == "all":
        # Revert-all gửi từ F12 khi chưa apply gì: từ chối sớm, không đụng build.
        # partial vẫn có file đã replace -> còn gì đó để revert.
        states = _states_or_none()
        if states is not None and not any(s.state in ("applied", "partial") for s in states):
            return _error(request, "Không còn patch applied nào để revert.")
    try:
        changed = engine.revert(engine.build_dir(), engine.load_patches(),
                                group=None if group == "all" else group)
    except Exception as e:
        return _error(request, str(e))
    if changed:
        # Revert cũng ghi build — cùng bệnh với apply.
        try:
            restart_fn = getattr(updater, "restart_router_stack_if_up", None)
            if callable(restart_fn):
                restart_fn()
        except Exception:
            pass                        # revert đã xong — restart lỗi không được 500
    _snap_refresh(scan=True)            # build vừa đổi — dashboard không được render state cũ
    return RedirectResponse("/", status_code=303)


@app.post("/shutdown", dependencies=CSRF)
def shutdown(request: Request):
    """Stop the dashboard server itself.

    Closing the tab leaves the process running (and holding :20129), so the UI needs an
    explicit exit. SIGINT is what uvicorn treats as a graceful shutdown; the signal is
    raised after the response is flushed, otherwise the client sees a dropped connection."""
    if not OP_SLOT.acquire("shutdown"):
        return JSONResponse({"ok": False, "error": "thao tác khác đang chạy"}, status_code=409)

    def stop():
        time.sleep(0.5)                 # let the 202 reach the browser first
        os.kill(os.getpid(), signal.SIGINT)

    threading.Thread(target=stop, name="shutdown", daemon=True).start()
    return JSONResponse({"ok": True, "action": "shutdown"}, status_code=202)


# secret-looking tokens. Everything that reaches the page goes through sanitize_log_line.

_PATH_RE = re.compile(
    r'[a-zA-Z]:\\[^\s"\'<>]+'                          # C:\...\... (drive path)
    r'|\\(?:[^\s"\'<>\\]+\\)+[^\s"\'<>]*'              # \Users\me\x (relative/UNC)
    r'|/(?:[^\s"\'<>]+/)*(?:node_modules|server|\.next)[^\s"\'<>]*'
    r'|(?:node_modules|server|\.next-cli-build|\.env)[^\s"\'<>]*'   # bare segment
)
_SECRET_RE = re.compile(
    r"(?i)(?:api[_-]?key|secret|token|password|bearer|auth)"
    r"\s*[:=]\s*[\"']?[^\s\"'&]+[\"']?"
    r"|bearer\s+[^\s\"'&]+"
)
_HEX_RE = re.compile(r"\b(?:sha256:[0-9a-f]{16,}|[0-9a-f]{32,}"
                     r"|(?:sk|pk|ghp|gho|xox[bp])[-_][A-Za-z0-9_-]{8,})\b")


def sanitize_log_line(line: str, search_terms=()) -> str:
    """One-line redaction for log output.

    `search_terms` are patch payload lines: any occurrence is a privacy leak, so the
    whole line is dropped rather than scrubbed in place.
    """
    if search_terms and any(t and len(t) >= 5 and t in line for t in search_terms):
        return "[redacted: patch payload]"
    s = _SECRET_RE.sub("[redacted]", line)
    s = _HEX_RE.sub("[redacted]", s)
    return _PATH_RE.sub("[redacted]", s)


# Dòng nào đáng tô đậm: lỗi đỏ / cảnh báo vàng. Chỉ báo loại — renderer (logs.html)
# chuyển thành class; không sửa nội dung nên sanitize vẫn là bất biến của dòng.
_LOG_BAD_RE = re.compile(r"\b(?:ERROR|FATAL|Traceback|Exception|FAIL|EBUSY|WinError)\b", re.I)
_LOG_WARN_RE = re.compile(r"\b(?:WARN|WARNING|CẢNH BÁO|Warn)\b", re.I)


def log_line_kind(line: str) -> str:
    """'err' | 'warn' | '' — cung cấp cho logs.html để tô đậm dòng sự cố."""
    if _LOG_BAD_RE.search(line):
        return "err"
    if _LOG_WARN_RE.search(line):
        return "warn"
    return ""


def _patch_search_terms() -> tuple[str, ...]:
    """Every distinct payload substring the logger should treat as secret.

    Cache theo mtime của file nguồn: mỗi request /logs gọi 1 lần, mà decrypt
    AES-GCM + parse TOML mỗi lần là phí. load_patches() vẫn là source of truth.
    """
    src = engine.PATCHES_ENC_FILE if engine.PATCHES_ENC_FILE.is_file() else engine.PATCHES_FILE
    try:
        stamp = src.stat().st_mtime_ns
    except OSError:
        stamp = 0
    return _terms_for(stamp)


@lru_cache(maxsize=2)
def _terms_for(_stamp: int) -> tuple[str, ...]:
    terms, seen = [], set()
    for p in engine.load_patches():
        for piece in (p.find.splitlines() or [p.find]) + (p.replace.splitlines() or [p.replace]):
            if len(piece) >= 5 and piece not in seen:
                seen.add(piece)
                terms.append(piece)
    return tuple(terms)


def _log_files(log_dir: Path, per_kind_limit: int = 5) -> list[tuple[str, Path]]:
    """Service log files (router-*/headroom-*) newest-first.

    boot.log is deliberately excluded: its content is already the boot section
    (ring buffer), showing it twice would just double the payload.
    per_kind_limit: chỉ lấy N file mới nhất mỗi loại — tránh đọc hàng trăm file
    restart cũ khiến trang /logs phình to megabyte (ponytail: 5 file gần nhất)."""
    files = []
    try:
        candidates = sorted(log_dir.glob("*.log"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return files
    counts: dict[str, int] = {}
    for f in candidates:
        if f.name == "boot.log":
            continue
        if f.name.startswith("router-"):
            kind = "router"
        elif f.name.startswith("headroom-"):
            kind = "headroom"
        else:
            kind = "other"
        if counts.get(kind, 0) >= per_kind_limit:
            continue
        counts[kind] = counts.get(kind, 0) + 1
        files.append((kind, f))
    return files


def _gather_logs() -> dict:
    """Aggregate boot + app/service log lines, history runs, and system context.

    app/router/headroom tách riêng vì log proxy có thể rất dài — trang /logs render
    mỗi nhóm vào một tab tự cuộn thay vì một khối khổng lồ.
    """
    terms = _patch_search_terms()
    boot_logs = [sanitize_log_line(line, terms)
                 for line in boot_doctor.get_boot_logs()]
    buckets: dict[str, list[str]] = {"app": [], "router": [], "headroom": []}
    for kind, f in _log_files(app_paths.get_log_dir()):
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                # ponytail: tail-only, a runaway router log must not be slurped on render;
                # add real paging if a single restart log's tail ever stops being enough
                body = list(deque(fh, maxlen=200))
        except OSError:
            continue
        # 'other' covers the pre-split naming and the app's own app.log -> App tab
        target = buckets.get(kind, buckets["app"])
        for ln in body:
            target.append(sanitize_log_line(ln.rstrip("\r\n"), terms))
    history = []
    for h in _load_history():
        history.append({
            "time_str": h.get("time_str", ""),
            "ok": bool(h.get("ok")),
            "autostop": bool(h.get("autostop")),
            "steps": [{"title": sanitize_log_line(str(s.get("title", "")), terms),
                       "log": sanitize_log_line(str(s.get("log", "")), terms),
                       "ok": bool(s.get("ok"))} for s in h.get("steps", [])],
        })
    local, latest = _fresh_versions()
    context = {
        "python": sys.version.split()[0],
        "os": platform.system() + " " + platform.release(),
        # never the app/home path: the export leaves the machine
        "versions": f"9router local {local} / npm {latest}",
    }
    return {"boot": boot_logs, "history": history, "context": context, **buckets}


def _log_app_path() -> Path:
    """File path for uvicorn runtime log; consumers read this for /logs display."""
    p = app_paths.get_log_dir() / "app.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def get_log_config() -> dict:
    """Access/default logs with timestamps — console hiển thị prompt điều khiển
    menu, uvicorn runtime & access logs được ghi vào file (app.log) cho /logs.
    asctime already carries ',<ms>'; appending %(msecs)03d printed the same ms twice."""
    app_log = str(_log_app_path())
    return {
        "version": 1,
        "disable_existing_loggers": False,
        # Một FileHandler duy nhất cho cả uvicorn + uvicorn.access: hai handler
        # cùng filename có offset riêng → dòng log xé lẫn nhau. AccessFormatter
        # mặc định của uvicorn đã tự format request string, DefaultFormatter đủ.
        "formatters": {
            "default": {
                "()": "uvicorn.logging.DefaultFormatter",
                "fmt": "%(asctime)s %(levelprefix)s %(message)s",
                "datefmt": "%d-%m-%Y %H:%M:%S",
                "use_colors": False,    # Windows: tránh crash isatty() khi stream không phải tty
            },
        },
        "handlers": {
            "default": {"class": "logging.FileHandler", "formatter": "default",
                        "filename": app_log},
        },
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"level": "INFO"},
            "uvicorn.access": {"handlers": ["default"], "level": "INFO", "propagate": False},
        },
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT, log_config=get_log_config())
