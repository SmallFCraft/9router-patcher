"""9router update pipeline: version probe -> lock probe -> (stop) -> npm install -> re-apply patches -> (restart).

Reuses engine for install/build paths and patch application. Locks are reported so a human
decides, unless autostop is requested — then they are captured, stopped, and relaunched
verbatim. `handle64.exe` exits 1 when nothing holds the path - that is
"no locks", not an error; any other failure must raise, because a silent "no locks" is exactly
how the EBUSY surprise happened.
"""
from __future__ import annotations

import ctypes
import http.client
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import app_paths
import engine
from engine import PatchError

REGISTRY_HOST = "registry.npmjs.org"       # allowlist: the only outbound host this tool may use
REGISTRY_PATH = "/9router/latest"
NODE = shutil.which("node")
NPM = shutil.which("npm.cmd") or shutil.which("npm")     # Windows: npm is a .cmd wrapper
# handle64.exe chỉ dùng khi có trên PATH hoặc file tồn tại thật; không hardcode đường dẫn chết.
def _resolve_handle64() -> str | None:
    found = shutil.which("handle64") or shutil.which("handle")
    if found:
        return found
    fallback = r"E:\Apps\Tools\handle64.exe"
    return fallback if os.path.isfile(fallback) else None

HANDLE64 = _resolve_handle64()
REGISTRY_TIMEOUT = 3
# Measured 2026-09-05: 37.6s with an explicit path, 71.8s with cwd="." on a busy box —
# keep generous headroom. Timeout stays mandatory: TimeoutExpired must raise, never read
# as "no locks".
HANDLE_TIMEOUT = 150
NPM_TIMEOUT = 300

ROUTER_PORT = 20128
HEADROOM_PORT = 8787
HEADROOM_PROBE_TIMEOUT = 5.0
# DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: relaunched processes outlive this server, no console
DETACHED_FLAGS = 0x00000008 | 0x00000200
# CREATE_NO_WINDOW: subprocess đồng bộ không bao giờ cấp console window mới (tránh chớp tắt)
SILENT_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
RESTART_LOG_DIR = app_paths.get_log_dir()
TASKKILL_TIMEOUT = 20
CMDLINE_TIMEOUT = 15
RESTART_PORT_WAIT = 20          # seconds max waiting for a restarted service to listen again

# `node.exe   pid: 15168  type: File   64: E:\...\9router\app`
LOCK_LINE = r"^(.+?)\s+pid:\s+(\d+)\s+type:\s+File\s+\S+:\s+(.+?)\s*$"


@dataclass(frozen=True)
class Lock:
    pid: int
    name: str
    path: str


@dataclass
class Step:
    title: str
    ok: bool
    log: str


def _npm_cli() -> list[str] | None:
    """[node, npm-cli.js] — npm is driven through its JS entry with node directly, never through
    the npm.cmd batch wrapper (batch files re-interpret arguments via cmd.exe)."""
    if not (NODE and NPM):
        return None
    cli = Path(NPM).resolve().parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
    if not cli.is_file():
        return None
    return [NODE, os.fspath(cli)]


def current_version() -> str:
    pkg = engine.install_dir() / "package.json"
    return json.loads(pkg.read_text(encoding="utf-8"))["version"]


def _target_version() -> str:
    """Tested 9router pin. Delegates to engine.target_version() once Task 1 lands."""
    fn = getattr(engine, "target_version", None)
    return fn() if callable(fn) else "0.5.81"


def check_router_compatibility(local_ver: str | None = None) -> dict:
    target = _target_version()
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
    target = _target_version()
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


TARBALL_TIMEOUT = 120


def _registry_conn(path: str, timeout: float = REGISTRY_TIMEOUT) -> http.client.HTTPSConnection:
    """Same outbound policy as latest_version: allowlist host, https only, public IPs only.
    Returns a NEW connection per call — tarball streams outlive the metadata request.
    `timeout` là SOCKET timeout trên MỌI recv, không chỉ lúc connect — luồng tarball 20MB
    cần ngân sách lớn hơn hẳn một lần probe metadata (đo 2026-09-19: tarball 0.5.69 chết
    giữa chừng với TimeoutError vì dùng chung 3s của metadata)."""
    infos = socket.getaddrinfo(REGISTRY_HOST, 443, proto=socket.IPPROTO_TCP)
    if not infos or not all(ipaddress.ip_address(i[4][0]).is_global for i in infos):
        raise PatchError(f"{REGISTRY_HOST} does not resolve to a public address")
    return http.client.HTTPSConnection(REGISTRY_HOST, 443, timeout=timeout)


def _registry_meta() -> dict:
    """GET /9router/latest — version + dist.tarball trong cùng một JSON, một lần gọi."""
    conn = _registry_conn(REGISTRY_PATH)
    try:
        conn.request("GET", REGISTRY_PATH)
        r = conn.getresponse()
        body = r.read()
        if r.status != 200:
            raise PatchError(f"registry GET {REGISTRY_PATH} -> HTTP {r.status}: {body[:200]!r}")
        return json.loads(body)
    finally:
        conn.close()


def latest_version() -> str:
    """Phiên bản latest từ registry. Host là allowlist constant, https only, IP resolve
    phải public (không loopback/private/metadata, không follow redirect)."""
    return _registry_meta()["version"]


