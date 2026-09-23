# tests/test_boot_doctor.py
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
import boot_doctor

def test_check_node_present(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda cmd: "C:\\Program Files\\nodejs\\node.exe" if "node" in cmd else "C:\\Program Files\\nodejs\\npm.cmd")
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="v22.14.0\n")
        ok, msg = boot_doctor.check_node()
        assert ok is True
        assert "v22.14.0" in msg

def test_check_node_missing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda cmd: None)
    ok, msg = boot_doctor.check_node()
    assert ok is False
    assert "nodejs.org" in msg

def test_check_9router_missing(monkeypatch):
    monkeypatch.setattr("engine.install_dir", MagicMock(side_effect=Exception("not found")))
    ok, msg = boot_doctor.check_9router()
    assert ok is False

def test_check_9router_present(monkeypatch):
    monkeypatch.setattr("engine.install_dir", lambda: Path("C:/mock/9router"))
    monkeypatch.setattr("updater.current_version", lambda: "0.5.81")
    ok, msg = boot_doctor.check_9router()
    assert ok is True
    assert "0.5.81" in msg

def test_check_and_apply_patches_skips_when_router_newer_than_target(monkeypatch):
    import boot_doctor, engine, updater
    monkeypatch.setattr(updater, "check_router_compatibility",
                        lambda: {"compatible": False, "relation": "newer", "local": "0.5.85", "target": "0.5.81"})
    calls = []
    monkeypatch.setattr(engine, "apply", lambda *a, **k: calls.append(True))
    ok, msg = boot_doctor.check_and_apply_patches()
    assert ok is False
    assert "mới hơn bản vá" in msg
    assert calls == []  # guard chạy TRƯỚC write — chưa từng chạm disk


def test_check_and_apply_patches_skips_when_router_older_than_target(monkeypatch):
    """Hồi quy incident 2026-09-22: local 0.5.81 + target 0.5.85, guard cũ chỉ chặn
    'newer' nên apply 0.5.85 patch lên build 0.5.81 → 8 dead-anchor, nothing written."""
    import boot_doctor, engine, updater
    monkeypatch.setattr(updater, "check_router_compatibility",
                        lambda: {"compatible": False, "relation": "older",
                                 "local": "0.5.81", "target": "0.5.85"})
    calls = []
    monkeypatch.setattr(engine, "apply", lambda *a, **k: calls.append(True))
    ok, msg = boot_doctor.check_and_apply_patches()
    assert ok is False
    assert "cũ hơn bản vá" in msg and "0.5.85" in msg
    assert calls == []  # guard chạy TRƯỚC write — chưa từng chạm disk


def test_install_9router_builds_versioned_cmd_and_runs_it(monkeypatch):
    """install_9router ghim đúng 9router@<target>, không @latest."""
    import boot_doctor, engine, updater
    monkeypatch.setattr(boot_doctor.shutil, "which", lambda *a, **k: "npm")
    monkeypatch.setattr(updater, "pid_on_port", lambda port: None)
    cmds = []

    class P:
        def __init__(self, cmd, **k):
            cmds.append(cmd)
            self.stdout, self.returncode = [], 0
        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(boot_doctor.subprocess, "Popen", P)
    ok, msg = boot_doctor.install_9router()
    assert ok is True
    assert cmds and cmds[0][-1] == f"9router@{engine.target_version()}"


def test_install_9router_reports_tail_on_npm_failure(monkeypatch):
    """npm exit != 0 → msg chứa 3 dòng cuối (không còn mã exit trần trụi như 4294963214)."""
    import boot_doctor, updater
    monkeypatch.setattr(boot_doctor.shutil, "which", lambda *a, **k: "npm")
    monkeypatch.setattr(updater, "pid_on_port", lambda port: None)
    out = ["npm error code EBUSY\n", "npm error syscall rename\n", "npm error path E:\\x\n"]

    class Lines:
        def __init__(self, lines):
            self._it = iter(lines)
        def readline(self, *a):
            try:
                return next(self._it)
            except StopIteration:
                return ""
        def close(self):
            pass

    class P:
        def __init__(self, cmd, **k):
            self.stdout = Lines(out)
            self.returncode = 4294963214
        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(boot_doctor.subprocess, "Popen", P)
    ok, msg = boot_doctor.install_9router()
    assert ok is False
    assert "EBUSY" in msg and "4294963214" in msg


