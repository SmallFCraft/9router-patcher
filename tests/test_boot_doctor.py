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

def test_boot_logs_ring_buffer():
    boot_doctor.log_boot("Test line 1")
    logs = boot_doctor.get_boot_logs()
    assert any("Test line 1" in l for l in logs)