def _fetch_tarball(url: str, dest_dir: Path) -> Path:
    """Stream tarball npm xuống dest_dir. Cùng policy với latest_version: host phải đúng
    REGISTRY_HOST, https only, IP public, không follow redirect."""
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != REGISTRY_HOST:
        raise PatchError(f"tarball URL không phải https://{REGISTRY_HOST}/...: {url}")
    conn = _registry_conn(parts.path, TARBALL_TIMEOUT)
    dest = dest_dir / "9router-latest.tgz"
    try:
        conn.request("GET", parts.path)
        r = conn.getresponse()
        if r.status != 200:
            raise PatchError(f"tarball GET -> HTTP {r.status}")
        with dest.open("wb") as f:
            while chunk := r.read(65536):
                f.write(chunk)
    finally:
        conn.close()
    return dest


def fetch_latest_build(dest_parent: Path) -> tuple[str, Path]:
    """Tải tarball bản latest vào dest_parent, giải nén, trả (version, build dir).

    Một chỗ duy nhất biết layout tarball (`package/app/.next-cli-build`) — cả gate dry-run
    lẫn `python -m engine locate --latest` đều đi qua đây."""
    meta = _registry_meta()
    tarball = meta.get("dist", {}).get("tarball", "")
    if not tarball:
        raise PatchError("registry metadata không có dist.tarball")
    tgz = _fetch_tarball(tarball, dest_parent)
    with tarfile.open(tgz) as tf:
        tf.extractall(dest_parent, filter="data")
    build = dest_parent / "package" / "app" / ".next-cli-build"
    if not build.is_dir():
        raise PatchError(f"tarball {meta['version']} không chứa app/.next-cli-build")
    return meta["version"], build


DRYRUN_TITLE = "Dò anchor trên bản mới (dry-run)"

def dryrun_anchors(emit=None) -> Step:
    """Gate TRƯỚC npm: tải tarball bản latest, scan anchor của 31 patch trên build trong đó.
    Anchor chết -> pipeline dừng ở đây; install bản cũ còn nguyên, npm chưa đụng vào gì.
    Caller (run_update) chỉ gọi khi local != latest."""
    try:
        with tempfile.TemporaryDirectory(prefix="9r-dryrun-") as td:
            if emit:
                emit({"type": "line", "text": "Tải tarball bản latest…"})
            latest, build = fetch_latest_build(Path(td))
            if emit:
                emit({"type": "line", "text": "Giải nén + scan anchor…"})
            states = engine.scan(build, engine.load_patches())
            dead = [s.patch.id for s in states if s.state == "dead-anchor"]
            for s in states:
                if emit:
                    emit({"type": "line", "text": f"  {s.patch.id}: {s.state}"})
            if dead:
                lines = ["dead-anchor: " + ", ".join(dead)
                         + " — KHÔNG chạy npm. Sửa anchor trong patches.toml trước, "
                           "chạy lại update."]
                try:                    # phụ trợ: chẩn đoán nổ thì gate vẫn phải đỏ gọn gàng
                    for loc in engine.locate(build, engine.load_patches(), ids=dead):
                        where = f"{loc.file}:{loc.offset}" if loc.file else "-"
                        lines.append(f"  {loc.patch_id}: {loc.verdict}  {where}")
                        if loc.snippet:
                            lines.append("    " + loc.snippet[:200].replace("\n", " "))
                except Exception as e:  # noqa: BLE001
                    lines.append(f"  (chẩn đoán locate lỗi: {type(e).__name__}: {e})")
                return Step(DRYRUN_TITLE, False, "\n".join(lines))
            return Step(DRYRUN_TITLE, True,
                        f"{len(states)}/{len(states)} anchor sống trên {latest} — an toàn để update")
    except Exception as e:      # noqa: BLE001 - gate fail phải thành Step, không crash job
        return Step(DRYRUN_TITLE, False,
                    f"{type(e).__name__}: {e} — KHÔNG chạy npm (không dò được bản mới)")


def _get_process_name(pid: int) -> str:
    """Tên tiến trình từ PID bằng Win32 API native (QueryFullProcessImageNameW)."""
    if sys.platform != "win32":
        return "unknown"
    try:
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)     # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return "unknown"
        buf = (wintypes.WCHAR * 1024)()
        size = wintypes.DWORD(1024)
        k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        k32.CloseHandle(h)
        return os.path.basename(buf.value) if buf.value else "unknown"
    except Exception:
        return "unknown"


def _probe_stack_locks() -> list[Lock]:
    """Dò process đang giữ 9router qua cổng mạng (20128 router, 8787 headroom).
    Chạy native trong ~0.05s, không cần binary ngoài, hoạt động trên mọi máy Windows."""
    locks = []
    target = os.fspath(engine.install_dir() / "app")
    for port, default_name in ((ROUTER_PORT, "node.exe"), (HEADROOM_PORT, "python.exe")):
        pid = pid_on_port(port)
        if pid:
            name = _get_process_name(pid) or default_name
            locks.append(Lock(pid=pid, name=name, path=target))
    return locks


