"""Updater tests: no network, no real npm, no real handle64, no process ever killed.

Every subprocess/urlopen call is monkeypatched; nothing writes into node_modules.
"""
import http.client
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest

import engine
import updater
from updater import Lock, Step, find_locks, latest_version, npm_update, restart_processes, \
    run_update, stop_locks

# Real handle64 -nobanner output measured on this machine (2 processes holding the install).
REAL_HANDLE_OUT = (
    r"node.exe           pid: 15168  type: File            64: E:\Apps\npm-global\node_modules\9router\app"
    "\n"
    r"python.exe         pid: 16432  type: File            54: E:\Apps\npm-global\node_modules\9router\app"
    "\n"
)
LOCKED_PATH = r"E:\Apps\npm-global\node_modules\9router\app"

BANNER_JUNK = (
    "\nNthandle v5.0 - Handle viewer\n"
    "Copyright (C) 1997-2022 Mark Russinovich\n"
    "Sysinternals - www.sysinternals.com\n\n"
    "No matching handles found.\n"
    "node.exe pid: notanumber type: File 64: E:\\nope\n"
    "random junk line\n"
    "   \n"
)


class Run:
    """Canned subprocess.run: records (cmd, kwargs), returns CompletedProcess per result tuple."""

    def __init__(self, *results):          # results: (returncode, stdout, stderr)
        self.results = list(results)
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        r = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        return subprocess.CompletedProcess(cmd, *r)


class Resp:
    """Minimal urlopen response (context manager + read)."""

    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def raiser(exc):
    def _f(*a, **k):
        raise exc
    return _f


def root_at(monkeypatch, tmp_path):
    """find_locks() probes engine.install_dir(); tests point that at a tmp tree."""
    monkeypatch.setattr(engine, "install_dir", lambda: tmp_path)


# ---------- find_locks: parsing ----------

def test_parse_two_real_handle64_lines(monkeypatch, tmp_path):
    root_at(monkeypatch, tmp_path)
    run = Run((0, REAL_HANDLE_OUT, ""))
    monkeypatch.setattr(subprocess, "run", run)
    locks = find_locks()
    assert [(l.name, l.pid) for l in locks] == [("node.exe", 15168), ("python.exe", 16432)]
    assert all(l.path == LOCKED_PATH for l in locks)
    cmd, kw = run.calls[0]
    assert cmd[1] == "-nobanner" and cmd[2] == str(tmp_path)
    # handle64 measured at ~37.6s with an explicit path, 71.8s on a busy box — see HANDLE_TIMEOUT.
    assert kw["timeout"] == updater.HANDLE_TIMEOUT
    assert updater.HANDLE_TIMEOUT >= 90     # do not "optimize" back under the measured runtime
    assert kw["capture_output"] is True and kw["text"] is True


def test_parse_process_name_with_spaces(monkeypatch, tmp_path):
    root_at(monkeypatch, tmp_path)
    output = r"Node Helper.exe  pid: 42  type: File  64: E:\Apps\9router" + "\n"
    monkeypatch.setattr(subprocess, "run", Run((0, output, "")))
    assert find_locks() == [Lock(pid=42, name="Node Helper.exe", path=r"E:\Apps\9router")]


def test_exit_code_1_means_no_locks_not_an_error(monkeypatch, tmp_path):
    root_at(monkeypatch, tmp_path)
    monkeypatch.setattr(subprocess, "run", Run((1, "", "")))
    assert find_locks() == []


def test_exit_zero_without_parsed_lock_raises_with_raw_output(monkeypatch, tmp_path):
    root_at(monkeypatch, tmp_path)
    monkeypatch.setattr(subprocess, "run", Run((0, BANNER_JUNK, "")))
    with pytest.raises(engine.PatchError) as exc:
        find_locks()
    assert "No matching handles found." in str(exc.value)
    assert "random junk line" in str(exc.value)


def test_garbage_mixed_with_one_real_line(monkeypatch, tmp_path):
    root_at(monkeypatch, tmp_path)
    monkeypatch.setattr(subprocess, "run",
                        Run((0, BANNER_JUNK + REAL_HANDLE_OUT.splitlines()[0] + "\n", "")))
    assert find_locks() == [Lock(pid=15168, name="node.exe", path=LOCKED_PATH)]


# ---------- find_locks: failures must NOT look like "no locks" (EBUSY trap) ----------

def test_missing_handle64_binary_raises(monkeypatch, tmp_path):
    root_at(monkeypatch, tmp_path)
    monkeypatch.setattr(subprocess, "run", raiser(FileNotFoundError(2, "not found")))
    with pytest.raises(engine.PatchError, match="handle64"):
        find_locks()


def test_handle64_timeout_raises(monkeypatch, tmp_path):
    """A timed-out probe must never read as "no locks" - that is how the EBUSY surprise hid."""
    root_at(monkeypatch, tmp_path)
    monkeypatch.setattr(subprocess, "run",
                        raiser(subprocess.TimeoutExpired("handle64", updater.HANDLE_TIMEOUT)))
    with pytest.raises(engine.PatchError, match="handle64") as exc:
        find_locks()
    assert "TimeoutExpired" in str(exc.value)


def test_unexpected_exit_code_raises(monkeypatch, tmp_path):
    root_at(monkeypatch, tmp_path)
    monkeypatch.setattr(subprocess, "run", Run((2, "", "must be run as administrator")))
    with pytest.raises(engine.PatchError, match="administrator"):
        find_locks()


