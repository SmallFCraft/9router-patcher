"""Tests for build pipeline command assembly and pre-flight checks."""
import json
from pathlib import Path

import build_app


def test_build_sets_clcache_direct_mode():
    """CLCACHE_NODIRECT=0 bật clcache direct mode: warm-cache 360 file từ ~170s -> ~94s.

    Nuitka ép =1 (no-direct: chạy `cl /EP` mỗi file trước khi tra cache) khi biến chưa có,
    nên build() phải tự set =0 trước khi spawn Nuitka — nếu không, warm build chậm gấp đôi.
    """
    src = Path(build_app.__file__).read_text(encoding="utf-8")
    assert 'os.environ.setdefault("CLCACHE_NODIRECT", "0")' in src


def test_requirements_file_exists_and_contains_deps():
    req = Path("requirements.txt")
    assert req.is_file()
    text = req.read_text(encoding="utf-8")
    for dep in ("nuitka", "fastapi", "uvicorn", "jinja2", "cryptography",
                  "python-multipart"):  # Form() routes chết khi boot nếu thiếu (đo 2026-09-22)
        assert dep in text.lower()
    # Nuitka 4.3 có bug PEP 649 deferred annotations -> exe crash NameError 'SetStr' khi boot.
    # 4.3.1 là bản hotfix; 4.3.3 (2026-10-29, chỉ bugfix, không đụng PEP 649) đã build+boot
    # test thật 2026-10-31. Cấm pin về 4.3.0.
    assert "nuitka==4.3.3" in text, "requirements.txt phải pin bản 4.3.3 đã qua test boot thật"



def test_build_app_command_assembly():
    """build_app generates correct Nuitka arguments without syntax errors."""
    cmd = build_app.get_nuitka_cmd(output_dir=Path("dist"))
    assert "--onefile" in cmd
    assert "--windows-console-mode=force" in cmd
    assert "--assume-yes-for-downloads" in cmd
    assert "--onefile-no-compression" not in cmd, "production build must compress payload"
    assert any("templates=templates" in arg for arg in cmd)
    assert any("patches.enc" in arg for arg in cmd)
    assert cmd[-1] == "app.py"


def test_build_app_bundles_tray_icon_into_onefile():
    """Hồi quy 2026-09-23: exe build xong tray hiện icon Windows mặc định vì
    assets/app.ico không được đóng gói — _load_icon fallback IDI_APPLICATION."""
    cmd = build_app.get_nuitka_cmd(output_dir=Path("dist"))
    assert any(
        arg.startswith("--include-data-files=") and arg.endswith("assets/app.ico")
        for arg in cmd
    ), "app.ico phải được include-data-files vào bundle, không thì frozen mode tìm không thấy file"


def test_build_app_skips_heavy_unused_deps():
    """pygments (321 C files, 47% compile) + rich phải bị loại khỏi build."""
    cmd = build_app.get_nuitka_cmd(output_dir=Path("dist"))
    skip = next(a for a in cmd if a.startswith("--nofollow-import-to="))
    mods = skip.split("=", 1)[1].split(",")
    for heavy in ("pygments", "rich"):
        assert heavy in mods, f"{heavy} phải nằm trong nofollow để build không biên dịch nó"
    for must_stay in ("click",):
        assert must_stay not in mods, f"{must_stay} uvicorn import eager — loại là exe chết"


def test_build_app_fast_flag_disables_compression():
    """Dev loop flag: --fast passes --onefile-no-compression to skip ~50s zstd."""
    cmd = build_app.get_nuitka_cmd(output_dir=Path("dist"), fast=True)
    assert "--onefile-no-compression" in cmd


def test_exe_locked_detects_free_and_open_file(tmp_path: Path):
    """_exe_locked returns False when file can be opened for writing, True if locked."""
    f = tmp_path / "dummy.exe"
    f.write_bytes(b"MZ\x00")
    assert build_app._exe_locked(f) is False
    with open(f, "r+b"):
        # Windows file sharing: standard open without explicit deny allows r+b,
        # but function must still not raise
        assert isinstance(build_app._exe_locked(f), bool)
    # Non-existent file raises OSError inside open -> reported as locked
    assert build_app._exe_locked(tmp_path / "nonexistent.exe") is True


def test_build_stages_versioned_exe_and_manifest(tmp_path, monkeypatch):
    """build() stages dist/9router-patch-v<ver>.exe + dist/version.json (không còn files/)."""
    import subprocess as sp

    import version

    root = tmp_path / "repo"
    (root / "dist").mkdir(parents=True)
    (root / "dist" / "9router-patch.exe").write_bytes(b"MZ-fake")
    monkeypatch.setattr(build_app, "ROOT", root)
    monkeypatch.setattr(sp, "run", lambda *a, **k: sp.CompletedProcess(a, 0))
    monkeypatch.setattr(build_app, "_release_dist_exe", lambda p: None)

    assert build_app.build() == 0
    versioned = root / "dist" / f"9router-patch-v{version.APP_VERSION}.exe"
    assert versioned.is_file()
    manifest = json.loads((root / "dist" / "version.json").read_text(encoding="utf-8"))
    assert manifest["url"].endswith(versioned.name)
    assert not (root / "dist" / "files").exists()


def test_build_fails_when_versioned_copy_fails(tmp_path, monkeypatch, capsys):
    """Copy fail (exe bị lock) → return 1, không ghi version.json cho binary thiếu."""
    import subprocess as sp

    root = tmp_path / "repo"
    (root / "dist").mkdir(parents=True)
    (root / "dist" / "9router-patch.exe").write_bytes(b"MZ-fake")
    monkeypatch.setattr(build_app, "ROOT", root)
    monkeypatch.setattr(sp, "run", lambda *a, **k: sp.CompletedProcess(a, 0))
    monkeypatch.setattr(build_app, "_release_dist_exe", lambda p: None)

    def boom(src, dst):
        raise OSError("locked")

    monkeypatch.setattr(build_app.shutil, "copyfile", boom)

    assert build_app.build() == 1
    assert not (root / "dist" / "version.json").exists()
    assert "LỖI" in capsys.readouterr().out


def test_prune_stale_artifacts_keeps_only_current_build(tmp_path):
    """prune_stale_artifacts: chỉ giữ bản vừa build, xóa artifact cũ khớp pattern;
    các file khác (trash.bin) được giữ nguyên."""
    folder = tmp_path / "dist"
    folder.mkdir()
    for name in ("9router-patch.exe", "9router-patch-v2.1.5.exe",
                 "9router-patch-v2.1.4.exe", "trash.bin"):
        (folder / name).write_bytes(b"x")

    removed = build_app.prune_stale_artifacts(folder, {"9router-patch-v2.1.5.exe"},
                                              pattern="9router-patch*.exe")
    assert removed == 2
    assert sorted(p.name for p in folder.iterdir()) == [
        "9router-patch-v2.1.5.exe", "trash.bin"]


def test_write_version_manifest_puts_url_at_base(tmp_path):
    """Manifest ghi root dist/version.json, url trỏ thẳng UPDATE_BASE_URL/<exe>."""
    dist = tmp_path / "dist"
    dist.mkdir(parents=True)
    artifact = dist / "9router-patch-v9.9.9.exe"
    artifact.write_bytes(b"MZ-fake")

    out, manifest = build_app.write_version_manifest(dist, artifact)
    assert out == dist / "version.json"
    assert out.is_file()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data == manifest
    assert data["url"] == f"{build_app.config.UPDATE_BASE_URL}/9router-patch-v9.9.9.exe"
    assert len(data["sha256"]) == 64
    assert data["version"]