def find_locks() -> list[Lock]:
    """Processes holding a handle under the global 9router install. Reports only - never kills.
    Thứ tự:
    1. Nếu có handle64 -> chạy để dò toàn diện cả tiến trình ngoài stack (chậm ~37s).
    2. Nếu không có handle64 -> fallback native: dò router (:20128) & headroom (:8787) qua port.
    """
    if not HANDLE64:
        return _probe_stack_locks()

    import re
    argv = [HANDLE64, "-nobanner", os.fspath(engine.install_dir())]
    try:
        r = subprocess.run(argv, shell=False, stdin=subprocess.DEVNULL,
                           creationflags=SILENT_FLAGS,
                           capture_output=True, text=True, timeout=HANDLE_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        raise PatchError(f"handle64 probe failed ({HANDLE64}): {type(e).__name__}: {e}") from e
    if r.returncode == 1:
        return []                                  # exit 1 = no match, not a failure
    if r.returncode != 0:
        raise PatchError(f"handle64 exit {r.returncode}: {(r.stderr or r.stdout).strip()}")
    out = []
    seen: set[int] = set()
    for line in (r.stdout or "").splitlines():
        m = re.match(LOCK_LINE, line)
        # handle64 liệt kê TỪNG handle (một process giữ chục file = chục dòng trùng PID);
        # report gọn theo process — dedupe theo pid, giữ path đầu tiên.
        if m and int(m[2]) not in seen:
            seen.add(int(m[2]))
            out.append(Lock(pid=int(m[2]), name=m[1], path=m[3]))
    if not out:
        raw = "\n".join(part for part in (r.stdout, r.stderr) if part)
        raise PatchError(f"handle64 exit 0 but parsed no locks:\n{raw}")
    return out


def npm_update(target_pin: str | None = None) -> Step:
    spec = f"9router@{target_pin}" if target_pin else "9router@latest"
    title = f"npm i -g {spec}"
    base = _npm_cli()
    if not base:
        return Step(title, False, "npm-cli.js không tìm thấy (cần node + npm-global trên PATH)")
    cmd = base + ["i", "-g", spec, "--prefer-online"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, creationflags=SILENT_FLAGS,
                           timeout=NPM_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        return Step(title, False, f"{' '.join(cmd)}\n{type(e).__name__}: {e}")
    log = "\n".join(x.strip() for x in (r.stdout, r.stderr) if x and x.strip())
    return Step(title, r.returncode == 0, log or f"exit {r.returncode}")


# ---------------------------------------------------------------- process stop / restart

def _valid_pid(pid) -> int | None:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    return pid if 0 < pid < 2 ** 31 else None


def _process_cmdline(pid: int) -> str | None:
    """Original command line of a running process, via CIM. None = không lấy được (đừng kill mù).
    pid is a strict int, interpolated by %-formatting - never user text reaches the command."""
    pid = _valid_pid(pid)
    if pid is None:
        return None
    fltr = "ProcessId=%d" % pid
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-CimInstance Win32_Process -Filter '%s').CommandLine" % fltr],
        shell=False, capture_output=True, text=True, stdin=subprocess.DEVNULL,
        creationflags=SILENT_FLAGS, timeout=CMDLINE_TIMEOUT)
    return r.stdout.strip() or None


def stop_locks(locks: list[Lock], emit) -> dict[int, tuple[str, str]]:
    """taskkill each lock AFTER capturing its command line. Returns {pid: (name, cmdline)} of the
    processes this tool stopped and therefore owes a restart. Three hard rules, learned the hard
    way: EVERY cmdline is captured BEFORE the first kill (taskkill /T tree-kills other locks'
    processes too — the headroom python died inside node's tree and its cmdline died with it),
    a lock whose CommandLine cannot be read is left alone, and a lock OUTSIDE the install dir is
    never touched — a bad probe must never turn into killing half the system."""
    stopped: dict[int, tuple[str, str]] = {}
    root = os.path.normcase(os.path.normpath(os.fspath(engine.install_dir())))

    candidates: list[tuple[int, str, str]] = []
    for l in locks:
        pid = _valid_pid(l.pid)
        if pid is None:
            emit({"type": "line", "text": f"  BỎ QUA {l.name}: pid không hợp lệ"})
            continue
        lock_path = os.path.normcase(os.path.normpath(l.path or ""))
        if lock_path != root and not lock_path.startswith(root + os.sep):
            emit({"type": "line",
                  "text": f"  BỎ QUA {l.name} pid {pid}: lock không nằm trong install ({l.path})"})
            continue
        cmdline = _process_cmdline(pid)
        if not cmdline:
            emit({"type": "line",
                  "text": f"  BỎ QUA {l.name} pid {pid}: không đọc được command line — không tắt mù"})
            continue
        candidates.append((pid, l.name, cmdline))

    for pid, name, cmdline in candidates:      # phase 2: kill only, cmdlines already safe
        emit({"type": "line", "text": f"  taskkill /PID {pid} /T /F  ({name})"})
        try:
            r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], shell=False,
                               stdin=subprocess.DEVNULL, creationflags=SILENT_FLAGS,
                               capture_output=True, text=True, timeout=TASKKILL_TIMEOUT)
            msg = (r.stdout or r.stderr or "").strip().replace("\n", " | ")
            emit({"type": "line",
                  "text": f"  → exit {r.returncode}{': ' + msg if msg else ''}"})
        except (OSError, subprocess.SubprocessError) as e:
            emit({"type": "line", "text": f"  → taskkill lỗi: {type(e).__name__}: {e}"})
            continue
        if r.returncode == 0:
            stopped[pid] = (name, cmdline)
    return stopped