def test_default_target_is_install_dir(monkeypatch):
    install = Path("E:/pkgs/node_modules/9router")
    monkeypatch.setattr(engine, "install_dir", lambda: install)
    run = Run((1, "", ""))
    monkeypatch.setattr(subprocess, "run", run)
    find_locks()
    assert run.calls[0][0][2] == str(install)


# ---------- versions ----------

def test_latest_version_parses_registry_json(monkeypatch):
    seen = {}

    class FakeConn:
        def __init__(self, host, port=None, timeout=None):
            seen.update(host=host, port=port, timeout=timeout)

        def request(self, method, path):
            seen.update(method=method, path=path)

        def getresponse(self):
            class R:
                status = 200
                def read(self):
                    return b'{"name":"9router","version":"0.5.65","dist":{}}'
            return R()

        def close(self):
            pass

    def fake_getaddrinfo(host, port, proto=None):
        return [(2, 1, 6, "", ("151.101.1.162", port))]

    monkeypatch.setattr(updater.socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(http.client, "HTTPSConnection", FakeConn)
    assert latest_version() == "0.5.65"
    assert seen["host"] == "registry.npmjs.org" and seen["path"] == "/9router/latest"
    assert seen["method"] == "GET" and seen["timeout"] == 3


def test_latest_version_blocks_non_public_resolution(monkeypatch):
    """Outbound policy: https + allowlist host + every resolved IP must be public."""
    def evil(host, port, proto=None):
        return [(2, 1, 6, "", ("127.0.0.1", port))]
    monkeypatch.setattr(updater.socket, "getaddrinfo", evil)
    with pytest.raises(engine.PatchError, match="public"):
        latest_version()


def test_current_version_reads_install_package_json(monkeypatch, tmp_path):
    (tmp_path / "package.json").write_text('{"name":"9router","version":"0.5.64"}',
                                           encoding="utf-8")
    monkeypatch.setattr(engine, "install_dir", lambda: tmp_path)
    assert updater.current_version() == "0.5.64"


# ---------- dryrun_anchors (gate trước npm) ----------

# ---------- dryrun_anchors (gate trước npm) ----------

def _make_tgz(tmp_path: Path, relpath: str, content: str) -> Path:
    import tarfile
    root = tmp_path / "pkgroot" / "package" / "app" / ".next-cli-build"
    f = root / relpath
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content, encoding="utf-8")
    tgz = tmp_path / "fake.tgz"
    with tarfile.open(tgz, "w:gz") as tf:
        tf.add(root.parent.parent, arcname="package")   # pkgroot/package -> package/...
    return tgz


def _all_finds() -> str:
    """Chuỗi thật của cả 9 `find` — build chứa đủ thì scan ra 9/9 clean."""
    return "\n".join(p.find for p in engine.load_patches())


def test_dryrun_all_clean_is_ok(monkeypatch):
    monkeypatch.setattr(updater, "_registry_meta",
                        lambda: {"version": "0.5.69",
                                 "dist": {"tarball": "https://registry.npmjs.org/9router/-/x.tgz"}})
    monkeypatch.setattr(updater, "_fetch_tarball",
                        lambda url, d: _make_tgz(d, "server/chunks/a.js", _all_finds()))
    s = updater.dryrun_anchors()
    assert s.ok is True and "an toàn" in s.log


def test_dryrun_dead_anchor_lists_ids_and_fails(monkeypatch):
    """Anchor chết -> gate đỏ + log nêu đúng id, kèm nhắc KHÔNG chạy npm."""
    monkeypatch.setattr(updater, "_registry_meta",
                        lambda: {"version": "0.5.69",
                                 "dist": {"tarball": "https://registry.npmjs.org/9router/-/x.tgz"}})
    monkeypatch.setattr(updater, "_fetch_tarball",
                        lambda url, d: _make_tgz(d, "server/chunks/a.js", "khong co gi"))
    s = updater.dryrun_anchors()
    assert s.ok is False
    assert "connect-timeout-180s" in s.log and "KHÔNG chạy npm" in s.log


def test_dryrun_tarball_without_build_dir_fails(monkeypatch, tmp_path):
    """Tarball không có app/.next-cli-build (dự án đổi cấu trúc) -> lỗi rõ, không crash."""
    import tarfile
    other = tmp_path / "pkgroot" / "package" / "somewhere"
    other.mkdir(parents=True, exist_ok=True)
    (other / "else.js").write_text("x", encoding="utf-8")
    tgz = tmp_path / "fake.tgz"
    with tarfile.open(tgz, "w:gz") as tf:
        tf.add(tmp_path / "pkgroot" / "package", arcname="package")
    monkeypatch.setattr(updater, "_registry_meta",
                        lambda: {"version": "0.5.69",
                                 "dist": {"tarball": "https://registry.npmjs.org/9router/-/x.tgz"}})
    monkeypatch.setattr(updater, "_fetch_tarball", lambda url, d: tgz)
    s = updater.dryrun_anchors()
    assert s.ok is False and "next-cli-build" in s.log


def test_dryrun_metadata_error_becomes_failed_step_not_crash(monkeypatch):
    monkeypatch.setattr(updater, "_registry_meta", raiser(engine.PatchError("HTTP 503")))
    s = updater.dryrun_anchors()
    assert s.ok is False and "HTTP 503" in s.log and "KHÔNG chạy npm" in s.log


def test_fetch_tarball_rejects_non_registry_url(tmp_path):
    with pytest.raises(engine.PatchError, match="registry.npmjs.org"):
        updater._fetch_tarball("https://evil.example.com/9router.tgz", tmp_path)
    with pytest.raises(engine.PatchError, match="https"):
        updater._fetch_tarball("http://registry.npmjs.org/9router.tgz", tmp_path)


