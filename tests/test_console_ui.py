"""Tests for console_ui: styles, VAN backfill, throttle, cache invalidation."""
from __future__ import annotations

import pytest

import console_ui


def test_clean_output_without_tty(capsys, monkeypatch):
    """Stdout pipe/CI (không TTY): không còn escape code, nội dung đầy đủ."""
    monkeypatch.setattr(console_ui.sys.stdout, "isatty", lambda: False)
    console_ui._VT = None
    import version

    console_ui.header("9router Patch Manager", f"v{version.APP_VERSION}")
    out = capsys.readouterr().out
    assert "\033[" not in out
    assert "9router Patch Manager" in out
    assert f"v{version.APP_VERSION}" in out
    console_ui._VT = None


def test_steps_print_one_line_each(capsys, monkeypatch):
    """5 bước + xác nhận: mỗi dòng có nhãn [N/M], nội dung và trạng thái. [0/5]
    là nhãn của bước tự cập nhật."""
    monkeypatch.setattr(console_ui.sys.stdout, "isatty", lambda: False)
    console_ui._VT = None

    console_ui.header("9router Patch Manager", "v2.1.5")
    console_ui.step_begin("[0/5]", "Kiểm tra bản cập nhật exe")
    console_ui.step_end("MỚI NHẤT v2.1.5", "info")
    console_ui.step_begin("[1/5]", "Kiểm tra Node.js & npm")
    console_ui.step_end("OK", "ok")
    console_ui.step_begin("[2/5]", "Kiểm tra 9router toàn cục")
    console_ui.step_end("OK", "ok")
    console_ui.step_begin("[3/5]", "Kiểm tra patches tối ưu")
    console_ui.step_end("OK", "ok")
    console_ui.step_begin("[4/5]", "Khởi động Proxy Router Stack")
    console_ui.step_end("OK", "ok")
    console_ui.success("Hoàn tất chuẩn bị!", "đang khởi động Web Dashboard...")

    out = capsys.readouterr().out
    for num in ("[0/5]", "[1/5]", "[2/5]", "[3/5]", "[4/5]"):
        assert num in out
    assert "MỚI NHẤT v2.1.5" in out
    assert "Hoàn tất chuẩn bị!" in out
    assert "\033[" not in out
    console_ui._VT = None


def test_no_color_env_disables_styling(capsys, monkeypatch):
    """NO_COLOR có hiệu lực ngay cả khi stdout là TTY."""
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(console_ui.sys.stdout, "isatty", lambda: True)
    console_ui._VT = None

    console_ui.header("X", "v1.0.0")
    console_ui.panel([("Dashboard", "http://127.0.0.1:20129")])
    out = capsys.readouterr().out
    assert "\033[" not in out
    assert "Dashboard" in out
    console_ui._VT = None


def test_ascii_glyphs_when_encoding_cannot_handle_unicode(monkeypatch):
    """Encoding cp1252 (không hỗ trợ ╭─│): rẽ sang bộ ASCII (+-|).

    `sys.stdout.encoding` là thuộc tính read-only — phải thay cả object stdout."""
    class Cp1252Stdout:
        encoding = "cp1252"

    monkeypatch.setattr(console_ui.sys, "stdout", Cp1252Stdout())
    g = console_ui.glyphs()
    assert g["tl"] == "+"
    assert g["h"] == "-"


def test_panel_render_keys_and_rows(capsys, monkeypatch):
    """Panel in đầy đủ URL + phím tắt, không còn escape."""
    monkeypatch.setattr(console_ui.sys.stdout, "isatty", lambda: False)
    console_ui._VT = None

    url = "http://127.0.0.1:20129"
    console_ui.panel(
        [("Dashboard", url), ("Logs", f"{url}/logs")],
        keys=[("Enter", "Mở Dashboard"), ("L", "Xem Logs"),
              ("H", "Ẩn Console"), ("Q", "Thoát")],
    )
    out = capsys.readouterr().out
    assert url in out
    assert f"{url}/logs" in out
    for key in ("Enter", "L", "H", "Q"):
        assert f"[{key}]" in out
    console_ui._VT = None


def test_npm_run_success_line_has_target_and_check(capsys, monkeypatch):
    """npm_run thay dòng trạng thái dài bằng 1 dòng: arrow + version + ✓."""
    monkeypatch.setattr(console_ui.sys.stdout, "isatty", lambda: False)
    console_ui._VT = None
    ok, msg = console_ui.npm_run("0.5.85", lambda on_output=None: (True, "done"))
    out = capsys.readouterr().out
    assert ok is True
    assert "9router@0.5.85" in out
    assert "đã sẵn sàng" in out
    assert "\033[" not in out
    console_ui._VT = None


def test_npm_run_failure_shows_cause_and_dashboard_hint(capsys, monkeypatch):
    """Fail: in nguyên nhân thật (EBUSY...) + chỉ đường Dashboard, không nuốt lỗi."""
    monkeypatch.setattr(console_ui.sys.stdout, "isatty", lambda: False)
    console_ui._VT = None
    logged = []
    ok, msg = console_ui.npm_run(
        "0.5.85",
        lambda on_output=None: (False, "npm cài 9router@0.5.85 thất bại (exit 1): EBUSY rename app"),
        log_fn=logged.append)
    out = capsys.readouterr().out
    assert ok is False
    assert "EBUSY" in out
    assert "Dashboard" in out
    assert logged and "EBUSY" in logged[0]
    assert "\033[" not in out
    console_ui._VT = None