def _wait_port(port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def _wait_port_closed(port: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                time.sleep(0.3)
        except OSError:
            return True
    return False


def pid_on_port(port: int) -> int | None:
    """PID đang LISTEN trên port (netstat -ano); None nếu không có ai nghe."""
    r = subprocess.run(["netstat", "-ano"], shell=False,
                       stdin=subprocess.DEVNULL, creationflags=SILENT_FLAGS,
                       capture_output=True, text=True, timeout=15)
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3].upper() == "LISTENING" and parts[1].endswith(f":{port}"):
            try:
                return int(parts[4])
            except ValueError:
                continue
    return None


# ---------------------------------------------------------------- router stack on/off

STACK_STATE_FILE = RESTART_LOG_DIR / "router-stack.json"
HEADROOM_CWD = Path(os.environ.get("APPDATA", "")) / "9router" / "headroom"
STACK_PORT_WAIT = 25           # 9router bind ~2s, headroom ~10-15s sau launch


def _runtime_node_modules() -> Path:
    data_dir = os.environ.get("DATA_DIR")
    if data_dir:
        return Path(data_dir) / "runtime" / "node_modules"
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home()) / "9router"
    else:
        base = Path.home() / ".9router"
    return base / "runtime" / "node_modules"


def _build_router_env(base_env: dict | None = None) -> dict:
    """Inject PORT and NODE_PATH so better-sqlite3 resolves from user-writable runtime dir
    (upstream cli.js does this via buildEnvWithRuntime; dashboard launch and restarts must match)."""
    env = dict(base_env if base_env is not None else os.environ)
    env["PORT"] = str(ROUTER_PORT)
    try:
        bundled_nm = str(engine.install_dir() / "app" / "node_modules")
    except Exception:
        bundled_nm = ""
    runtime_nm = str(_runtime_node_modules())
    existing = env.get("NODE_PATH", "")
    env["NODE_PATH"] = os.pathsep.join(filter(None, [runtime_nm, bundled_nm, existing]))
    return env


def _default_router_cmd() -> str | None:
    if not NODE:
        return None
    server = engine.install_dir() / "app" / "custom-server.js"
    return (f"{NODE} --dns-result-order=ipv4first --max-old-space-size=6144 "
            f"{os.fspath(server)}")


def _real_pythonw() -> str | None:
    """pythonw.exe THẬT đã cài (không phải stub WindowsApps 0 byte).

    Stub `%LOCALAPPDATA%\\Microsoft\\WindowsApps\\pythonw.exe` luôn tồn tại và luôn
    `is_file()`, nhưng chỉ mở Microsoft Store — spawn nó sinh cửa sổ Store và
    headroom không bao giờ chạy (đo 2026-09-22). Chỉ nhận path có thật cạnh
    python.exe đang chạy."""
    cand = Path(sys.executable).parent / "pythonw.exe"
    return str(cand) if cand.is_file() else None


