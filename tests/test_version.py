"""Tests for app versioning and CLI arguments."""
from __future__ import annotations

import pytest


def test_app_version_format():
    """APP_VERSION phải là chuỗi semver chuẩn (X.Y.Z)."""
    import version
    assert hasattr(version, "APP_VERSION")
    assert version.APP_VERSION == "2.0.0"
    parts = version.APP_VERSION.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def test_parse_args_defaults():
    """Mặc định không có flag: force=False."""
    import app
    args = app._parse_args([])
    assert args.force is False


def test_parse_args_force():
    """Flag --force đặt force=True."""
    import app
    args = app._parse_args(["--force"])
    assert args.force is True


def test_cli_version_flag_exits_cleanly(capsys):
    """--version và -v in ra APP_VERSION và thoát exit 0."""
    import app
    import version

    with pytest.raises(SystemExit) as exc_info:
        app._parse_args(["--version"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert version.APP_VERSION in captured.out

    with pytest.raises(SystemExit) as exc_info:
        app._parse_args(["-v"])
    assert exc_info.value.code == 0


def test_console_banner_contains_app_version(capsys):
    """Banner khởi động in đúng v2.0.0."""
    import app
    import version
    app._console_banner("http://127.0.0.1:20129")
    captured = capsys.readouterr()
    assert f"v{version.APP_VERSION}" in captured.out


def test_main_force_bypasses_port_busy_early_exit(monkeypatch):
    """Khi --force được bật, main() không return sớm tại _port_busy mà đi tiếp."""
    import app

    monkeypatch.setattr(app, "_port_busy", lambda host, port: True)
    doctor_called = []
    monkeypatch.setattr(app.boot_doctor, "run_doctor", lambda interactive=True: doctor_called.append(True) or False)

    # 1. Không có --force: early exit ngay, run_doctor không được gọi
    app.main([])
    assert len(doctor_called) == 0

    # 2. Có --force: bỏ qua early exit, run_doctor ĐƯỢC gọi
    app.main(["--force"])
    assert len(doctor_called) == 1


def test_web_header_renders_app_version(monkeypatch, tmp_path):
    """Web UI hiển thị v2.0.0 trên header (brand)."""
    from starlette.testclient import TestClient
    import main
    import version

    own_url = f"http://{main.HOST}:{main.PORT}"
    client = TestClient(main.app, base_url=own_url)
    res = client.get("/")
    assert res.status_code == 200
    assert f"v{version.APP_VERSION}" in res.text


def test_build_app_version_flags_are_four_part(tmp_path):
    """Nuitka --file-version / --product-version chỉ nhận tối đa 4 số, không chuỗi.

    Regression: nếu ai đó đổi APP_VERSION thành "2.0.0-beta" build sẽ chết ở
    postprocessing; test này bắt trước khi tốn 160s compile.
    """
    import build_app

    cmd = build_app.get_nuitka_cmd(tmp_path)
    flags = [a for a in cmd if a.startswith(("--file-version=", "--product-version="))]
    assert len(flags) == 2, flags
    for flag in flags:
        value = flag.split("=", 1)[1]
        parts = value.split(".")
        assert 2 <= len(parts) <= 4, flag
        assert all(p.isdigit() for p in parts), flag