def test_fetch_tarball_streams_body(monkeypatch, tmp_path):
    class R:
        status = 200
        def __init__(self, body=b"abc123"):
            self._b, self._off = body, 0
        def read(self, n=-1):
            if self._off >= len(self._b):
                return b""          # EOF: chunked download phải dừng, không lặp vô hạn
            end = len(self._b) if n < 0 else min(self._off + n, len(self._b))
            c = self._b[self._off:end]
            self._off = end
            return c
    class FakeConn:
        def __init__(self, host, port=None, timeout=None):
            pass
        def request(self, method, path):
            pass
        def getresponse(self):
            return R()
        def close(self):
            pass
    monkeypatch.setattr(updater.socket, "getaddrinfo",
                        lambda h, p, proto=None: [(2, 1, 6, "", ("151.101.1.162", p))])
    monkeypatch.setattr(http.client, "HTTPSConnection", FakeConn)
    dest = updater._fetch_tarball("https://registry.npmjs.org/x.tgz", tmp_path)
    assert dest.read_bytes() == b"abc123"


def test_run_update_skips_gate_when_same_version(pipeline):
    """local == latest: không tải tarball 20MB cho update no-op."""
    steps = run_update()
    assert len(steps) == 6 and all(s.ok for s in steps)
    assert not any("dry-run" in s.title for s in steps)


def test_run_update_gate_failure_stops_before_locks_and_npm(pipeline, monkeypatch):
    """Gate đỏ là fatal: dừng ngay — không handle64, không npm, không apply."""
    monkeypatch.setattr(updater, "current_version", lambda: "0.5.69")
    monkeypatch.setattr(updater, "latest_version", lambda: "0.5.70")
    monkeypatch.setattr(updater, "dryrun_anchors",
                        lambda emit=None: Step(updater.DRYRUN_TITLE, False, "dead-anchor: x"))
    steps = run_update()
    assert len(steps) == 3 and steps[2].ok is False
    assert "dead-anchor" in steps[2].log
    assert "apply" not in pipeline


def test_run_update_skip_gate_bypasses(monkeypatch, pipeline):
    """skip_gate=True: version khác nhau vẫn đi thẳng tới npm, không có bước dry-run."""
    monkeypatch.setattr(updater, "current_version", lambda: "0.5.69")
    monkeypatch.setattr(updater, "latest_version", lambda: "0.5.70")
    steps = run_update(skip_gate=True)
    assert len(steps) == 6
    assert not any("dry-run" in s.title for s in steps)
    assert "apply" in pipeline


def test_run_update_runs_gate_when_versions_differ(pipeline, monkeypatch):
    monkeypatch.setattr(updater, "current_version", lambda: "0.5.69")
    monkeypatch.setattr(updater, "latest_version", lambda: "0.5.70")
    monkeypatch.setattr(updater, "dryrun_anchors",
                        lambda emit=None: Step(updater.DRYRUN_TITLE, True, "9/9 anchor sống"))
    steps = run_update()
    assert len(steps) == 7 and steps[2].title.endswith("(dry-run)") and steps[2].ok is True
    assert steps[6].log and "apply" in pipeline


def test_run_update_runs_gate_when_probe_failed(pipeline, monkeypatch):
    """Probe version fail vẫn chạy gate: 'không biết bản mới' không được đọc thành an toàn."""
    monkeypatch.setattr(updater, "current_version", raiser(OSError("no package.json")))
    monkeypatch.setattr(updater, "latest_version", raiser(OSError("no registry")))
    monkeypatch.setattr(updater, "dryrun_anchors",
                        lambda emit=None: Step(updater.DRYRUN_TITLE, True, "gate ok (mock)"))
    steps = run_update()
    assert steps[2].title.endswith("(dry-run)") and steps[2].ok is True
    assert len(steps) == 7


# ---------- npm_update ----------

def test_npm_update_returncode_0_is_ok(monkeypatch):
    run = Run((0, "changed 1 package in 12s", ""))
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(updater, "_npm_cli",
                        lambda: ["C:/node/node.exe", "C:/npm/npm-cli.js"])
    s = npm_update()
    assert isinstance(s, Step) and s.ok
    assert "changed 1 package in 12s" in s.log
    cmd, kw = run.calls[0]
    assert cmd == ["C:/node/node.exe", "C:/npm/npm-cli.js",
                   "i", "-g", "9router@latest", "--prefer-online"]
    assert kw["timeout"] == 300 and kw["capture_output"] is True and kw["text"] is True


def test_npm_update_nonzero_is_failure_with_stderr_in_log(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        Run((1, "npm notice\n", "npm ERR! EBUSY: resource busy or locked")))
    monkeypatch.setattr(updater, "_npm_cli",
                        lambda: ["C:/node/node.exe", "C:/npm/npm-cli.js"])
    s = npm_update()
    assert s.ok is False
    assert "EBUSY: resource busy or locked" in s.log


def test_npm_update_timeout_is_failure_not_crash(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        raiser(subprocess.TimeoutExpired(["node.exe", "i"], 300)))
    monkeypatch.setattr(updater, "_npm_cli",
                        lambda: ["C:/node/node.exe", "C:/npm/npm-cli.js"])
    s = npm_update()
    assert isinstance(s, Step) and s.ok is False
    assert "TimeoutExpired" in s.log and "300" in s.log
    assert "9router@latest" in s.log          # the command that timed out is in the log