def _pythonw_has_headroom(pythonw: str) -> bool:
    """headroom đã cài trong interpreter này chưa.

    headroom là module TUỲ CHỌN: máy chỉ dùng 9router làm proxy thì không cài,
    và lúc đó mọi lần spawn chỉ đẻ ra `ModuleNotFoundError: headroom.cli` trong
    log. Probe một lần (~0.3s) rẻ hơn nhiều so với log rác mỗi lần boot."""
    try:
        r = subprocess.run(
            [pythonw, "-c", "import importlib.util,sys;sys.exit(0 if importlib.util.find_spec('headroom.cli') else 1)"],
            shell=False, stdin=subprocess.DEVNULL, creationflags=SILENT_FLAGS,
            capture_output=True, timeout=HEADROOM_PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


_HEADROOM_STATUS: dict | None = None


def headroom_status(refresh: bool = False) -> dict:
    """Trạng thái headroom của máy này: có pythonw thật không, có headroom không.

    Memo hoá: probe spawn subprocess (~0.3s), mà snapshot worker gọi mỗi 3s.
    headroom cài/gỡ giữa chừng thì khởi động lại app là thấy."""
    global _HEADROOM_STATUS
    if _HEADROOM_STATUS is not None and not refresh:
        return _HEADROOM_STATUS
    pythonw = _real_pythonw()
    if not pythonw:
        _HEADROOM_STATUS = {"installed": False, "pythonw": None,
                            "reason": "Không tìm thấy pythonw.exe (Python GUI subsystem)"}
    elif not _pythonw_has_headroom(pythonw):
        _HEADROOM_STATUS = {"installed": False, "pythonw": pythonw,
                            "reason": "Chưa cài headroom (module tuỳ chọn — pip install headroom-ai)"}
    else:
        _HEADROOM_STATUS = {"installed": True, "pythonw": pythonw, "reason": ""}
    return _HEADROOM_STATUS


def _default_headroom_cmd() -> str | None:
    """Command line for the headroom proxy, None nếu máy chưa cài headroom.

    MUST run via pythonw.exe (GUI subsystem), never via the headroom.exe pip shim.
    The shim is a CONSOLE-subsystem launcher: spawning it detached leaves it without
    a console, so its inner python.exe child AllocConsoles a NEW Windows Terminal
    window — closing that window kills headroom with it (repro 2026-09-21:
    CASCADIA_HOSTING_WINDOW_CLASS visible). pythonw spawns zero windows and the
    detached process survives on its own; stdout still goes to logs/ as before.

    Under Nuitka onefile sys.executable is the temp-dir python, so resolving the
    interpreter relative to it points nowhere: prefer the real installed pythonw
    on PATH (works frozen and in a venv), and only fall back to the
    interpreter-relative location the source build used."""
    pythonw = _real_pythonw()
    if pythonw and _pythonw_has_headroom(pythonw):
        return f'"{pythonw}" -m headroom.cli proxy --port {HEADROOM_PORT} --code-aware'
    return None


def _stack_cmdlines() -> dict:
    try:
        data = json.loads(STACK_STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_stack_cmdlines(data: dict) -> None:
    try:
        RESTART_LOG_DIR.mkdir(exist_ok=True)
        STACK_STATE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass                            # tiện ích — không được làm rơi thao tác tắt/mở


def _kill_port(port: int, name: str, emit) -> bool:
    pid = pid_on_port(port)
    if pid is None:
        emit({"type": "line", "text": f"  {name} (:{port}) không đang chạy — bỏ qua"})
        return True
    cmdline = _process_cmdline(pid)
    if cmdline:
        stack = _stack_cmdlines()
        key = "router_cmd" if port == ROUTER_PORT else "headroom_cmd"
        stack[key] = cmdline
        _save_stack_cmdlines(stack)     # chụp TRƯỚC khi kill để Bật lại y nguyên
    emit({"type": "line", "text": f"  taskkill /PID {pid} /T /F  ({name})"})
    try:
        r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], shell=False,
                           stdin=subprocess.DEVNULL, creationflags=SILENT_FLAGS,
                           capture_output=True, text=True, timeout=TASKKILL_TIMEOUT)
        emit({"type": "line",
              "text": f"  → exit {r.returncode}{': ' + (r.stdout or r.stderr or '').strip() if (r.stdout or r.stderr) else ''}"})
    except (OSError, subprocess.SubprocessError) as e:
        emit({"type": "line", "text": f"  → taskkill lỗi: {type(e).__name__}: {e}"})
        return False
    closed = _wait_port_closed(port)
    emit({"type": "line",
          "text": f"  cổng {port} {'đã đóng' if closed else 'VẪN nghe — kiểm tra lại!'}"})
    return closed