def test_install_9router_stops_stack_first_and_restarts_after(monkeypatch):
    """EBUSY 2026-09-22: npm rename app/ fail khi router còn nghe port.
    install_9router phải tắt stack trước npm và bật lại sau (kể cả npm fail)."""
    import boot_doctor, updater
    monkeypatch.setattr(boot_doctor.shutil, "which", lambda *a, **k: "npm")
    monkeypatch.setattr(updater, "pid_on_port", lambda port: 1234 if port == 20128 else None)
    calls = []
    monkeypatch.setattr(updater, "stop_router_stack",
                        lambda emit: calls.append("stop") or True)
    monkeypatch.setattr(updater, "start_router_stack",
                        lambda emit: calls.append("start") or True)

    class Lines:
        def readline(self, *a):
            return ""
        def close(self):
            pass

    class P:
        def __init__(self, cmd, **k):
            self.stdout = Lines()
            self.returncode = 0
        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(boot_doctor.subprocess, "Popen", P)
    ok, _ = boot_doctor.install_9router()
    assert ok is True
    assert calls == ["stop", "start"]


def test_install_9router_skips_stack_when_nothing_listening(monkeypatch):
    """Không ai nghe port → không đụng stack."""
    import boot_doctor, updater
    monkeypatch.setattr(boot_doctor.shutil, "which", lambda *a, **k: "npm")
    monkeypatch.setattr(updater, "pid_on_port", lambda port: None)
    calls = []
    monkeypatch.setattr(updater, "stop_router_stack",
                        lambda emit: calls.append("stop") or True)
    monkeypatch.setattr(updater, "start_router_stack",
                        lambda emit: calls.append("start") or True)

    class Lines:
        def readline(self, *a):
            return ""
        def close(self):
            pass

    class P:
        def __init__(self, cmd, **k):
            self.stdout = Lines()
            self.returncode = 0
        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(boot_doctor.subprocess, "Popen", P)
    ok, _ = boot_doctor.install_9router()
    assert ok is True
    assert calls == []


def test_boot_logs_ring_buffer():
    boot_doctor.log_boot("Test line 1")
    logs = boot_doctor.get_boot_logs()
    assert any("Test line 1" in l for l in logs)


def test_run_doctor_banner_shows_app_version(capsys, monkeypatch):
    """Boot header in đúng APP_VERSION hiện tại (2.1.2)."""
    import boot_doctor
    import version

    monkeypatch.setattr(boot_doctor, "check_node", lambda: (True, "ok"))
    monkeypatch.setattr(boot_doctor, "check_9router", lambda: (True, "ok"))
    monkeypatch.setattr(boot_doctor, "check_and_apply_patches", lambda: (True, "ok"))
    monkeypatch.setattr(boot_doctor, "ensure_router_stack", lambda: (True, "ok"))
    monkeypatch.setattr(boot_doctor, "self_update", None, raising=False)

    boot_doctor.run_doctor(interactive=False)
    out = capsys.readouterr().out
    assert f"v{version.APP_VERSION}" in out


def test_boot_check_update_step_reports_results(capsys, monkeypatch):
    """Bước [1/5] kiểm tra exe: mới nhất / có bản mới / mất mạng."""
    import boot_doctor
    import self_update

    monkeypatch.setattr(boot_doctor, "check_node", lambda: (True, "ok"))
    monkeypatch.setattr(boot_doctor, "check_9router", lambda: (True, "ok"))
    monkeypatch.setattr(boot_doctor, "check_and_apply_patches", lambda: (True, "ok"))
    monkeypatch.setattr(boot_doctor, "ensure_router_stack", lambda: (True, "ok"))

    # 1. Có bản mới -> swap -> báo đã cập nhật
    monkeypatch.setattr(self_update, "check_update",
                        lambda url=None: {"version": "9.9.9", "has_update": True,
                                          "url": "https://x/y.exe", "sha256": ""})
    monkeypatch.setattr(self_update, "download_and_swap",
                        lambda meta: {"ok": True, "error": None})
    boot_doctor.run_doctor(interactive=False)
    out = capsys.readouterr().out
    assert "[1/5]" in out
    assert "9.9.9" in out

    # 2. Mất mạng -> bỏ qua, vẫn boot tiếp
    monkeypatch.setattr(self_update, "check_update", lambda url=None: None)
    assert boot_doctor.run_doctor(interactive=False) is True
    out = capsys.readouterr().out
    assert "Bỏ qua" in out


def test_boot_restarts_after_successful_self_update(capsys, monkeypatch):
    """Boot hoán đổi exe mới xong -> gọi restart_self, bản mới có hiệu lực ngay."""
    import boot_doctor
    import self_update

    monkeypatch.setattr(boot_doctor, "check_node", lambda: (True, "ok"))
    restarted = []
    monkeypatch.setattr(self_update, "restart_self",
                        lambda: restarted.append(True) or (_ for _ in ()).throw(SystemExit(0)))
    monkeypatch.setattr(self_update, "check_update",
                        lambda url=None: {"version": "9.9.9", "has_update": True,
                                          "url": "https://x/y.exe", "sha256": ""})
    monkeypatch.setattr(self_update, "download_and_swap",
                        lambda meta: {"ok": True, "error": None})

    with pytest.raises(SystemExit):
        boot_doctor.run_doctor(interactive=False)

    out = capsys.readouterr().out
    assert "Đã cập nhật v9.9.9" in out
    assert "khởi động lại" in out
    assert len(restarted) == 1