def test_npm_update_oserror_is_failure_not_crash(monkeypatch):
    monkeypatch.setattr(subprocess, "run", raiser(OSError(8, "Exec format error")))
    monkeypatch.setattr(updater, "_npm_cli",
                        lambda: ["C:/node/node.exe", "C:/npm/npm-cli.js"])
    s = npm_update()
    assert isinstance(s, Step) and s.ok is False
    assert "Exec format error" in s.log


def test_npm_update_missing_npm_is_failure_not_crash(monkeypatch):
    monkeypatch.setattr(subprocess, "run", raiser(AssertionError("must not run")))
    monkeypatch.setattr(updater, "_npm_cli", lambda: None)
    s = npm_update()
    assert s.ok is False and "npm-cli" in s.log


# ---------- run_update pipeline ----------

@pytest.fixture
def pipeline(monkeypatch, tmp_path):
    """Stub every external effect run_update has; returns a call log tests can assert on."""
    calls = []
    monkeypatch.setattr(subprocess, "run", raiser(AssertionError("no real subprocess")))
    monkeypatch.setattr(updater.socket, "getaddrinfo", raiser(AssertionError("no network")))
    monkeypatch.setattr(http.client, "HTTPSConnection", raiser(AssertionError("no network")))
    monkeypatch.setattr(updater, "current_version", lambda: "0.5.65")
    monkeypatch.setattr(updater, "latest_version", lambda: "0.5.65")
    monkeypatch.setattr(updater, "find_locks", lambda: [])
    monkeypatch.setattr(updater, "npm_update",
                        lambda: Step("npm i -g 9router@latest", True, "up to date"))
    monkeypatch.setattr(updater, "_npm_stream",
                        lambda out: Step("npm i -g 9router@latest", True, "up to date"))
    monkeypatch.setattr(updater, "restore_sqlite",
                        lambda emit: "better-sqlite3 còn nguyên — bỏ qua.")
    monkeypatch.setattr(engine, "build_dir", lambda: tmp_path)
    monkeypatch.setattr(engine, "load_patches", lambda: [])
    monkeypatch.setattr(engine, "apply", lambda *a, **k: calls.append("apply") or [])
    monkeypatch.setattr(engine, "scan", lambda *a, **k: calls.append("scan") or [])
    return calls


def test_run_update_six_steps_same_version_still_reapplies(pipeline):
    """Plan order: 1 local, 2 latest, 3 locks, 4 npm, 5 sqlite, 6 engine.apply (+ restart note)."""
    steps = run_update()
    assert len(steps) == 6
    assert all(s.ok for s in steps)
    assert all(isinstance(s, Step) for s in steps)
    assert "0.5.65" in steps[0].log                     # 1. current_version
    assert "0.5.65" in steps[1].log                     # 2. latest_version
    assert "process" in steps[2].log.lower()            # 3. find_locks (none held here)
    assert "npm i -g 9router@latest" in steps[3].title  # 4. npm_update
    assert "sqlite" in steps[4].title.lower()           # 5. native driver restore
    assert "apply" in pipeline                          # 6. engine.apply
    assert "restart" in steps[5].log.lower()            # reminder is step 6's log, not a 7th step


def test_run_update_holds_global_lock_for_whole_pipeline(pipeline, monkeypatch):
    locked = []

    def npm_while_locked():
        acquired = []

        def probe_from_other_thread():
            got_lock = engine.LOCK.acquire(blocking=False)
            acquired.append(got_lock)
            if got_lock:
                engine.LOCK.release()

        thread = threading.Thread(target=probe_from_other_thread)
        thread.start()
        thread.join()
        locked.append(not acquired[0])
        return Step("npm i -g 9router@latest", True, "up to date")

    monkeypatch.setattr(updater, "npm_update", npm_while_locked)
    assert all(s.ok for s in run_update())
    assert locked == [True]


def test_run_update_does_not_stop_when_locks_found(pipeline, monkeypatch):
    monkeypatch.setattr(updater, "find_locks",
                        lambda: [Lock(15168, "node.exe", LOCKED_PATH),
                                 Lock(16432, "python.exe", LOCKED_PATH)])
    steps = run_update()
    assert len(steps) == 6 and all(s.ok for s in steps)
    assert steps[2].ok is True                 # locks are informational, not a failure
    assert "15168" in steps[2].log and "node.exe" in steps[2].log
    assert "16432" in steps[2].log
    assert "apply" in pipeline                # later steps still ran


def test_run_update_stops_after_failing_npm_step(pipeline, monkeypatch):
    monkeypatch.setattr(updater, "npm_update",
                        lambda: Step("npm i -g 9router@latest", False, "npm ERR! EBUSY"))
    steps = run_update()
    assert len(steps) == 4                     # step 5 absent
    assert [s.ok for s in steps] == [True, True, True, False]
    assert "EBUSY" in steps[-1].log
    assert pipeline == []                      # engine.apply never called


def test_run_update_continues_when_current_version_probe_raises(pipeline, monkeypatch):
    """Probe fail vẫn chạy TIẾP các bước sau gate — nhưng chính gate thì CHẠY (không biết
    version mới là gì không được đọc thành an toàn để cài; fixture chặn network nên gate
    fail fatal, pipeline dừng trước handle64)."""
    monkeypatch.setattr(updater, "current_version", raiser(OSError("package missing")))
    steps = run_update()
    assert len(steps) == 3 and steps[0].ok is False
    assert "package missing" in steps[0].log
    assert steps[2].title.endswith("(dry-run)") and steps[2].ok is False