def test_npm_run_streams_output_through_callback(capsys, monkeypatch):
    """Output npm chảy qua on_output, không bị bỏ khi không TTY."""
    monkeypatch.setattr(console_ui.sys.stdout, "isatty", lambda: False)
    console_ui._VT = None

    def installer(on_output=None):
        on_output("npm error code EBUSY")
        return False, "exit 1"

    console_ui.npm_run("0.5.85", installer)
    out = capsys.readouterr().out
    assert "EBUSY" in out
    console_ui._VT = None


def test_glyph_fallback_covers_all_runtime_icons(monkeypatch):
    """Hồi quy 2026-09-23: cp1252 + glyphs() fallback — mọi icon runtime
    (warn/skip/cross/info) phải có bản ASCII, không chỉ 11 box glyph."""

    class Cp1252Stdout:
        encoding = "cp1252"

    monkeypatch.setattr(console_ui.sys, "stdout", Cp1252Stdout())
    assert console_ui.unicode_ok() is False
    g = console_ui.glyphs()
    assert g is console_ui._ASCII_GLYPHS
    # mọi key mà code runtime tra cứu phải tồn tại và encode được cp1252
    for key in ("check", "cross", "caret", "dot", "info", "warn", "skip", "v", "h", "tl"):
        assert key in g, key
        g[key].encode("cp1252", "strict")


def test_unicode_glyphs_have_matching_ascii_keys():
    """Hai bộ glyph phải cùng key — thiếu key là KeyError lúc runtime trên console cũ."""
    assert set(console_ui._UNICODE_GLYPHS) == set(console_ui._ASCII_GLYPHS)


def test_status_line_never_exceeds_width(capsys, monkeypatch):
    """Hồi quy 2026-09-23: dòng idle 102 ký tự tràn — status_line phải cắt."""
    monkeypatch.setattr(console_ui.sys.stdout, "isatty", lambda: False)
    console_ui._VT = None
    long_text = ("Đang chạy: router :20128 · patch 34/34 đã áp (9router v0.5.85) "
                 "· Enter mở dashboard · H ẩn · Q thoát")
    assert len(long_text) > 78
    console_ui.status_line(long_text)
    out = capsys.readouterr().out.rstrip("\n")
    assert len(out) <= console_ui.width() - 2, out
    console_ui._VT = None


def test_status_line_swallows_console_oserror(monkeypatch):
    """Hồi quy 2026-10-10: exe onefile console từ chối write với OSError 22.
    status_line là dòng trang trí — nuốt OSError, không được để sập boot. app.py
    gọi lần hai trong except, nếu status_line ném lại thì lọt ra _fatal."""
    monkeypatch.setattr(console_ui.sys.stdout, "isatty", lambda: False)
    console_ui._VT = None

    def boom(*a, **k):
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr("builtins.print", boom)
    console_ui.status_line("Đang chạy · Enter mở dashboard")   # không được ném
    console_ui._VT = None


def test_download_run_renders_progress_non_tty(capsys, monkeypatch):
    """Môi trường non-TTY (pipe/log file): in mốc sạch 25/50/75/100%, không escape \\r."""
    monkeypatch.setattr(console_ui.sys.stdout, "isatty", lambda: False)
    console_ui._VT = None

    def fake_dl(on_progress=None):
        if on_progress:
            on_progress(25, 100)
            on_progress(50, 100)
            on_progress(75, 100)
            on_progress(100, 100)
        return {"ok": True, "error": None}

    res = console_ui.download_run("Tải exe mới", fake_dl)
    assert res["ok"] is True
    out = capsys.readouterr().out
    assert "Tải exe mới" in out
    assert "50%" in out
    assert "100%" in out
    assert "\r" not in out
    console_ui._VT = None


def test_download_run_handles_failure(capsys, monkeypatch):
    """Khi download thất bại: in ✗ đỏ và trả nguyên kết quả lỗi."""
    monkeypatch.setattr(console_ui.sys.stdout, "isatty", lambda: False)
    console_ui._VT = None

    def fail_dl(on_progress=None):
        return {"ok": False, "error": "Mất kết nối server"}

    res = console_ui.download_run("Tải exe mới", fail_dl)
    assert res["ok"] is False
    out = capsys.readouterr().out
    assert "Mất kết nối server" in out
    assert "✗" in out or "x" in out
    console_ui._VT = None


def test_download_run_renders_live_bar_and_clamps_overflow(capsys, monkeypatch):
    """TTY thật: vẽ thanh 1 dòng bằng \\r; download vượt Content-Length không tràn bar/pct."""
    monkeypatch.setattr(console_ui.sys.stdout, "isatty", lambda: True)
    console_ui._VT = None

    def fake_dl(on_progress=None):
        on_progress(50, 100)     # giữa chừng
        on_progress(120, 100)    # vượt total: phải clamp về 100% / bar 16 ký tự
        return {"ok": True, "error": None}

    console_ui.download_run("Tải exe mới", fake_dl)
    out = capsys.readouterr().out
    assert "\r" in out
    assert "125%" not in out
    bar = out.rsplit("[", 1)[1].split("]", 1)[0]
    assert len(bar) == 16, bar
    console_ui._VT = None


def test_download_run_clears_line_when_downloader_raises(capsys, monkeypatch):
    """Downloader ném exception: dòng \\r vẫn phải được xóa (try/finally)."""
    monkeypatch.setattr(console_ui.sys.stdout, "isatty", lambda: True)
    console_ui._VT = None

    def boom(on_progress=None):
        raise KeyError("url")

    with pytest.raises(KeyError):
        console_ui.download_run("Tải exe mới", boom)
    out = capsys.readouterr().out
    assert out.endswith("\r"), repr(out[-40:])
    console_ui._VT = None