def test_ask_yn_defaults_no_on_eof(monkeypatch):
    """Hồi quy 2026-09-23: stdin đóng (pipe/task scheduler) mà mặc định Yes thì
    prompt nâng cấp tự chạy npm install -g khi không có người xác nhận."""
    import boot_doctor
    import console_ui
    monkeypatch.setattr(console_ui, "prompt", lambda icon, q: "")   # EOF trả ""
    assert boot_doctor._ask_yn("Nâng cấp?", default=True) is True   # câu hỏi cài mới
    assert boot_doctor._ask_yn("Nâng cấp?", default=False) is False  # câu đổi bản cài: phải là False


def test_ask_yn_accepts_only_explicit_yes(monkeypatch):
    import boot_doctor
    import console_ui
    for ans, expect in (("y", True), ("Y", True), ("yes", True),
                        ("n", False), ("no", False), ("", True), ("lol", False)):
        monkeypatch.setattr(console_ui, "prompt", lambda icon, q, _a=ans: _a)
        assert boot_doctor._ask_yn("Q?", default=True) is expect, ans


def test_run_doctor_non_interactive_never_asks(monkeypatch):
    """interactive=False (stdin không phải TTY) không được gọi prompt nào."""
    import boot_doctor
    import console_ui, updater
    called = []
    monkeypatch.setattr(console_ui, "prompt", lambda *a, **k: called.append(a) or "")
    monkeypatch.setattr(boot_doctor, "check_node", lambda: (True, "ok"))
    monkeypatch.setattr(boot_doctor, "check_9router", lambda: (True, "ok"))
    monkeypatch.setattr(updater, "check_router_compatibility",
                        lambda: {"compatible": False, "relation": "older",
                                 "local": "0.5.81", "target": "0.5.85"})
    monkeypatch.setattr(boot_doctor, "check_and_apply_patches", lambda: (True, "ok"))
    monkeypatch.setattr(boot_doctor, "ensure_router_stack", lambda: (True, "ok"))
    boot_doctor.run_doctor(interactive=False)
    assert called == []


def test_check_and_apply_patches_restarts_router_when_changed(monkeypatch, tmp_path):
    """Hồi quy 2026-09-23: bước [4/5] auto-apply (sau npm update ở [3/5]) ghi file xong
    mà [5/5] early-return vì cổng vẫn nghe => router chạy build cũ trong RAM, patch vô dụng.
    changed khác rỗng là bắt buộc restart."""
    from types import SimpleNamespace
    import boot_doctor, engine, updater
    monkeypatch.setattr(updater, "check_router_compatibility",
                        lambda: {"compatible": True, "relation": "match",
                                 "local": "0.5.85", "target": "0.5.85"})
    build = tmp_path / "build"
    build.mkdir()
    monkeypatch.setattr(engine, "build_dir", lambda: build)
    monkeypatch.setattr(engine, "load_patches", lambda: [])
    monkeypatch.setattr(engine, "scan",
                        lambda b, p, **k: [engine.PatchState(
                            patch=SimpleNamespace(id="p1"), state="clean",
                            applied_files=[], clean_files=[])])
    monkeypatch.setattr(engine, "apply", lambda *a, **k: ["server/x.js"])
    restarted = []
    monkeypatch.setattr(updater, "restart_router_stack_if_up",
                        lambda emit=None: restarted.append(True) or "đã khởi động lại router")
    ok, msg = boot_doctor.check_and_apply_patches()
    assert ok is True
    assert restarted == [True]
    assert "khởi động lại" in msg


def test_check_and_apply_patches_no_restart_when_build_unchanged(monkeypatch, tmp_path):
    """apply trả [] (không file nào đổi) thì không được kill router — restart vô ích."""
    from types import SimpleNamespace
    import boot_doctor, engine, updater
    monkeypatch.setattr(updater, "check_router_compatibility",
                        lambda: {"compatible": True, "relation": "match",
                                 "local": "0.5.85", "target": "0.5.85"})
    build = tmp_path / "build"
    build.mkdir()
    monkeypatch.setattr(engine, "build_dir", lambda: build)
    monkeypatch.setattr(engine, "load_patches", lambda: [])
    monkeypatch.setattr(engine, "scan",
                        lambda b, p, **k: [engine.PatchState(
                            patch=SimpleNamespace(id="p1"), state="clean",
                            applied_files=[], clean_files=[])])
    monkeypatch.setattr(engine, "apply", lambda *a, **k: [])
    monkeypatch.setattr(updater, "restart_router_stack_if_up",
                        lambda emit=None: (_ for _ in ()).throw(AssertionError("must not restart")))
    ok, msg = boot_doctor.check_and_apply_patches()
    assert ok is True