def test_run_update_continues_when_registry_probe_raises(pipeline, monkeypatch):
    """Registry không tới được không chặn update người dùng đã bấm — nhưng gate vẫn PHẢI chạy
    ('không biết bản mới' != 'an toàn'), nên test giả luôn gate xanh để đi tới npm+apply."""
    monkeypatch.setattr(updater, "latest_version",
                        raiser(OSError("registry unreachable")))
    monkeypatch.setattr(updater, "dryrun_anchors",
                        lambda emit=None: Step(updater.DRYRUN_TITLE, True, "gate ok (mock)"))
    steps = run_update()
    assert len(steps) == 7 and steps[1].ok is False
    assert "registry unreachable" in steps[1].log
    assert steps[2].title.endswith("(dry-run)") and steps[2].ok is True
    assert steps[5].ok is True and "apply" in pipeline


def test_run_update_continues_when_lock_probe_raises(pipeline, monkeypatch):
    monkeypatch.setattr(updater, "find_locks",
                        raiser(engine.PatchError("handle64 not found")))
    steps = run_update()
    assert len(steps) == 6 and steps[2].ok is False
    assert "handle64 not found" in steps[2].log
    assert steps[3].ok is True and "apply" in pipeline


def test_run_update_reapply_failure_is_sixth_and_last_step(pipeline, monkeypatch):
    monkeypatch.setattr(engine, "apply",
                        raiser(engine.PatchError("dead anchor, nothing written: p1")))
    steps = run_update()
    assert len(steps) == 6 and steps[5].ok is False
    assert "dead anchor" in steps[5].log


# ---------- run_update: streaming (emit) ----------

def test_run_update_emits_stream_events(pipeline):
    events = []
    steps = run_update(emit=events.append)
    assert len(steps) == 6
    assert events[0]["type"] == "step-start" and "title" in events[0] and "hint" in events[0]
    assert events[-1]["type"] == "step-end"
    assert {e["type"] for e in events} == {"step-start", "step-end"}   # stub emits no lines


class FakeProc:
    """Minimal Popen double: two stdout lines, nonzero exit."""

    def __init__(self):
        self.stdout = iter(["npm line\n", "second\n"])

    def wait(self):
        return 1

    def kill(self):
        pass


# ---------- run_update: autostop (stop -> npm -> restart) ----------

@pytest.fixture
def stack(monkeypatch):
    """Stub the port-based stack control; returns the log of stop/start calls."""
    log = []
    monkeypatch.setattr(updater, "pid_on_port",
                        lambda port: {updater.ROUTER_PORT: 111,
                                      updater.HEADROOM_PORT: 222}.get(port))
    for name in ("stop_router", "stop_headroom", "start_router", "start_headroom"):
        monkeypatch.setattr(updater, name,
                            lambda emit, _n=name: log.append(_n) or True)
    return log


def test_run_update_autostop_stops_stack_even_when_lock_probe_found_nothing(pipeline, stack):
    """handle64 timed out at 08:22 2026-09-06 -> locks_found=[] -> nothing was stopped ->
    npm died on EBUSY. An empty probe never proves there are no locks, so autostop must
    stop the stack it KNOWS about, by port."""
    steps = run_update(autostop=True)              # fixture stubs find_locks -> []
    assert stack == ["stop_router", "stop_headroom", "start_router", "start_headroom"]
    titles = [s.title for s in steps]
    assert any("Tắt" in t for t in titles) and any("Khởi động lại" in t for t in titles)
    assert all(s.ok for s in steps)


def test_run_update_autostop_leaves_a_service_that_was_not_running_down(pipeline, monkeypatch):
    """Nothing on :8787 -> headroom is neither stopped nor started: update must not launch
    a service the user had deliberately turned off."""
    log = []
    monkeypatch.setattr(updater, "pid_on_port",
                        lambda port: 111 if port == updater.ROUTER_PORT else None)
    for name in ("stop_router", "stop_headroom", "start_router", "start_headroom"):
        monkeypatch.setattr(updater, name, lambda emit, _n=name: log.append(_n) or True)
    run_update(autostop=True)
    assert log == ["stop_router", "start_router"]


def test_run_update_autostop_stops_and_restarts(pipeline, stack, monkeypatch):
    """A lock outside the known stack (pid not on either port) still goes through stop_locks."""
    monkeypatch.setattr(updater, "find_locks",
                        lambda: [Lock(15168, "node.exe", LOCKED_PATH)])
    monkeypatch.setattr(updater, "stop_locks",
                        lambda locks, emit: {15168: ("node.exe", "node custom-server.js")})
    monkeypatch.setattr(updater, "restart_processes",
                        lambda stopped, emit: "đã khởi động lại: node.exe (pid 99)")
    steps = run_update(autostop=True)
    assert len(steps) == 8 and all(s.ok for s in steps)
    titles = " | ".join(s.title for s in steps)
    assert "Tắt process đang giữ handle" in titles
    assert "Khởi động lại process đã tắt" in titles
    assert "npm i -g 9router@latest" in steps[4].title
    assert "sqlite" in steps[5].title.lower()


def test_run_update_autostop_skips_stop_locks_for_the_stack_itself(pipeline, stack, monkeypatch):
    """The router's own pid is stopped by port; feeding it to stop_locks too would taskkill
    a pid that is already dead and record a duplicate restart."""
    monkeypatch.setattr(updater, "find_locks", lambda: [Lock(111, "node.exe", LOCKED_PATH)])
    monkeypatch.setattr(updater, "stop_locks",
                        raiser(AssertionError("stack pid must not reach stop_locks")))
    steps = run_update(autostop=True)
    assert all(s.ok for s in steps)