def _launch_port_cmd(kind: str, port: int, cmd: str, cwd: Path | None, emit) -> bool:
    if pid_on_port(port) is not None:
        emit({"type": "line", "text": f"  {kind} đang chạy sẵn ở cổng {port} — bỏ qua"})
        return True
    emit({"type": "line", "text": f"  khởi động {kind} ở cổng {port}"})
    env = dict(os.environ)
    if kind == "router":
        env = _build_router_env(env)
        emit({"type": "line", "text": f"  env PORT={ROUTER_PORT}"})
    try:
        RESTART_LOG_DIR.mkdir(exist_ok=True)
        logfile = RESTART_LOG_DIR / f"{kind}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
        with open(logfile, "ab") as fh:
            subprocess.Popen(cmd, shell=False, cwd=os.fspath(cwd) if cwd else None,
                             stdout=fh, stderr=fh, stdin=subprocess.DEVNULL,
                             creationflags=DETACHED_FLAGS, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        emit({"type": "line", "text": f"  → KHÔNG khởi động được: {type(e).__name__}: {e}"})
        return False
    up = _wait_port(port, STACK_PORT_WAIT)
    emit({"type": "line",
          "text": f"  cổng {port}: {'ĐÃ LÊN' if up else 'chưa nghe — xem log trong logs/'}"})
    return up


def stop_router(emit) -> bool:
    return _kill_port(ROUTER_PORT, "9router", emit)


def start_router(emit) -> bool:
    cmd = _stack_cmdlines().get("router_cmd") or _default_router_cmd()
    if not cmd:
        emit({"type": "line", "text": "  Không tìm thấy node/custom-server.js"})
        return False
    return _launch_port_cmd("router", ROUTER_PORT, cmd, engine.install_dir() / "app", emit)


def stop_headroom(emit) -> bool:
    return _kill_port(HEADROOM_PORT, "headroom", emit)


def start_headroom(emit) -> bool:
    cmd = _stack_cmdlines().get("headroom_cmd") or _default_headroom_cmd()
    if not cmd:
        st = headroom_status()
        emit({"type": "line", "text": f"  Headroom: {st.get('reason') or 'bỏ qua'}"})
        return False
    cwd = HEADROOM_CWD if HEADROOM_CWD.is_dir() else None
    return _launch_port_cmd("headroom", HEADROOM_PORT, cmd, cwd, emit)


def stop_router_stack(emit) -> bool:
    """Tắt LẦN LƯỢT: router trước (chờ cổng đóng), headroom sau — không song song."""
    ok = stop_router(emit)
    emit({"type": "line", "text": "  --"})
    return stop_headroom(emit) and ok


def start_router_stack(emit) -> bool:
    """Bật LẦN LƯỢT: router lên hẳn trước rồi mới tới headroom (nếu có).

    headroom là tuỳ chọn: router lên là stack OK, headroom thiếu không làm fail."""
    ok = start_router(emit)
    emit({"type": "line", "text": "  --"})
    # Nếu user đã từng chạy headroom (có trong saved state) hoặc máy có cài headroom
    has_saved_cmd = bool(_stack_cmdlines().get("headroom_cmd"))
    if not has_saved_cmd and not headroom_status()["installed"]:
        emit({"type": "line", "text": "  Headroom chưa được cài đặt (tuỳ chọn) — bỏ qua"})
        return ok
    h_ok = start_headroom(emit)
    return ok and h_ok


def restart_processes(stopped: dict[int, tuple[str, str]], emit) -> str:
    """Relaunch every process this tool stopped, verbatim command line, detached, output to logs/.
    The command lines come from this machine's own CIM snapshot (trusted local provenance); they
    are sanity-checked and run via CreateProcess (shell=False), never through a shell."""
    RESTART_LOG_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    relaunched = []
    for pid, (name, cmdline) in sorted(stopped.items()):
        if (not isinstance(cmdline, str) or not cmdline.strip() or len(cmdline) > 8192
                or any(c in cmdline for c in "\r\n\x00")):
            emit({"type": "line", "text": f"  BỎ QUA pid {pid}: command line bất thường"})
            continue
        logfile = RESTART_LOG_DIR / f"{Path(name).stem}-{pid}-{stamp}.log"
        env = dict(os.environ)
        if "node" in cmdline.lower():
            # custom-server.js reads PORT from env — the captured command line carries no port,
            # and relaunching without it silently binds 3000 (happened for real 2026-09-06).
            # NODE_PATH too: same runtime dir the dashboard launch injects, else better-sqlite3
            # resolves to nothing and the router falls back to node:sqlite after every update.
            env = _build_router_env(env)
            emit({"type": "line", "text": f"  env PORT={ROUTER_PORT}"})
        emit({"type": "line", "text": f"  khởi động lại: {Path(name).stem}"})
        try:
            with open(logfile, "ab") as fh:
                p = subprocess.Popen(cmdline, shell=False, cwd=engine.install_dir(),
                                     stdout=fh, stderr=fh, stdin=subprocess.DEVNULL,
                                     creationflags=DETACHED_FLAGS, env=env)
            relaunched.append((name, cmdline, p.pid, logfile))
            emit({"type": "line", "text": f"  → PID mới {p.pid}, log: {logfile.name}"})
        except (OSError, subprocess.SubprocessError) as e:
            emit({"type": "line",
                  "text": f"  → KHÔNG khởi động lại được {name}: {type(e).__name__}: {e}"})
    joined = " ".join(cmd for _, cmd, _, _ in relaunched).lower()
    if "node" in joined:
        emit({"type": "line", "text": f"  chờ 9router nghe lại cổng {ROUTER_PORT}…"})
        up = _wait_port(ROUTER_PORT, RESTART_PORT_WAIT)
        emit({"type": "line",
              "text": f"  9router cổng {ROUTER_PORT}: {'ĐÃ LÊN' if up else 'chưa nghe — xem log trong logs/'}"})
    if "python" in joined:
        emit({"type": "line", "text": f"  chờ headroom nghe lại cổng {HEADROOM_PORT}…"})
        up = _wait_port(HEADROOM_PORT, RESTART_PORT_WAIT)
        emit({"type": "line",
              "text": f"  headroom cổng {HEADROOM_PORT}: {'ĐÃ LÊN' if up else 'chưa nghe — xem log trong logs/'}"})
    if not relaunched:
        return "không khởi động lại được process nào"
    return "đã khởi động lại: " + ", ".join(f"{n} (pid {p})" for n, _, p, _ in relaunched)


def _npm_stream(emit, target_pin: str | None = None) -> Step:
    """npm with live output: every line goes to the console as it arrives. Popen has no timeout
    parameter, so a watchdog timer kills npm past NPM_TIMEOUT."""
    spec = f"9router@{target_pin}" if target_pin else "9router@latest"
    title = f"npm i -g {spec}"
    base = _npm_cli()
    if not base:
        return Step(title, False, "npm-cli.js không tìm thấy (cần node + npm-global trên PATH)")
    cmd = base + ["i", "-g", spec, "--prefer-online"]
    try:
        p = subprocess.Popen(cmd, shell=False, stdin=subprocess.DEVNULL,
                             creationflags=SILENT_FLAGS,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, errors="replace", bufsize=1)
    except (OSError, subprocess.SubprocessError) as e:
        return Step(title, False, f"{' '.join(cmd)}\n{type(e).__name__}: {e}")

    state = {"killed": False}

    def _kill():
        state["killed"] = True
        p.kill()
    watchdog = threading.Timer(NPM_TIMEOUT, _kill)
    watchdog.start()
    lines: list[str] = []
    try:
        for raw in p.stdout:                    # type: ignore[union-attr]
            line = raw.rstrip()
            lines.append(line)
            if line:
                emit({"type": "line", "text": line})
        rc = p.wait()
    finally:
        watchdog.cancel()
    log = "\n".join(x for x in lines if x.strip())
    if state["killed"]:
        log = (log + "\n" if log else "") + f"TimeoutExpired: vượt {NPM_TIMEOUT}s, đã kill npm"
    return Step(title, rc == 0 and not state["killed"], log or f"exit {rc}")


# ---------------------------------------------------------------- pipeline

def restore_sqlite(emit) -> str:
    """0.5.6x ships better-sqlite3 as an optionalDependency that npm silently skips when the
    prebuild/build fails — the runtime then logs "[DB] better-sqlite3 unavailable" and falls
    back to node:sqlite. Reinstall it after every version change so the native driver returns
    (measured 2026-09-06: pulls ~570 packages, ~40s)."""
    app = engine.install_dir() / "app"
    binary = app / "node_modules" / "better-sqlite3" / "build" / "Release" / "better_sqlite3.node"
    if binary.is_file():
        return "better-sqlite3 còn nguyên — bỏ qua."
    base = _npm_cli()
    if not base:
        return Step("deps", False, "npm-cli không tìm thấy — runtime sẽ dùng fallback node:sqlite")
    cmd = base + ["i", "better-sqlite3@^12.6.2", "--no-save", "--no-audit", "--no-fund"]
    try:
        r = subprocess.run(cmd, shell=False, cwd=os.fspath(app),
                           stdin=subprocess.DEVNULL, creationflags=SILENT_FLAGS,
                           capture_output=True, text=True, timeout=NPM_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        return Step("deps", False, f"{' '.join(cmd)}\n{type(e).__name__}: {e}")
    ok = r.returncode == 0 and binary.is_file()
    log = "\n".join(x.strip() for x in (r.stdout, r.stderr) if x and x.strip())
    return Step("deps", ok, (log or f"exit {r.returncode}")[-2000:])


def _locks(locks_found: list[Lock]) -> str:
    if not locks_found:
        return "Không process nào đang giữ file trong install."
    body = "\n".join(f"  {l.name}  pid {l.pid}  →  {l.path}" for l in locks_found)
    return "Process đang giữ handle:\n" + body


def _reapply(restarting: bool) -> str:
    build = engine.build_dir()
    patches = engine.load_patches()
    changed = engine.apply(build, patches)
    lines = [f"Áp patch: {len(changed)} file được cập nhật" if changed else "Áp patch: không đổi (đã patch sẵn)"]
    lines += [f"  {s.patch.id}: {s.state} ({len(s.applied_files)} file)"
              for s in engine.scan(build, patches)]
    lines.append("Process sẽ được khởi động lại ở bước kế tiếp."
                 if restarting else
                 "Không tự restart 9router. Tự tắt process 9router cũ rồi chạy lại `9router` "
                 "để build mới + patch mới có hiệu lực.")
    return "\n".join(lines)


def run_update(emit=None, autostop=False, skip_gate=False, target_pin: str | None = None) -> list[Step]:
    """6 bước theo thứ tự plan, dưới cùng một global lock với apply/revert.

    Bước 1–4 chỉ là thông tin: fail thì ghi `ok=False` + lỗi vào log rồi CHẠY TIẾP, vì
    `npm i -g 9router@latest` tự resolve `@latest` — registry không tới được hay thiếu
    handle64 không được chặn update mà người dùng đã bấm. Ngoại lệ: bước dry-run anchor
    (3) fail là CHẶN — anchor chết trên build bản mới thì đừng đụng vào install.
    Bước 5 (npm) fail là fatal: cài dở dang phải để người xem trước khi patch đè lên.
    Với autostop: process giữ lock được tắt trước npm và LUÔN được khởi động lại sau —
    kể cả khi npm fail (không bao giờ để lại máy thiếu proxy).
    skip_gate=True bỏ qua bước dry-run anchor (người dùng chấp nhận rủi ro anchor chết).
    """
    out = emit or (lambda ev: None)
    steps: list[Step] = []
    stopped: dict[int, tuple[str, str]] = {}
    stack_stopped: list[str] = []
    locks_found: list[Lock] = []
    s_local: Step | None = None
    s_latest: Step | None = None

    def do_step(title: str, fn, hint: str = ""):
        out({"type": "step-start", "title": title, "hint": hint})
        try:
            r = fn()
        except Exception as e:      # noqa: BLE001 - log mọi lỗi, đừng crash UI
            r = Step(title, False, f"{type(e).__name__}: {e}")
        step = (Step(title, r.ok, r.log) if isinstance(r, Step)
                else Step(title, True, str(r)))     # numbered title wins over the fn's own
        steps.append(step)
        out({"type": "step-end", "ok": step.ok, "title": title})
        return step

    with engine.LOCK:               # RLock: engine.apply re-acquires it inside step 5
        n = 0

        def nxt(name: str) -> str:
            nonlocal n
            n += 1
            return f"{n}. {name}"

        def step_locks():
            nonlocal locks_found
            locks_found = find_locks()
            return _locks(locks_found)

        def step_stop():
            """Tắt theo CỔNG trước, không dựa vào handle64.

            `locks_found` rỗng cũng có nghĩa là probe lỗi (2026-09-06: handle64 timeout 150s
            -> không tắt gì -> npm EBUSY), và handle64 còn có dương-tính-giả không tái hiện
            được. Cổng 20128/8787 thì đo được trong ~2s và cmdline đã lưu ở router-stack.json
            nên bật lại y nguyên. handle64 chỉ còn việc duy nhất nó làm được: gọi tên holder
            thứ ba ngoài stack.
            """
            nonlocal stopped
            lines, stack_pids = [], set()
            for kind, port, stop_fn in (("9router", ROUTER_PORT, stop_router),
                                        ("headroom", HEADROOM_PORT, stop_headroom)):
                pid = pid_on_port(port)
                if pid is None:
                    lines.append(f"{kind} (:{port}) không chạy — bỏ qua")
                    continue        # người dùng tự tắt: update không được bật hộ
                stack_pids.add(pid)
                if stop_fn(out):
                    stack_stopped.append(kind)
                    lines.append(f"đã tắt {kind} pid {pid} (:{port})")
                else:
                    lines.append(f"KHÔNG tắt được {kind} pid {pid} — npm có thể lỗi EBUSY")
            extra = [l for l in locks_found if l.pid not in stack_pids]
            if extra:
                stopped = stop_locks(extra, out)
                lines.append(f"holder ngoài stack: tắt {len(stopped)}/{len(extra)} process")
            ok = not any("KHÔNG tắt được" in x for x in lines)
            return Step("stop", ok, "\n".join(lines))

        def step_restart(after_fail: bool, unpatched: bool = False):
            lines = []
            for kind, start_fn in (("9router", start_router), ("headroom", start_headroom)):
                if kind in stack_stopped:
                    lines.append(f"{kind}: {'ĐÃ LÊN' if start_fn(out) else 'CHƯA lên — xem logs/'}")
            if stopped:
                lines.append(restart_processes(stopped, out))
            if not lines:
                return "Không có process nào cần khởi động lại."
            if after_fail:
                lines.append("(npm đã lỗi — khôi phục process như cũ)")
            if unpatched:
                lines.append("CẢNH BÁO: bước áp patch FAIL nên 9router vừa lên đang chạy build "
                             "CHƯA patch — SSE sẽ treo lại. Sửa anchor, bấm Apply, rồi restart.")
            return Step("restart", True, "\n".join(lines))

        probed: dict[str, str] = {}

        def ver(fn, key: str):
            v = fn()
            probed[key] = v
            return v

        s_local = do_step(nxt("Phiên bản local"), lambda: f"Local: {ver(current_version, 'local')}",
                          "vài trăm ms")
        s_latest = do_step(nxt("Phiên bản npm latest"),
                           lambda: f"Npm latest: {ver(latest_version, 'latest')}",
                           "~1s (timeout 3s)")
        # Gate dry-run: 2/3 lần update gần nhất fail vì anchor chết SAU khi npm đã cài bản mới
        # (build chưa patch, SSE treo). Tarball chứa .next-cli-build — scan TRƯỚC khi npm đụng
        # vào install. Gate fail là fatal: dừng ngay, trước handle64 (40–70s), trước tắt
        # process, trước npm. Probe version fail -> vẫn chạy gate: "không biết bản mới là gì"
        # không được đọc thành "an toàn để cài".
        same = probed.get("local") is not None and probed.get("local") == probed.get("latest")
        if not skip_gate and not target_pin and not same:
            s_gate = do_step(nxt(DRYRUN_TITLE), lambda: dryrun_anchors(out),
                             "tải tarball (~20MB) + scan, vài chục giây")
            if not s_gate.ok:
                return steps
        do_step(nxt("Dò process đang giữ file install"), step_locks,
                "handle64 thường mất ~40–70s — cứ chờ, đừng đóng trang")
        if autostop:                # KHÔNG gác bằng locks_found: probe rỗng cũng là probe lỗi
            do_step(nxt("Tắt process đang giữ handle"), step_stop, "~2s mỗi process")

        npm_title = f"npm i -g 9router@{target_pin}" if target_pin else "npm i -g 9router@latest"
        if emit is None:
            npm_fn = (lambda: npm_update(target_pin)) if target_pin else npm_update
        else:
            npm_fn = (lambda: _npm_stream(out, target_pin)) if target_pin else (lambda: _npm_stream(out))
        s_npm = do_step(nxt(npm_title), npm_fn,
                        "thường 30–120s, có thể dài hơn — log chảy realtime bên dưới")
        if not s_npm.ok:            # npm fail: không patch đè lên cài dở dang
            if stopped or stack_stopped:
                do_step(nxt("Khởi động lại process đã tắt"), lambda: step_restart(True),
                        "chờ cổng tối đa ~20s")
            return steps

        do_step(nxt("Khôi phục better-sqlite3"), lambda: restore_sqlite(out),
                "chỉ cài khi thiếu (~40s); còn nguyên thì bỏ qua")

        s_patch = do_step(nxt("Áp lại toàn bộ patch"),
                          lambda: _reapply(restarting=bool(stopped or stack_stopped)),
                          "~1–2s (scan + node --check)")
        if stopped or stack_stopped:
            do_step(nxt("Khởi động lại process đã tắt"),
                    lambda: step_restart(False, unpatched=not s_patch.ok),
                    "chờ cổng tối đa ~20s")
    return steps
