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
    """Bước [0/5] kiểm tra exe: mới nhất / có bản mới / mất mạng."""
    import boot_doctor
    import self_update

    monkeypatch.setattr(boot_doctor, "check_node", lambda: (True, "ok"))
    monkeypatch.setattr(boot_doctor, "check_9router", lambda: (True, "ok"))
    monkeypatch.setattr(boot_doctor, "check_and_apply_patches", lambda: (True, "ok"))
    monkeypatch.setattr(boot_doctor, "ensure_router_stack", lambda: (True, "ok"))

    # 1. Có bản mới -> swap -> báo đã tải
    monkeypatch.setattr(self_update, "check_update",
                        lambda url=None: {"version": "9.9.9", "has_update": True,
                                          "url": "https://x/y.exe", "sha256": ""})
    monkeypatch.setattr(self_update, "download_and_swap",
                        lambda meta: {"ok": True, "error": None})
    boot_doctor.run_doctor(interactive=False)
    out = capsys.readouterr().out
    assert "[0/5]" in out
    assert "9.9.9" in out

    # 2. Mất mạng -> bỏ qua, vẫn boot tiếp
    monkeypatch.setattr(self_update, "check_update", lambda url=None: None)
    assert boot_doctor.run_doctor(interactive=False) is True
    out = capsys.readouterr().out
    assert "BỎ QUA" in out


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
    assert "ĐÃ CẬP NHẬT v9.9.9" in out
    assert "khởi động lại" in out
    assert len(restarted) == 1