def test_run_update_autostop_restarts_even_when_npm_fails(pipeline, stack, monkeypatch):
    monkeypatch.setattr(updater, "npm_update",
                        lambda: Step("npm i -g 9router@latest", False, "npm ERR! EBUSY"))
    steps = run_update(autostop=True)
    assert [s.ok for s in steps] == [True, True, True, True, False, True]
    assert "khôi phục" in steps[-1].log           # the proxy never stays down after a failure
    assert "start_router" in stack


def test_run_update_restart_warns_when_reapply_failed(pipeline, stack, monkeypatch):
    """Restarting on an unpatched build hangs SSE again — the log must say so, not just
    'đã khởi động lại'."""
    monkeypatch.setattr(engine, "apply",
                        raiser(engine.PatchError("dead anchor, nothing written: p6")))
    steps = run_update(autostop=True)
    assert steps[-2].ok is False                  # 6. áp lại patch
    assert "CHƯA patch" in steps[-1].log


def test_run_update_without_autostop_never_touches_the_stack(pipeline, monkeypatch):
    """autostop off = hands off: no kill, no launch, whatever the probe said."""
    monkeypatch.setattr(updater, "find_locks",
                        lambda: [Lock(15168, "node.exe", LOCKED_PATH)])
    for name in ("stop_router", "stop_headroom", "start_router", "start_headroom",
                 "stop_locks", "restart_processes"):
        monkeypatch.setattr(updater, name, raiser(AssertionError(f"{name} must not run")))
    steps = run_update(autostop=False)
    assert len(steps) == 6 and all(s.ok for s in steps)


# ---------- stop_locks / restart_processes ----------

def test_stop_locks_skips_when_cmdline_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(engine, "install_dir", lambda: tmp_path)
    monkeypatch.setattr(updater, "_process_cmdline", lambda pid: None)
    monkeypatch.setattr(subprocess, "run", raiser(AssertionError("must not kill without cmdline")))
    events = []
    stopped = stop_locks([Lock(5, "app.exe", str(tmp_path))], events.append)
    assert stopped == {}
    assert any("BỎ QUA" in e["text"] for e in events)


def test_stop_locks_captures_cmdline_then_kills(monkeypatch, tmp_path):
    monkeypatch.setattr(engine, "install_dir", lambda: tmp_path)
    monkeypatch.setattr(updater, "_process_cmdline", lambda pid: "node server.js")
    run = Run((0, "SUCCESS: terminated", ""))
    monkeypatch.setattr(subprocess, "run", run)
    stopped = stop_locks([Lock(15168, "node.exe", str(tmp_path / "app"))], lambda e: None)
    assert stopped == {15168: ("node.exe", "node server.js")}
    assert run.calls[0][0][:2] == ["taskkill", "/PID"]


def test_stop_locks_captures_every_cmdline_before_first_kill(monkeypatch, tmp_path):
    """taskkill /T tree-kills OTHER locks' processes too (headroom died inside node's tree,
    2026-09-06) — so every cmdline must be snapshotted before ANY kill runs."""
    monkeypatch.setattr(engine, "install_dir", lambda: tmp_path)
    cmdlines = {15168: "node custom-server.js", 16432: "python headroom proxy"}
    monkeypatch.setattr(updater, "_process_cmdline", lambda pid: cmdlines.get(pid))
    kill_log = []

    def killing_run(cmd, **kw):
        kill_log.append(cmd[2])             # the /PID argument
        return subprocess.CompletedProcess(cmd, 0, "SUCCESS", "")

    monkeypatch.setattr(subprocess, "run", killing_run)
    stopped = stop_locks([Lock(15168, "node.exe", str(tmp_path / "app")),
                          Lock(16432, "python.exe", str(tmp_path / "app"))], lambda e: None)
    # all cmdlines were read before the first taskkill, so BOTH dead trees get restarted
    assert kill_log == ["15168", "16432"]
    assert stopped == {15168: ("node.exe", "node custom-server.js"),
                       16432: ("python.exe", "python headroom proxy")}


def test_stop_locks_kill_failure_is_not_recorded(monkeypatch, tmp_path):
    monkeypatch.setattr(engine, "install_dir", lambda: tmp_path)
    monkeypatch.setattr(updater, "_process_cmdline", lambda pid: "node server.js")
    monkeypatch.setattr(subprocess, "run", Run((1, "", "access denied")))
    stopped = stop_locks([Lock(9, "node.exe", str(tmp_path / "app"))], lambda e: None)
    assert stopped == {}                          # only what WE stopped gets restarted


def test_stop_locks_never_touches_locks_outside_install(monkeypatch):
    """A broken probe (handle64 noise) must never turn into killing half the system."""
    monkeypatch.setattr(engine, "install_dir",
                        lambda: Path(r"E:\Apps\npm-global\node_modules\9router"))
    monkeypatch.setattr(updater, "_process_cmdline",
                        raiser(AssertionError("foreign lock must not be probed")))
    events = []
    stopped = stop_locks([Lock(999, "explorer.exe", r"C:\Windows\explorer.exe")], events.append)
    assert stopped == {}
    assert any("không nằm trong install" in e["text"] for e in events)


def test_restart_processes_launches_detached(monkeypatch, tmp_path):
    pops = []

    class P:
        pid = 4242
    monkeypatch.setattr(subprocess, "Popen",
                        lambda cmd, **kw: pops.append((cmd, kw)) or P())
    monkeypatch.setattr(updater, "RESTART_LOG_DIR", tmp_path)
    monkeypatch.setattr(engine, "install_dir", lambda: tmp_path)
    monkeypatch.setattr(updater, "_wait_port", lambda port, timeout: True)
    events = []
    out = restart_processes({7: ("node.exe", "node custom-server.js")}, events.append)
    assert "node.exe" in out and "4242" in out
    assert any("node custom-server.js" in e["text"] for e in events)   # cmdline in the log
    cmd, kw = pops[0]
    assert kw["cwd"] == tmp_path and kw["creationflags"] == updater.DETACHED_FLAGS
    assert kw["env"]["PORT"] == str(updater.ROUTER_PORT)   # custom-server reads PORT from env


def test_restart_processes_leaves_env_alone_for_non_node(monkeypatch, tmp_path):
    pops = []

    class P:
        pid = 42
    monkeypatch.setattr(subprocess, "Popen",
                        lambda cmd, **kw: pops.append((cmd, kw)) or P())
    monkeypatch.setattr(updater, "RESTART_LOG_DIR", tmp_path)
    monkeypatch.setattr(engine, "install_dir", lambda: tmp_path)
    monkeypatch.setattr(updater, "_wait_port", lambda port, timeout: True)
    restart_processes({7: ("python.exe", "python proxy --port 8787")}, lambda e: None)
    assert "PORT" not in pops[0][1]["env"]      # headroom's cmdline carries --port already


def test_restart_processes_skips_weird_cmdline(monkeypatch, tmp_path):
    pops = []
    monkeypatch.setattr(subprocess, "Popen",
                        lambda cmd, **kw: pops.append(cmd) or type("P", (), {"pid": 1})())
    monkeypatch.setattr(updater, "RESTART_LOG_DIR", tmp_path)
    monkeypatch.setattr(engine, "install_dir", lambda: tmp_path)
    events = []
    out = restart_processes({7: ("evil.exe", "whatever\r\nformat c:")}, events.append)
    assert pops == [] and "không khởi động lại" in out
    assert any("BỎ QUA" in e["text"] for e in events)


def test_restart_processes_injects_node_path(monkeypatch, tmp_path):
    """A relaunched router must get the same NODE_PATH the dashboard launch injects, else
    better-sqlite3 falls back to node:sqlite after every update."""
    pops = []
    monkeypatch.setattr(subprocess, "Popen",
                        lambda cmd, **kw: pops.append((cmd, kw)) or type("P", (), {"pid": 1})())
    monkeypatch.setattr(updater, "RESTART_LOG_DIR", tmp_path)
    monkeypatch.setattr(engine, "install_dir", lambda: tmp_path)
    monkeypatch.setattr(updater, "_wait_port", lambda port, timeout: True)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    restart_processes({7: ("node.exe", "node custom-server.js")}, lambda e: None)
    parts = pops[0][1]["env"]["NODE_PATH"].split(os.pathsep)
    assert any("runtime" in p and "node_modules" in p for p in parts)
    assert any("app" in p and "node_modules" in p for p in parts)


def test_runtime_node_modules_falls_back_to_home_without_appdata(monkeypatch, tmp_path):
    """APPDATA absent (POSIX, service account, stripped env): must never join onto "" and hand
    Node a relative NODE_PATH that resolves against whatever cwd the child happens to have."""
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    p = updater._runtime_node_modules()
    assert p.is_absolute(), p


def test_build_router_env_survives_missing_install_dir(monkeypatch, tmp_path):
    """npm missing / `npm root -g` failing must not abort the launch: bundled path drops out,
    PORT and the runtime NODE_PATH still land."""
    def boom():
        raise engine.PatchError("npm not found on PATH")
    monkeypatch.setattr(engine, "install_dir", boom)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    env = updater._build_router_env({})
    assert env["PORT"] == str(updater.ROUTER_PORT)
    assert "runtime" in env["NODE_PATH"]


def test_npm_stream_pushes_lines_and_reports_failure(monkeypatch):
    monkeypatch.setattr(updater, "_npm_cli", lambda: ["node", "npm-cli.js"])
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: FakeProc())
    events = []
    s = updater._npm_stream(events.append)
    assert s.ok is False and "npm line" in s.log
    assert {"npm line", "second"} <= {e["text"] for e in events}


# ---------- router stack on/off ----------

def test_pid_on_port_parses_netstat(monkeypatch):
    out = ("\n  TCP    0.0.0.0:20128    0.0.0.0:0    LISTENING    26372"
           "\n  TCP    127.0.0.1:8787    0.0.0.0:0    LISTENING    15244"
           "\n  UDP    0.0.0.0:5353    *:*                         9999\n")
    monkeypatch.setattr(subprocess, "run", Run((0, out, "")))
    assert updater.pid_on_port(20128) == 26372
    assert updater.pid_on_port(8787) == 15244
    assert updater.pid_on_port(9999) is None          # UDP row is not a LISTENING tcp entry
    monkeypatch.setattr(subprocess, "run", Run((0, "", "")))
    assert updater.pid_on_port(80) is None


def test_stop_router_stack_sequential_and_saves_cmdlines(monkeypatch, tmp_path):
    monkeypatch.setattr(engine, "install_dir", lambda: tmp_path)
    monkeypatch.setattr(updater, "STACK_STATE_FILE", tmp_path / "stack.json")
    monkeypatch.setattr(updater, "_wait_port_closed", lambda port, timeout=10.0: True)
    seq = []

    def fake_run(cmd, **kw):
        name = cmd[0]
        if name == "netstat":
            out = ("  TCP    0.0.0.0:20128    0.0.0.0:0    LISTENING    111\n"
                   "  TCP    127.0.0.1:8787     0.0.0.0:0    LISTENING    222\n")
            return subprocess.CompletedProcess(cmd, 0, out, "")
        if name == "powershell":
            pid = "111" if "111" in cmd[-1] else "222"
            return subprocess.CompletedProcess(cmd, 0, f"proc-{pid}.exe --flag", "")
        if name == "taskkill":
            seq.append(cmd[2])
            return subprocess.CompletedProcess(cmd, 0, "SUCCESS", "")
        raise AssertionError(cmd)
    monkeypatch.setattr(subprocess, "run", fake_run)
    events = []
    assert updater.stop_router_stack(events.append)
    assert seq == ["111", "222"]                       # router TRƯỚC, headroom SAU
    saved = json.loads((tmp_path / "stack.json").read_text(encoding="utf-8"))
    assert saved["router_cmd"] == "proc-111.exe --flag"
    assert saved["headroom_cmd"] == "proc-222.exe --flag"


def test_start_router_stack_skips_running_launches_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(engine, "install_dir", lambda: tmp_path)
    monkeypatch.setattr(updater, "STACK_STATE_FILE", tmp_path / "stack.json")
    (tmp_path / "stack.json").write_text(json.dumps({
        "router_cmd": "node custom-server.js",
        "headroom_cmd": "python headroom proxy"}), encoding="utf-8")
    monkeypatch.setattr(updater, "pid_on_port", lambda port: 1 if port == 20128 else None)
    monkeypatch.setattr(updater, "_wait_port", lambda port, timeout: True)
    pops = []
    monkeypatch.setattr(subprocess, "Popen",
                        lambda cmd, **kw: pops.append((cmd, kw))
                        or type("P", (), {"pid": 1})())
    assert updater.start_router_stack(lambda e: None)
    assert len(pops) == 1 and pops[0][0] == "python headroom proxy"   # router skip, headroom launch


def test_start_router_injects_port_env(monkeypatch, tmp_path):
    monkeypatch.setattr(engine, "install_dir", lambda: tmp_path)
    monkeypatch.setattr(updater, "STACK_STATE_FILE", tmp_path / "stack.json")
    monkeypatch.setattr(updater, "pid_on_port", lambda port: None)
    monkeypatch.setattr(updater, "_wait_port", lambda port, timeout: True)
    pops = []
    monkeypatch.setattr(subprocess, "Popen",
                        lambda cmd, **kw: pops.append((cmd, kw))
                        or type("P", (), {"pid": 1})())
    assert updater.start_router(lambda e: None)
    kw = pops[0][1]
    assert kw["env"]["PORT"] == str(updater.ROUTER_PORT)
    assert Path(kw["cwd"]) == tmp_path / "app"


def test_start_router_injects_node_path_with_runtime(monkeypatch, tmp_path):
    """NODE_PATH must contain runtime/node_modules (better-sqlite3) and bundled app/node_modules
    so the Next.js server resolves native deps installed in the user-writable runtime dir."""
    monkeypatch.setattr(engine, "install_dir", lambda: tmp_path)
    monkeypatch.setattr(updater, "STACK_STATE_FILE", tmp_path / "stack.json")
    monkeypatch.setattr(updater, "pid_on_port", lambda port: None)
    monkeypatch.setattr(updater, "_wait_port", lambda port, timeout: True)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    pops = []
    monkeypatch.setattr(subprocess, "Popen",
                        lambda cmd, **kw: pops.append((cmd, kw))
                        or type("P", (), {"pid": 1})())
    assert updater.start_router(lambda e: None)
    node_path = pops[0][1]["env"]["NODE_PATH"]
    parts = node_path.split(os.pathsep)
    assert any("runtime" in p and "node_modules" in p for p in parts), f"runtime missing in {node_path}"
    assert any("app" in p and "node_modules" in p for p in parts), f"bundled missing in {node_path}"


# ---------- fetch_latest_build ----------

def test_fetch_latest_build_returns_version_and_build_dir(monkeypatch, tmp_path):
    """Tarball -> (version, <dest>/package/app/.next-cli-build), the one path both the
    dry-run gate and the locate CLI need."""
    monkeypatch.setattr(updater, "_registry_meta",
                        lambda: {"version": "0.5.99",
                                 "dist": {"tarball": "https://registry.npmjs.org/9router/-/x.tgz"}})
    monkeypatch.setattr(updater, "_fetch_tarball",
                        lambda url, d: _make_tgz(d, "server/chunks/a.js", "hello"))
    ver, build = updater.fetch_latest_build(tmp_path)
    assert ver == "0.5.99"
    assert build == tmp_path / "package" / "app" / ".next-cli-build"
    assert (build / "server" / "chunks" / "a.js").is_file()


def test_fetch_latest_build_raises_when_build_dir_missing(monkeypatch, tmp_path):
    import tarfile
    other = tmp_path / "pkgroot" / "package" / "somewhere"
    other.mkdir(parents=True, exist_ok=True)
    (other / "else.js").write_text("x", encoding="utf-8")
    tgz = tmp_path / "fake.tgz"
    with tarfile.open(tgz, "w:gz") as tf:
        tf.add(tmp_path / "pkgroot" / "package", arcname="package")
    monkeypatch.setattr(updater, "_registry_meta",
                        lambda: {"version": "0.5.99",
                                 "dist": {"tarball": "https://registry.npmjs.org/9router/-/x.tgz"}})
    monkeypatch.setattr(updater, "_fetch_tarball", lambda url, d: tgz)
    with pytest.raises(engine.PatchError, match="next-cli-build"):
        updater.fetch_latest_build(tmp_path)


def test_fetch_latest_build_raises_when_tarball_url_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "_registry_meta", lambda: {"version": "0.5.99", "dist": {}})
    with pytest.raises(engine.PatchError, match="dist.tarball"):
        updater.fetch_latest_build(tmp_path)
