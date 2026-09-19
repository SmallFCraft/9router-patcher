"""Engine tests on a fake build tree under tmp_path. Never touches real node_modules."""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest

import engine
from engine import BACKUP_ROOT, PatchError, apply, groups, load_patches, revert, scan

PATCHES_FILE = ROOT / "patches.toml"


@pytest.fixture(scope="session")
def patches():
    return load_patches(PATCHES_FILE)


@pytest.fixture(autouse=True)
def backup_root(tmp_path, monkeypatch):
    root = tmp_path / "backups"
    root.mkdir()
    monkeypatch.setattr(engine, "BACKUP_ROOT", root)
    return root


def by_id(ps, pid):
    return next(p for p in ps if p.id == pid)


def state_for(scanres, pid):
    return next(s for s in scanres if s.patch.id == pid)


def write(build, rel, text):
    p = build / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def read(path):
    return Path(path).read_text(encoding="utf-8")


def ok_check(log=None):
    def _c(path):
        if log is not None:
            log.append(str(path))
    return _c


def sse(ps):
    return [p for p in ps if p.group == "sse-hang"]


class RecordingStr(str):
    """A str that logs every `.replace(old, new)` and keeps logging down the chain.

    The only way to observe the order the engine really composes a group in: final bytes are
    order-independent (all 24 permutations of sse-hang give one identical result, plan.md), so
    a bytes assertion cannot catch a wrong sequence.
    """
    def __new__(cls, value, log):
        s = super().__new__(cls, value)
        s.log = log
        return s

    def replace(self, old, new, *a):
        self.log.append(old)
        return RecordingStr(str.replace(self, old, new, *a), self.log)


def record_replaces(monkeypatch, log):
    real_read = engine._read
    monkeypatch.setattr(engine, "_read", lambda f: RecordingStr(real_read(f), log))


def touched_ids(log, ps):
    """Patch ids in the exact order the engine rewrote them (`find` on apply, `replace` on revert)."""
    by_str = {p.find: p.id for p in ps} | {p.replace: p.id for p in ps}
    return [by_str[s] for s in log]


def link_dir(target: Path, link: Path) -> None:
    """Create a directory link; skip the test when the platform refuses to make one."""
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError:
        pass                       # Windows without Developer Mode: WinError 1314
    if os.name != "nt":
        pytest.skip("cannot create a directory symlink here")
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if r.returncode != 0 or not link.is_dir():
        pytest.skip(f"cannot create a junction: {(r.stderr or r.stdout).strip()}")


def unlink_dir(link: Path) -> None:
    try:
        os.unlink(link)            # POSIX symlink
    except OSError:
        os.rmdir(link)             # Windows junction / directory symlink


# ---------- structural ----------

def test_load_patches_real_file(patches):
    # P31 responses-thinking-history-400 (AgentRouter thinking-replay 400) added
    assert len(patches) == 31
    assert [p.order for p in patches] == sorted(p.order for p in patches)
    assert patches[0].id == "connect-timeout-180s"
    for a in ("id", "order", "group", "summary", "why", "find", "replace"):
        assert hasattr(patches[0], a)
    with pytest.raises(AttributeError):
        patches[0].id = "nope"  # frozen
    # group defaults to id for standalone patches
    assert by_id(patches, "ua-messages").group == "ua-messages"
    sg = sse(patches)
    assert len(sg) == 4
    assert [p.id for p in sg] == [
        "sse-close-translate", "sse-close-passthrough", "gauge-guard", "gauge-flush-route",
    ]
    grouped = groups(patches)
    assert len(grouped) == 27  # 23 standalone + sse-hang + nonstream-sse-retry + claude-system-hoist + errbody-html-title + responses-thinking-history-400 + opencode-freetier-tool-signature
    ns = [p for p in patches if p.group == "nonstream-sse-retry"]
    assert [p.id for p in ns] == ["nonstream-retry-exec", "nonstream-retry-aggregate"]
    assert by_id(patches, "claude-system-hoist").group == "claude-system-hoist"


# ---------- scan ----------

def test_no_patch_drops_a_binding_its_anchor_declared(patches):
    """Sập thật 2026-09-06: remap `find` sang 0.5.69 mà giữ `replace` của 0.5.65 → p8 khai
    `O=()=>` thay vì `P=()=>`, xoá luôn binding `P`. `node --check` pass (P chỉ là định danh),
    nhưng 4 lời gọi `P()` còn lại thành ReferenceError lúc chạy: SSE không đóng, gauge kẹt.
    Mọi tên `x=` trong `find` phải còn được khai trong `replace`."""
    decl = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*)=(?!=)")
    for p in patches:
        assert not set(decl.findall(p.find)) - set(decl.findall(p.replace)), p.id


def test_scan_empty_build_dead_anchor(patches, tmp_path):
    b = tmp_path / "build"
    b.mkdir()
    s = state_for(scan(b, patches), "connect-timeout-180s")
    assert s.state == "dead-anchor"
    assert s.applied_files == [] and s.clean_files == []


def test_scan_clean_applied_partial(patches, tmp_path):
    b = tmp_path / "build"
    b.mkdir()
    p1 = by_id(patches, "connect-timeout-180s")
    write(b, "a.js", f"x {p1.find} y")               # clean
    s = state_for(scan(b, [p1]), p1.id)
    assert (s.state, len(s.applied_files), len(s.clean_files)) == ("clean", 0, 1)
    write(b, "b.js", f"x {p1.replace} y")            # already applied
    s = state_for(scan(b, [p1]), p1.id)
    assert (s.state, len(s.applied_files), len(s.clean_files)) == ("partial", 1, 1)
    write(b, "c.js", f"x {p1.replace} y")            # second applied file
    s = state_for(scan(b, [p1]), p1.id)
    assert (s.state, len(s.applied_files), len(s.clean_files)) == ("partial", 2, 1)
    # remove the clean file -> all applied
    (b / "a.js").unlink()
    s = state_for(scan(b, [p1]), p1.id)
    assert (s.state, len(s.applied_files), len(s.clean_files)) == ("applied", 2, 0)


def test_scan_counts_file_once_not_occurrence(patches, tmp_path):
    b = tmp_path / "build"
    b.mkdir()
    p1 = by_id(patches, "connect-timeout-180s")
    write(b, "a.js", f"{p1.find} {p1.find}")          # two occurrences, one file
    s = state_for(scan(b, [p1]), p1.id)
    assert s.clean_files == ["a.js"]


def test_scan_p2_replace_contains_find_is_applied(patches, tmp_path):
    """p2's replacement embeds its own find; a patched file must read applied, not clean/partial."""
    b = tmp_path / "build"
    b.mkdir()
    p2 = by_id(patches, "ua-messages")
    assert p2.find in p2.replace  # precondition from real data
    write(b, "chunk.js", f"AAA{p2.replace}ZZZ")       # patched state
    s = state_for(scan(b, [p2]), p2.id)
    assert s.state == "applied"
    assert s.applied_files == ["chunk.js"] and s.clean_files == []


def test_scan_only_js_files(patches, tmp_path):
    b = tmp_path / "build"
    b.mkdir()
    p1 = by_id(patches, "connect-timeout-180s")
    write(b, "app.js", f"x {p1.find} y")
    write(b, "notes.txt", f"x {p1.find} y")
    write(b, "cfg.json", f"x {p1.find} y")
    s = state_for(scan(b, [p1]), p1.id)
    assert s.clean_files == ["app.js"]


def test_js_files_return_resolved_contained_paths_only(tmp_path):
    """FINDING 3: later backup/write/verify/rollback must use the resolved path that
    passed containment, never a mutable directory link alias."""
    b = tmp_path / "build"
    inside = b / "inside"
    outside = tmp_path / "outside"
    inside.mkdir(parents=True)
    outside.mkdir()
    direct = write(b, "inside/chunk.js", "inside")
    write(outside, "outside.js", "outside")
    link_dir(inside, b / "alias-inside")
    link_dir(outside, b / "alias-outside")

    files = engine._js_files(b.resolve())

    assert files == [direct.resolve()]               # duplicate alias deduplicated
    assert all(f == f.resolve() and f.is_relative_to(b.resolve()) for f in files)


# ---------- apply ----------

def test_apply_scan_applied_and_idempotent(patches, tmp_path):
    b = tmp_path / "build"
    b.mkdir()
    p1 = by_id(patches, "connect-timeout-180s")
    write(b, "x.js", f"const s = `{p1.find}`;\n")
    log = []
    changed = apply(b, patches, ids=[p1.id], check=ok_check(log))
    assert changed == ["x.js"]
    t = read(b / "x.js")
    assert p1.find not in t and p1.replace in t
    assert state_for(scan(b, patches), p1.id).state == "applied"
    assert len(log) == 1  # checked exactly the one written file
    before = t
    assert apply(b, patches, ids=[p1.id], check=ok_check()) == []
    assert read(b / "x.js") == before  # byte-identical


def test_apply_unknown_id_raises(patches, tmp_path):
    b = tmp_path / "build"
    b.mkdir()
    with pytest.raises(PatchError, match="unknown"):
        apply(b, patches, ids=["does-not-exist"], check=ok_check())


def test_apply_missing_build_dir_raises(patches, tmp_path):
    with pytest.raises(PatchError):
        apply(tmp_path / "absent", [by_id(patches, "connect-timeout-180s")],
              check=ok_check())


def test_apply_dead_anchor_writes_nothing(patches, tmp_path, backup_root):
    b = tmp_path / "build"
    b.mkdir()
    p1 = by_id(patches, "connect-timeout-180s")
    write(b, "x.js", "no anchor anywhere here")
    before = read(b / "x.js")
    log = []
    with pytest.raises(PatchError):
        apply(b, patches, ids=[p1.id], check=ok_check(log))
    assert read(b / "x.js") == before
    assert log == []
    assert list(backup_root.iterdir()) == []  # no snapshot, no write


def test_apply_ignores_non_js(patches, tmp_path):
    b = tmp_path / "build"
    b.mkdir()
    p1 = by_id(patches, "connect-timeout-180s")
    write(b, "sub/data.js", f"A{p1.find}B")
    write(b, "cfg.json", f"A{p1.find}B")
    changed = apply(b, patches, ids=[p1.id], check=ok_check())
    assert changed == ["sub/data.js"]
    assert p1.find in read(b / "cfg.json")  # untouched
    assert p1.find not in read(b / "sub/data.js")


def test_apply_missing_anchor_in_group_aborts_all(patches, tmp_path, backup_root):
    """sse-hang needs all 4 anchors; drop one -> whole group aborts, nothing written."""
    b = tmp_path / "build"
    b.mkdir()
    p6, p7, p8, p9 = sse(patches)
    text = f"A{p6.find}B{p8.find}C{p9.find}D"  # no p7 find anywhere
    write(b, "8895.js", text)
    with pytest.raises(PatchError):
        apply(b, patches, ids=[p6.id], check=ok_check())
    assert read(b / "8895.js") == text
    assert p6.replace not in read(b / "8895.js")
    assert list(backup_root.iterdir()) == []


def test_apply_id_expands_whole_group(patches, tmp_path):
    b = tmp_path / "build"
    b.mkdir()
    p6, p7, p8, p9 = sse(patches)
    write(b, "8895.js", f"A{p6.find}B{p7.find}C{p8.find}D{p9.find}E")
    changed = apply(b, patches, ids=[p6.id], check=ok_check())  # one id, whole group
    assert changed == ["8895.js"]
    t = read(b / "8895.js")
    for p in (p6, p7, p8, p9):
        assert p.replace in t and p.find not in t


def test_apply_accepts_group_name_as_selector(patches, tmp_path):
    b = tmp_path / "build"
    b.mkdir()
    p6, p7, p8, p9 = sse(patches)
    write(b, "8895.js", f"{p6.find}|{p7.find}|{p8.find}|{p9.find}")
    changed = apply(b, patches, ids=["sse-hang"], check=ok_check())
    assert changed == ["8895.js"]


def test_apply_one_dead_group_aborts_other_live_group(patches, tmp_path, backup_root):
    """Two selected groups, one dead -> no file in the live group is written either."""
    b = tmp_path / "build"
    b.mkdir()
    p1 = by_id(patches, "connect-timeout-180s")
    p2 = by_id(patches, "ua-messages")
    write(b, "x.js", f"A{p1.find}B")          # p1 live
    write(b, "y.js", "no p2 anchor")           # p2 dead
    text = read(b / "x.js")
    with pytest.raises(PatchError):
        apply(b, patches, check=ok_check())    # ids=None -> all 6 groups
    assert read(b / "x.js") == text
    assert p1.replace not in read(b / "x.js")
    assert list(backup_root.iterdir()) == []  # preflight aborts before any snapshot/write
    assert p2.replace not in read(b / "y.js")


def test_apply_creates_one_snapshot_across_many_groups(patches, tmp_path, backup_root):
    """Four real file groups touched by ids=None; exactly one backup dir, then retention holds."""
    b = tmp_path / "build"
    b.mkdir()
    p2, p3, p4, p5 = [by_id(patches, i) for i in
                       ("ua-messages", "ua-models-route", "ua-models-agg", "tools-strip-custom")]
    for i, p in enumerate((p2, p3, p4, p5)):
        write(b, f"f{i}.js", f"A{p.find}Z")
    changed = apply(b, patches, ids=[p2.id, p3.id, p4.id, p5.id], check=ok_check())
    assert sorted(changed) == ["f0.js", "f1.js", "f2.js", "f3.js"]
    assert len([d for d in backup_root.iterdir() if d.is_dir()]) == 1
    for p in (p2, p3, p4, p5):
        assert state_for(scan(b, [p]), p.id).state == "applied"


def test_apply_link_repoint_cannot_modify_outside_build(patches, tmp_path, monkeypatch):
    """FINDING 3: a link inside the build passes containment at enumeration, then gets
    repointed outside before the write. Every later write/backup must use the resolved
    path that was proven contained. Skip where the platform refuses to make a link."""
    b = tmp_path / "build"
    real = b / "real"
    outside = tmp_path / "outside"
    real.mkdir(parents=True)
    outside.mkdir()
    p1 = by_id(patches, "connect-timeout-180s")
    inside_file = write(real, "chunk.js", f"inside {p1.find}")
    outside_file = write(outside, "chunk.js", f"outside {p1.find}")
    alias = b / "alias"
    link_dir(real, alias)                                  # contained at enumeration time
    outside_before = outside_file.read_bytes()
    real_preflight = engine._preflight

    def repoint_after_enumeration(build, texts, selected):
        real_preflight(build, texts, selected)
        unlink_dir(alias)
        link_dir(outside, alias)                           # now escapes the build dir

    monkeypatch.setattr(engine, "_preflight", repoint_after_enumeration)
    changed = apply(b, patches, ids=[p1.id], check=ok_check())

    assert outside_file.read_bytes() == outside_before     # file outside build untouched
    assert p1.replace in read(inside_file)
    assert changed == ["real/chunk.js"]


def test_apply_preserves_file_bytes_exactly(patches, tmp_path):
    """Only the anchor bytes change; no reformatting, no newline rewrite."""
    b = tmp_path / "build"
    b.mkdir()
    p1 = by_id(patches, "connect-timeout-180s")
    original = f"// header\r\nconst s = `{p1.find}`;\r\nconsole.log(1);\r\n".encode()
    (b / "x.js").write_bytes(original)
    apply(b, patches, ids=[p1.id], check=ok_check())
    assert (b / "x.js").read_bytes() == original.replace(p1.find.encode(), p1.replace.encode())


def test_apply_group_anchors_must_coexist_in_same_file(patches, tmp_path):
    """Group semantics demand the patches land together; split across files is a broken build."""
    b = tmp_path / "build"
    b.mkdir()
    p6, p7, p8, p9 = sse(patches)
    write(b, "a.js", f"A{p6.find}B{p8.find}C")   # half the group
    write(b, "b.js", f"D{p7.find}E{p9.find}F")   # other half
    before_a = read(b / "a.js")
    with pytest.raises(PatchError):
        apply(b, patches, ids=[p6.id], check=ok_check())
    assert read(b / "a.js") == before_a
    assert p6.replace not in read(b / "a.js") and p7.replace not in read(b / "b.js")


def test_apply_group_rejects_extra_file_with_proper_anchor_subset(patches, tmp_path, backup_root):
    """FINDING 1: 8895.js holds all four sse-hang anchors, another chunk holds only the
    generic p9 anchor. Rewriting it to `$G();try{` leaves a ReferenceError that
    `node --check` cannot see (it is only an identifier), so abort instead."""
    b = tmp_path / "build"
    b.mkdir()
    p6, p7, p8, p9 = sse(patches)
    full = write(b, "server/chunks/8895.js",
                 f"A{p6.find}B{p7.find}C{p8.find}D{p9.find}E")
    partial = write(b, "server/chunks/2001.js", f"X{p9.find}Y")
    before = full.read_bytes(), partial.read_bytes()

    with pytest.raises(PatchError) as exc:
        apply(b, patches, ids=["sse-hang"], check=ok_check())
    msg = str(exc.value)
    assert "sse-hang" in msg
    assert "server/chunks/2001.js" in msg          # names the offending file
    assert p9.id in msg                            # ... and the patch ids involved

    assert (full.read_bytes(), partial.read_bytes()) == before
    assert list(backup_root.iterdir()) == []       # nothing written, no snapshot


def test_apply_preserves_non_utf8_surrounding_bytes(patches, tmp_path):
    """A stray non-UTF8 byte in a file must not crash scan/apply nor be mangled on rewrite."""
    b = tmp_path / "build"
    b.mkdir()
    p1 = by_id(patches, "connect-timeout-180s")
    original = b"// raw:\xff\r\n" + p1.find.encode() + b"\r\n"
    (b / "x.js").write_bytes(original)
    apply(b, patches, ids=[p1.id], check=ok_check())
    assert (b / "x.js").read_bytes() == original.replace(p1.find.encode(), p1.replace.encode())


def test_apply_preserves_bytes_in_unpatched_file(patches, tmp_path):
    """Selection touches file A; unrelated non-UTF8 bytes in file B are byte-identical after."""
    b = tmp_path / "build"
    b.mkdir()
    p1 = by_id(patches, "connect-timeout-180s")
    write(b, "a.js", f"x {p1.find} y")
    blob = b"\x00\xff\xfe\x01"
    (b / "b.js").write_bytes(blob)
    apply(b, patches, ids=[p1.id], check=ok_check())
    assert (b / "b.js").read_bytes() == blob
    assert state_for(scan(b, [p1]), p1.id).state == "applied"  # scan survives file B too


def test_scan_files_sorted_stable(patches, tmp_path):
    b = tmp_path / "build"
    b.mkdir()
    p1 = by_id(patches, "connect-timeout-180s")
    write(b, "z.js", f"x {p1.find} y")
    write(b, "a.js", f"x {p1.find} y")
    s = state_for(scan(b, [p1]), p1.id)
    assert s.clean_files == ["a.js", "z.js"]


def test_apply_composes_patches_sharing_a_file(patches, tmp_path):
    b = tmp_path / "build"
    b.mkdir()
    p2 = by_id(patches, "ua-messages")
    p5 = by_id(patches, "tools-strip-custom")
    write(b, "318.js", f"A{p2.find}M{p5.find}Z")     # both anchors in one file
    changed = apply(b, patches, ids=[p2.id, p5.id], check=ok_check())
    assert changed == ["318.js"]
    t = read(b / "318.js")
    assert p2.replace in t
    assert p5.replace in t
    # order composition is stable: applying again is a no-op (replace-contains-find)
    assert apply(b, patches, ids=[p2.id, p5.id], check=ok_check()) == []


# ---------- revert ----------

def test_apply_then_revert_roundtrip_original_bytes(patches, tmp_path):
    b = tmp_path / "build"
    b.mkdir()
    p1 = by_id(patches, "connect-timeout-180s")
    original = f"const s = `{p1.find}`;\nconsole.log(1);\n"
    write(b, "x.js", original)
    apply(b, patches, ids=[p1.id], check=ok_check())
    assert p1.replace in read(b / "x.js")
    reverted = revert(b, patches, group=p1.group, check=ok_check())
    assert reverted == ["x.js"]
    assert read(b / "x.js") == original
    assert state_for(scan(b, [p1]), p1.id).state == "clean"


def test_revert_unknown_group_raises(patches, tmp_path):
    b = tmp_path / "build"
    b.mkdir()
    with pytest.raises(PatchError, match="unknown"):
        revert(b, patches, group="nope", check=ok_check())


def test_revert_dead_anchor_writes_nothing(patches, tmp_path, backup_root):
    b = tmp_path / "build"
    b.mkdir()
    p1 = by_id(patches, "connect-timeout-180s")
    write(b, "x.js", "no anchors at all")
    before = read(b / "x.js")
    with pytest.raises(PatchError):
        revert(b, patches, group=p1.group, check=ok_check())
    assert read(b / "x.js") == before
    assert list(backup_root.iterdir()) == []


def test_revert_one_group_in_shared_file_keeps_other(patches, tmp_path):
    b = tmp_path / "build"
    b.mkdir()
    p2 = by_id(patches, "ua-messages")
    p5 = by_id(patches, "tools-strip-custom")
    write(b, "318.js", f"A{p2.find}M{p5.find}Z")
    apply(b, patches, ids=[p2.id, p5.id], check=ok_check())
    assert p2.replace in read(b / "318.js") and p5.replace in read(b / "318.js")
    reverted = revert(b, patches, group=p5.group, check=ok_check())
    assert reverted == ["318.js"]
    t = read(b / "318.js")
    assert p5.find in t and p5.replace not in t   # p5 undone
    assert p2.replace in t                        # p2 intact
    assert state_for(scan(b, [p2]), p2.id).state == "applied"


def test_revert_sse_hang_group_all_and_idempotent(patches, tmp_path):
    b = tmp_path / "build"
    b.mkdir()
    ps = sse(patches)
    text = "".join(f"L{i}{p.find}L{i}!" for i, p in enumerate(ps))
    write(b, "8895.js", text)
    apply(b, patches, ids=["sse-hang"], check=ok_check())
    assert apply(b, patches, ids=["sse-hang"], check=ok_check()) == []  # idempotent apply
    patched = read(b / "8895.js")
    reverted = revert(b, patches, group="sse-hang", check=ok_check())
    assert reverted == ["8895.js"]
    t = read(b / "8895.js")
    for p in ps:
        assert p.find in t and p.replace not in t
        assert state_for(scan(b, ps), p.id).state == "clean"
    # second revert is a clean no-op
    assert revert(b, patches, group="sse-hang", check=ok_check()) == []
    assert read(b / "8895.js") == t
    assert patched != t


# ---------- backup + verify ----------

def test_backup_snapshot_matches_original_and_retention(patches, tmp_path, backup_root):
    b = tmp_path / "build"
    b.mkdir()
    p1 = by_id(patches, "connect-timeout-180s")
    for i in range(6):
        original = f"/* v{i} */ x {p1.find} y\n"
        write(b, "x.js", original)
        apply(b, patches, ids=[p1.id], check=ok_check())
        assert p1.replace in read(b / "x.js")
        # snapshot of this step's original exists and matches byte-for-byte
        snaps = sorted(backup_root.iterdir())
        assert (snaps[-1] / "x.js").read_text(encoding="utf-8") == original
    snaps = sorted(d.name for d in backup_root.iterdir() if d.is_dir())
    assert len(snaps) == 5  # retention 5: oldest pruned
    # newest snapshot holds the last original
    newest = sorted(backup_root.iterdir())[-1]
    assert newest.joinpath("x.js").read_text(encoding="utf-8") == \
        f"/* v5 */ x {p1.find} y\n"


def test_noop_apply_creates_no_snapshot(patches, tmp_path, backup_root):
    b = tmp_path / "build"
    b.mkdir()
    p1 = by_id(patches, "connect-timeout-180s")
    write(b, "x.js", f"x {p1.find} y")
    apply(b, patches, ids=[p1.id], check=ok_check())
    assert len(list(backup_root.iterdir())) == 1
    apply(b, patches, ids=[p1.id], check=ok_check())  # no-op
    assert len(list(backup_root.iterdir())) == 1      # nothing new backed up


def test_apply_rolls_back_all_files_when_check_fails(patches, tmp_path, backup_root):
    b = tmp_path / "build"
    b.mkdir()
    p1 = by_id(patches, "connect-timeout-180s")
    write(b, "a.js", f"a{p1.find}a")
    write(b, "b.js", f"b{p1.find}b")
    orig_a, orig_b = read(b / "a.js"), read(b / "b.js")

    def bad_check(path):
        if Path(path).name == "b.js":
            raise PatchError("syntax error in b.js")

    with pytest.raises(PatchError):
        apply(b, patches, ids=[p1.id], check=bad_check)
    # both files back to original: the one that passed check AND the one that failed
    assert read(b / "a.js") == orig_a
    assert read(b / "b.js") == orig_b
    assert p1.replace not in read(b / "a.js") and p1.replace not in read(b / "b.js")


def test_rollback_restores_every_file_and_names_unrestorable_ones(
        patches, tmp_path, backup_root, monkeypatch):
    """FINDING 2: one restore raising PermissionError must not abandon the remaining
    files. Restore all we can, then re-raise the ORIGINAL verification failure with the
    still-dirty files and their snapshot paths named."""
    b = tmp_path / "build"
    b.mkdir()
    p1 = by_id(patches, "connect-timeout-180s")
    for name in ("a.js", "b.js", "c.js"):
        write(b, name, f"{name}:{p1.find}")
    orig = {n: read(b / n) for n in ("a.js", "b.js", "c.js")}
    real_copy2 = shutil.copy2

    def copy2_fail_b_restore(src, dst, *a, **kw):
        # snapshot phase copies build -> backups; restore phase copies backups -> build
        if Path(dst).name == "b.js" and Path(dst).parent == b:
            raise PermissionError(13, "Access is denied", str(dst))
        return real_copy2(src, dst, *a, **kw)

    monkeypatch.setattr(engine.shutil, "copy2", copy2_fail_b_restore)

    def bad_check(path):
        raise PatchError("node --check failed for c.js: Unexpected token")

    with pytest.raises(PatchError) as exc:
        apply(b, patches, ids=[p1.id], check=bad_check)
    msg = str(exc.value)

    assert "node --check failed for c.js" in msg     # ORIGINAL error, not the restore error
    assert "b.js" in msg                             # un-restored file named
    snap = sorted(d for d in backup_root.iterdir() if d.is_dir())[-1]
    assert str(snap) in msg or snap.name in msg      # ... with where its snapshot is
    assert "a.js" not in msg and "PermissionError" in msg

    assert read(b / "a.js") == orig["a.js"]          # restored despite b.js failing
    assert read(b / "c.js") == orig["c.js"]          # restored even though it came after b
    assert p1.replace in read(b / "b.js")            # honestly still dirty
    assert (snap / "b.js").read_text(encoding="utf-8") == orig["b.js"]


def test_retention_prunes_only_engine_generated_snapshots(patches, tmp_path, backup_root):
    """FINDING 4: a human-kept backups/<name>/ must survive retention pruning."""
    b = tmp_path / "build"
    b.mkdir()
    p1 = by_id(patches, "connect-timeout-180s")
    keeper = backup_root / "20240101-manual"
    keeper.mkdir(parents=True)
    (keeper / "x.js").write_text("hand-saved original", encoding="utf-8")
    loose = backup_root / "notes.txt"
    loose.write_text("why I kept that", encoding="utf-8")

    for i in range(6):
        write(b, "x.js", f"/* v{i} */ {p1.find}")
        apply(b, patches, ids=[p1.id], check=ok_check())

    assert (keeper / "x.js").read_text(encoding="utf-8") == "hand-saved original"
    assert loose.exists()
    generated = [d.name for d in backup_root.iterdir() if d.is_dir() and d != keeper]
    assert len(generated) == 5


def test_backup_root_created_on_demand(patches, tmp_path, monkeypatch):
    """First ever apply must not fail because backups/ does not exist yet."""
    root = tmp_path / "fresh" / "backups"
    monkeypatch.setattr(engine, "BACKUP_ROOT", root)
    b = tmp_path / "build"
    b.mkdir()
    p1 = by_id(patches, "connect-timeout-180s")
    write(b, "x.js", f"x {p1.find} y")
    assert not root.exists()
    apply(b, patches, ids=[p1.id], check=ok_check())
    assert len([d for d in root.iterdir() if d.is_dir()]) == 1


def test_load_patches_default_path_is_project_toml(patches):
    assert engine.PATCHES_FILE == PATCHES_FILE
    assert [p.id for p in load_patches()] == [p.id for p in patches]


# ---------- real build (read-only, skipped if absent) ----------

def test_scan_real_build_matches_measurements(patches):
    """Read-only sanity against the live install. Never writes. Skips if not installed."""
    try:
        build = engine.build_dir()
    except PatchError as e:
        pytest.skip(f"npm/install unavailable: {e}")
    if not build.is_dir():
        pytest.skip(f"real build not present: {build}")
    try:
        import updater
        version = updater.current_version()
    except Exception:
        version = "unknown"
    if version != "0.5.65":
        pytest.skip(f"measurements below were taken on 0.5.65; install is {version} — "
                    "patch states legitimately differ after an update")
    states = {s.patch.id: s for s in scan(build, patches)}
    expected = {
        "connect-timeout-180s": 4,   # p1 lands in 4 files
        "ua-messages": 1,
        "ua-models-route": 1,
        "ua-models-agg": 1,
        "tools-strip-custom": 1,
        "sse-close-translate": 1,
        "sse-close-passthrough": 1,
        "gauge-guard": 1,
        "gauge-flush-route": 1,
    }
    for pid, n in expected.items():
        s = states[pid]
        assert s.state == "applied", f"{pid} is {s.state}"
        assert len(s.applied_files) == n, f"{pid}: {s.applied_files}"
    # p2 and p5 share one file; the sse-hang group shares one file
    assert states["ua-messages"].applied_files == states["tools-strip-custom"].applied_files
    sse_files = {tuple(states[p.id].applied_files) for p in sse(patches)}
    assert len(sse_files) == 1


# ---------- node --check (real binary, local only) ----------

def test_node_check_valid_and_invalid(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    good = tmp_path / "good.js"
    good.write_text("var x = 1;\nconsole.log(x);\n", encoding="utf-8")
    engine.node_check(good)  # must not raise
    bad = tmp_path / "bad.js"
    bad.write_text("var = ;\n", encoding="utf-8")
    with pytest.raises(PatchError):
        engine.node_check(bad)


# ---------- install/build path derivation ----------

def test_build_dir_derived_from_install(monkeypatch):
    monkeypatch.setattr(engine, "install_dir",
                        lambda: Path("E:/pkgs/node_modules/9router"))
    assert engine.build_dir() == Path("E:/pkgs/node_modules/9router/app/.next-cli-build")


# ---------- ordering + anchor-safety invariants (hazard round 2) ----------

def test_apply_sse_hang_composes_definer_first(patches, tmp_path, monkeypatch):
    """HAZARD 1 (apply side): p8 defines $G, p6/p7/p9 call it. The engine must compose
    p8 FIRST so an aborted mid-sequence state still has the definition before any call.
    Final bytes are order-independent, so we record the real .replace() call order."""
    b = tmp_path / "build"
    b.mkdir()
    p6, p7, p8, p9 = sse(patches)
    write(b, "8895.js", f"A{p6.find}B{p7.find}C{p8.find}D{p9.find}E")
    log = []
    record_replaces(monkeypatch, log)
    apply(b, patches, ids=["sse-hang"], check=ok_check())
    order = touched_ids(log, sse(patches))
    assert order.index("gauge-guard") == 0, f"p8 not applied first: {order}"
    assert sorted(order) == sorted(p.id for p in sse(patches))


def test_revert_sse_hang_composes_definer_last(patches, tmp_path, monkeypatch):
    """HAZARD 1 (revert side): undo order must remove $G's definition (p8) only after
    every $G() call is gone — p8 LAST. `reversed(order)` today gives p9,p8,p7,p6: p8 second."""
    b = tmp_path / "build"
    b.mkdir()
    ps = sse(patches)
    write(b, "8895.js", f"A{ps[0].find}B{ps[1].find}C{ps[2].find}D{ps[3].find}E")
    apply(b, patches, ids=["sse-hang"], check=ok_check())
    log = []
    record_replaces(monkeypatch, log)
    revert(b, patches, group="sse-hang", check=ok_check())
    order = touched_ids(log, ps)
    assert order.index("gauge-guard") == len(order) - 1, f"p8 not reverted last: {order}"
    assert sorted(order) == sorted(p.id for p in ps)


def test_sse_hang_roundtrip_bytes_exact(patches, tmp_path):
    """Keep: final bytes after apply -> revert are exactly the original (order-independent
    baseline this new ordering must not break)."""
    b = tmp_path / "build"
    b.mkdir()
    ps = sse(patches)
    original = "".join(f"pre{i};{p.find};post{i}\n" for i, p in enumerate(ps))
    write(b, "8895.js", original)
    apply(b, patches, ids=["sse-hang"], check=ok_check())
    patched = read(b / "8895.js")
    assert patched != original
    for p in ps:
        assert p.replace in patched and p.find not in patched
    revert(b, patches, group="sse-hang", check=ok_check())
    assert read(b / "8895.js") == original


# ---------- no find is a substring of another patch's replace ----------

def test_no_find_inside_any_other_replace(patches):
    """HAZARD 2: if a patch's `find` occurs inside another patch's `replace`, the later
    patch rewrites the earlier one's replacement body — e.g. a shortened p9 anchor would
    turn p8's $G into infinite recursion. The data invariant catches any such pair."""
    for a in patches:
        for b in patches:
            if a.id != b.id and a.find in b.replace:
                pytest.fail(f"{a.id} find is substring of {b.id} replace "
                            f"(same group={a.group == b.group})")


def test_hazard2_teeth_shortened_p9_anchor_fires(patches):
    """Prove the invariant test has teeth: shrink p9's anchor to the bare call (drop the
    `;try{` safety margin, in memory only) and the assertion must fire."""
    p9 = by_id(patches, "gauge-flush-route")
    assert ";try{" in p9.find  # the margin that keeps p8's replace body out of reach
    bare = p9.find[: p9.find.index(";try{")]
    doctored = [p if p.id != "gauge-flush-route"
                else engine.Patch(p.id, p.order, p.group, p.summary, p.why, bare, p.replace)
                for p in patches]
    for a in doctored:
        for b in doctored:
            if a.id != b.id and a.find in b.replace:
                assert a.id == "gauge-flush-route" and b.id == "gauge-guard"
                return  # fired on the exact pair
    pytest.fail("shortened p9 anchor did not trip the substring invariant")


# ---------- nonstream-sse-retry ----------

def test_nonstream_retry_group_apply_revert(patches, tmp_path):
    """Group mới áp/gỡ atomic trên cùng một file giả lập 8895.js chứa cả 2 anchor."""
    b = tmp_path / "build"
    b.mkdir()
    p10 = by_id(patches, "nonstream-retry-exec")
    p11 = by_id(patches, "nonstream-retry-aggregate")
    write(b, "8895.js", "A" + p10.find + "B" + p11.find + "C")
    changed = apply(b, patches, ids=["nonstream-sse-retry"], check=ok_check())
    assert changed == ["8895.js"]
    t = read(b / "8895.js")
    assert p10.find not in t and p10.replace in t
    assert p11.find not in t and p11.replace in t
    assert state_for(scan(b, patches), p10.id).state == "applied"
    assert state_for(scan(b, patches), p11.id).state == "applied"
    revert(b, patches, group="nonstream-sse-retry", check=ok_check())
    t2 = read(b / "8895.js")
    assert p10.find in t2 and p11.find in t2 and p10.replace not in t2


def test_nonstream_retry_anchors_exactly_one_state_in_real_build(patches):
    """Trên build thật: mỗi anchor phải ở đúng một trạng thái (clean hoặc applied), không
    dead-anchor, không nửa vời. Skip khi máy không có build / anchor khác version."""
    try:
        build = engine.build_dir()
    except Exception:
        pytest.skip("no real build on this machine")
    f = build / "server" / "chunks" / "8895.js"
    if not f.is_file():
        pytest.skip("8895.js not in this build")
    t = read(f)
    for pid in ("nonstream-retry-exec", "nonstream-retry-aggregate"):
        p = by_id(patches, pid)
        if p.find not in t and p.replace not in t:
            pytest.skip(f"{pid} anchor absent — minifier remap differs on this version")
        assert (p.find in t) != (p.replace in t), pid


def test_nonstream_retry_075_anchor_tracks_remapped_handler_bindings(patches):
    """0.5.75 added handler args, shifting trackDone/appendLog and local names.
    Retry must call the new appendLog binding, not stale F (now trackDone)."""
    p11 = by_id(patches, "nonstream-retry-aggregate")
    assert p11.find == ('log:J}){let K;if(F(),(a.headers.get("content-type")||"")'
                        '.includes("text/event-stream")){let b=await a.text(),d=(0,k.F)(b,c);'
                        'if(!d)return G({status:')
    assert p11.replace.startswith('log:J,retry:$x}){let K;if(F(),')
    assert p11.replace.endswith('if($m)d=$m}}if(!d)return G({status:')


def test_nonstream_retry_aggregator_builds_claude_message(patches):
    """Unit-test aggregator inject ở patch 11 bằng node: SSE anthropic hoàn chỉnh → message
    claude đúng shape; thiếu message_stop / frame error / rác → null (rơi về 502 cũ)."""
    p11 = by_id(patches, "nonstream-retry-aggregate")
    marker = "await (async($rp,$mo)=>{"
    i = p11.replace.index(marker)
    tail = "})($r,c)"
    j = p11.replace.index(tail, i)
    expr = p11.replace[i + len("await "):j + len(tail)]     # (async($rp,$mo)=>{...})($r,c)
    fn_src = expr[1:expr.rindex("})($r,c)") + 1]            # async($rp,$mo)=>{...}
    sse = "\n".join([
        'data: {"type":"message_start","message":{"id":"msg_1","model":"glm-5.3-flash",'
        '"usage":{"input_tokens":10,"output_tokens":1}}}',
        "",
        'data: {"type":"content_block_start","index":0,'
        '"content_block":{"type":"tool_use","id":"toolu_9","name":"Bash"}}',
        "",
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"input_json_delta","partial_json":"{\\"command\\":\\"ls\\"}"}}',
        "",
        'data: {"type":"content_block_start","index":1,"content_block":{"type":"text"}}',
        "",
        'data: {"type":"content_block_delta","index":1,'
        '"delta":{"type":"text_delta","text":"Xin chào"}}',
        "",
        'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":7}}',
        "",
        'data: {"type":"message_stop"}',
        "",
    ])
    no_stop = ('data: {"type":"message_start","message":{"id":"x"}}\n\n'
               'data: {"type":"content_block_delta","index":0,'
               '"delta":{"type":"text_delta","text":"hi"}}\n\n')
    err = 'data: {"type":"error","error":{"message":"boom"}}\n\n'
    junk = "data: oops\n\nevent: ping\n\n"
    script = """
const fn = %s;
(async () => {
  const ok = await fn({ body: null, text: async () => %s }, "glm-5.3-flash");
  if (!ok || ok.type !== "message" || ok.role !== "assistant") throw new Error("shape: " + JSON.stringify(ok));
  if (ok.id !== "msg_1" || ok.model !== "glm-5.3-flash") throw new Error("id/model: " + JSON.stringify(ok));
  if (ok.content[0].type !== "tool_use" || ok.content[0].input.command !== "ls")
    throw new Error("tool_use: " + JSON.stringify(ok.content[0]));
  if (ok.content[1].text !== "Xin chào") throw new Error("text: " + JSON.stringify(ok.content[1]));
  if (ok.stop_reason !== "tool_use" || ok.usage.input_tokens !== 10 || ok.usage.output_tokens !== 7)
    throw new Error("stop/usage: " + JSON.stringify(ok));
  const noStopR = await fn({ body: null, text: async () => %s }, "m");
  if (noStopR !== null) throw new Error("thiếu message_stop phải null, got " + JSON.stringify(noStopR));
  const err = await fn({ body: null, text: async () => %s }, "m");
  if (err !== null) throw new Error("frame error phải null");
  const junk = await fn({ body: null, text: async () => %s }, "m");
  if (junk !== null) throw new Error("rác phải null");
  console.log("AGGREGATOR-OK");
})().catch(e => { console.error("FAIL:", e.message); process.exit(1); });
""" % (fn_src, json.dumps(sse), json.dumps(no_stop), json.dumps(err), json.dumps(junk))
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"node failed: {r.stderr or r.stdout}"


# ---------- non-SSE failure metadata ----------

def test_non_sse_failure_carries_classifier_metadata(patches):
    """20623's non-SSE branch must expose fields used by 8635's account loop. Without
    them `vk(connectionId, q.status, q.error, ...)` receives undefined/undefined, misses
    p13's ``upstream non-sse`` branch, and locks every account 30s. The sibling p16 has
    the expected return shape; assert P18 preserves it for a 200 text/html response."""
    p18 = by_id(patches, "non-sse-failure-metadata")
    head = 'handleError?.(Error(`upstream non-SSE: ${g}`)),'
    assert p18.replace.startswith(head)
    literal = p18.replace[len(head):]          # the {success:!1,status,error,response} object
    script = """
const make = (g, f) => (%s);
for (const [status, body] of [[200, "Upstream returned non-SSE response (text/plain)"], [502, "bad gateway"]]) {
  const q = make(status, body);
  if (q.success !== false || q.status !== status) throw new Error("status: " + JSON.stringify(q));
  if (q.error !== "upstream non-SSE: " + status) throw new Error("error: " + JSON.stringify(q));
  if (q.response.status !== status) throw new Error("response status: " + q.response.status);
}
console.log("NON-SSE-METADATA-OK");
""" % literal
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "NON-SSE-METADATA-OK" in r.stdout, r.stderr or r.stdout


# ---------- reasoning-effort body cap (p14) ----------

def test_reasoning_effort_cap_mutates_the_body_that_is_sent(patches):
    """HAZARD (measured 2026-09-08): the cap IIFE must rewrite `ah` — the object handed to
    `execute({body:ah})`. `aT` is the headroom RESPONSE ({tokens_before,...}), so capping it
    is a silent no-op: THINK:max still goes out on >448KB bodies (the am empty-stream case).
    The IIFE must also size-check `ah`, not `aT`."""
    p14 = by_id(patches, "reasoning-effort-body-cap")
    iife = p14.replace[p14.replace.index("(function(){"):]
    assert "aT" not in iife, "cap still reads/writes aT (headroom response), not ah (sent body)"
    script = """
const d={line:()=>{}}; const as="t";
let aT={tokens_before:1,tokens_after:1};
let ah={reasoning_effort:"max",messages:[{role:"user",content:"x".repeat(460000)}]};
%s
if (ah.reasoning_effort !== "high") throw new Error("body not capped: " + ah.reasoning_effort);
ah={reasoning:{effort:"xhigh"},messages:[{role:"user",content:"y".repeat(460000)}]};
%s
if (ah.reasoning.effort !== "high") throw new Error("reasoning.effort not capped");
ah={reasoning_effort:"max",messages:[{role:"user",content:"small"}]};
%s
if (ah.reasoning_effort !== "max") throw new Error("small body must not cap");
console.log("P14-BODY-CAP-OK");
""" % (iife, iife, iife)
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "P14-BODY-CAP-OK" in r.stdout, r.stderr or r.stdout


# ---------- claude tool-result canonicalizer ----------

def test_claude_tool_results_merged_into_next_user_message(patches):
    """Anthropic requires every parallel tool_result in one immediately-following user
    message. The client can split one result per consecutive user message; merge those
    messages without dropping any tool_use/result or text."""
    p19 = by_id(patches, "claude-tool-result-canonicalize")
    marker = "a.messages=(()=>{"
    start = p19.replace.index(marker) + len("a.messages=")
    end = p19.replace.index("(),a}function t(a)", start) + 2
    fn = p19.replace[start:end]
    script = """
const canon = (messages) => { const a = {messages}; a.messages = %s; return a.messages; };
const input = [
  {role:"user",content:[{type:"text",text:"q"}]},
  {role:"assistant",content:[{type:"text",text:"working"},{type:"tool_use",id:"c1"},{type:"tool_use",id:"c2"},{type:"tool_use",id:"c3"}]},
  {role:"user",content:[{type:"tool_result",tool_use_id:"c3"}]},
  {role:"user",content:[{type:"tool_result",tool_use_id:"c2"}]},
  {role:"user",content:[{type:"tool_result",tool_use_id:"c1"},{type:"text",text:"tail"}]},
  {role:"assistant",content:[{type:"text",text:"done"}]}
];
const out = canon(JSON.parse(JSON.stringify(input)));
if (out.length !== 4) throw new Error("length: " + JSON.stringify(out));
if (out[1].content.filter(x => x.type === "tool_use").length !== 3) throw new Error("tool_use lost");
const results = out[2].content.filter(x => x.type === "tool_result").map(x => x.tool_use_id).sort();
if (results.join(",") !== "c1,c2,c3") throw new Error("results: " + JSON.stringify(out[2]));
if (!out[2].content.some(x => x.type === "text" && x.text === "tail")) throw new Error("text lost");
const valid = [{role:"assistant",content:[{type:"tool_use",id:"v1"},{type:"tool_use",id:"v2"}]},{role:"user",content:[{type:"tool_result",tool_use_id:"v1"},{type:"tool_result",tool_use_id:"v2"}]}];
if (JSON.stringify(canon(JSON.parse(JSON.stringify(valid)))) !== JSON.stringify(valid)) throw new Error("valid changed");
const unrelated = [{role:"user",content:[{type:"text",text:"a"}]},{role:"user",content:[{type:"text",text:"b"}]}];
if (JSON.stringify(canon(JSON.parse(JSON.stringify(unrelated)))) !== JSON.stringify(unrelated)) throw new Error("unrelated merged");
console.log("CLAUDE-TOOL-CANON-OK");
""" % fn
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "CLAUDE-TOOL-CANON-OK" in r.stdout, r.stderr or r.stdout


# ---------- claude-system-hoist ----------

def test_claude_system_hoist_apply_revert(patches, tmp_path):
    b = tmp_path / "build"
    b.mkdir()
    p12 = by_id(patches, "claude-system-hoist")
    write(b, "318.js", "A" + p12.find + "B")
    changed = apply(b, patches, ids=["claude-system-hoist"], check=ok_check())
    assert changed == ["318.js"]
    t = read(b / "318.js")
    assert p12.find not in t and p12.replace in t
    assert state_for(scan(b, patches), p12.id).state == "applied"
    revert(b, patches, group="claude-system-hoist", check=ok_check())
    t2 = read(b / "318.js")
    assert p12.find in t2 and p12.replace not in t2


def test_claude_system_hoist_real_build_exactly_one_state(patches):
    try:
        build = engine.build_dir()
    except Exception:
        pytest.skip("no real build on this machine")
    f = build / "server" / "chunks" / "318.js"
    if not f.is_file():
        pytest.skip("318.js not in this build")
    p12 = by_id(patches, "claude-system-hoist")
    t = read(f)
    assert (p12.find in t) != (p12.replace in t)


def test_claude_system_hoist_transform(patches):
    """Node unit: hoist system-in-messages lên top-level system; giữ adjacency; body không có
    system-in-messages và body openai (system tại 0) phải nguyên vẹn."""
    p12 = by_id(patches, "claude-system-hoist")
    head = "j=e.find(a=>a.match(b));"
    stmt = p12.replace[len(head):p12.replace.index("var k=function(")]
    script = """
{
  const c = %s;
  %s
  if (c.messages.some(m => m && m.role === "system")) throw new Error("van con system trong messages");
  if (c.messages.length !== 3) throw new Error("messages length: " + c.messages.length);
  if (c.messages[1].role !== "assistant" || c.messages[1].content[0].type !== "tool_use") throw new Error("assistant lost: " + JSON.stringify(c.messages[1]));
  if (c.messages[2].role !== "user" || c.messages[2].content[0].type !== "tool_result" || c.messages[2].content[0].tool_use_id !== "t1") throw new Error("adjacency broken: " + JSON.stringify(c.messages[2]));
  if (!Array.isArray(c.system) || c.system.length !== 2) throw new Error("system: " + JSON.stringify(c.system));
  if (c.system[0].text !== "BASE" || c.system[1].text !== "PONYTAIL" || c.system[1].type !== "text") throw new Error("system content: " + JSON.stringify(c.system));
}
{
  const c = %s;
  const before2 = JSON.stringify(c);
  %s
  if (JSON.stringify(c) !== before2) throw new Error("body khong co system-in-messages bi dot bien");
}
{
  const c = %s;
  const before3 = JSON.stringify(c);
  %s
  if (JSON.stringify(c) !== before3) throw new Error("openai body (system tai 0) bi dot bien");
}
console.log("HOIST-OK");
""" % (
        json.dumps({
            "model": "deepseek-v4-flash",
            "system": "BASE",
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "hi"}, {"type": "image", "source": {}}]},
                {"role": "system", "content": [{"type": "text", "text": "PONYTAIL"}, {"type": "image", "source": {}}]},
                {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Grep", "input": {}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x"}]},
            ],
        }),
        stmt,
        json.dumps({
            "system": "BASE",
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Grep", "input": {}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1"}]},
            ],
        }),
        stmt,
        json.dumps({
            "messages": [
                {"role": "system", "content": "you are helpful"},
                {"role": "user", "content": "hello"},
            ],
        }),
        stmt,
    )
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"node failed: {r.stderr or r.stdout}"


# ---------- empty-stream-fallback (p16) ----------

def test_empty_stream_fallback_real_build_exactly_one_state(patches):
    if not any(x.id == "empty-stream-fallback" for x in patches):
        pytest.skip("p16 suspended")
    try:
        build = engine.build_dir()
    except Exception:
        pytest.skip("no real build on this machine")
    f = build / "server" / "chunks" / "8895.js"
    if not f.is_file():
        pytest.skip("8895.js not in this build")
    t = read(f)
    p16 = by_id(patches, "empty-stream-fallback")
    assert (p16.find in t) != (p16.replace in t)


def test_errbody_read_timeout_real_build_states(patches):
    """Module 43659 inline ở 10 file: mỗi file phải ở đúng một trạng thái, không dead-anchor."""
    try:
        build = engine.build_dir()
    except Exception:
        pytest.skip("no real build on this machine")
    p17 = by_id(patches, "errbody-read-timeout")
    hits = 0
    for f in engine._js_files(build):
        t = read(f)
        if p17.find in t or p17.replace in t:
            assert (p17.find in t) != (p17.replace in t), f
            hits += 1
    assert hits > 0


def test_empty_stream_peek_live_or_502(patches):
    """Node unit: stream rỗng (kể cả thinking-only) → {success:!1,status:502}; có evidence
    (anthropic text_delta tách giữa frame / openai delta.content) → stream live nguyên vẹn
    kèm headers; read() throw phải propagate."""
    if not any(x.id == "empty-stream-fallback" for x in patches):
        pytest.skip("p16 suspended")
    p16 = by_id(patches, "empty-stream-fallback")
    head = "let $pk="
    tail = ";return await $pk(Q,n.RK)"
    fn = p16.replace[p16.replace.index(head) + len(head):p16.replace.index(tail)]
    script = """
const $pk = %s;
const enc = new TextEncoder();
const mk = (frames) => { let i = 0; return { read: async () => i < frames.length ? { done: false, value: enc.encode(frames[i++]) } : { done: true, value: void 0 }, cancel: async () => {} }; };
(async () => {
  let r = await $pk({ getReader: () => mk(['data: {"type":"message_start"}\\n\\n', 'data: {"type":"message_stop"}\\n\\n']) }, {"ct":"sse"});
  if (r.success !== false || r.status !== 502) throw new Error("empty: " + JSON.stringify(r).slice(0, 140));

  r = await $pk({ getReader: () => mk(['data: {"type":"message_start"}\\n\\n', 'data: {"type":"content_block_delta","del', 'ta":{"type":"text_delta","text":"Xin"}}\\n\\ndata: {"type":"message_stop"}\\n\\n']) }, {"ct":"sse"});
  if (!r.success) throw new Error("anthropic text bi coi la empty");
  const txt = await r.response.text();
  if (!txt.includes("text_delta") || !txt.includes("message_stop")) throw new Error("pipe mat data: " + txt);
  if (r.response.headers.get("ct") !== "sse") throw new Error("headers lost");

  r = await $pk({ getReader: () => mk(['data: {"choices":[{"delta":{"content":"Hi"}}]}\\n\\n', "data: [DONE]\\n\\n"]) }, {});
  if (!r.success) throw new Error("openai content bi coi la empty");

  r = await $pk({ getReader: () => mk(['data: {"type":"content_block_delta","delta":{"type":"thinking_delta","thinking":"hmm"}}\\n\\n']) }, {});
  if (r.success !== false) throw new Error("thinking-only phai la empty");

  let threw = false;
  try { await $pk({ getReader: () => ({ read: async () => { throw new Error("boom"); } }) }, {}); } catch (e) { threw = true; }
  if (!threw) throw new Error("read-throw phai propagate");
  console.log("PEEK-OK");
})().catch(e => { console.error(e.message); process.exit(1); });
""" % fn
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "PEEK-OK" in r.stdout, f"node failed: {r.stderr or r.stdout}"


def test_errbody_timeout_races_body_read(patches):
    """Node unit: body đọc nhanh → nguyên văn; body treo → hết 9R_ERRBODY_TIMEOUT_MS thì rỗng
    + body bị cancel; không được có unhandled rejection."""
    p17 = by_id(patches, "errbody-read-timeout")
    inner = p17.replace[p17.replace.index("try{") + 4:p17.replace.index('}catch{c=""}')]
    script = """
process.on("unhandledRejection", (e) => { console.error("UNHANDLED: " + e.message); process.exit(1); });
process.env["9R_ERRBODY_TIMEOUT_MS"] = "60";
const inner = %s;
const f = new Function("a", 'return (async()=>{let c="";try{' + inner + '}catch{c=""}return c;})()');
(async () => {
  let cancelled = false;
  const fast = await f({ status: 503, text: async () => "astral", body: { cancel() { cancelled = true; } } });
  if (fast !== "astral") throw new Error("fast body: " + JSON.stringify(fast));
  if (cancelled) throw new Error("fast body khong duoc cancel");
  const t0 = Date.now();
  const slow = await f({ status: 503, text: () => new Promise(() => {}), body: { cancel() { cancelled = true; } } });
  const dt = Date.now() - t0;
  if (slow !== "") throw new Error("slow body: " + JSON.stringify(slow));
  if (!cancelled) throw new Error("slow body phai duoc cancel");
  if (dt > 500) throw new Error("timeout chua nhay: " + dt + "ms");
  await new Promise(r => setTimeout(r, 250));
  console.log("RACE-OK");
})().catch(e => { console.error(e.message); process.exit(1); });
""" % json.dumps(inner)
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "RACE-OK" in r.stdout, f"node failed: {r.stderr or r.stdout}"


# ---------- p15: attempt-total-deadline ----------
# LỊCH SỬ 2026-09-19: replace chèn `Date.now()-x` nhưng closure g() khai báo `i,j,k,l,m,n,o,p`
# — không có `x`. 0.5.81 re-minify đổi tên biến (find đã theo, replace thì không), nên
# `node --check` vẫn pass (chỉ là identifier) còn timer nổ runtime ReferenceError. Không có
# `uncaughtException` handler nào trong build → process chết, đúng log
# `⨯ uncaughtException: ReferenceError: x is not defined at Timeout._onTimeout`.

def test_attempt_deadline_fires_and_reports_elapsed(patches):
    """Timer $td chạy được trong closure g() (không ReferenceError) và báo đúng số ms.
    `node --check` không bắt được lớp lỗi này: identifier sai vẫn hợp lệ cú pháp."""
    p15 = by_id(patches, "attempt-total-deadline")
    injected = p15.replace
    script = """
process.env["9R_ATTEMPT_DEADLINE_MS"] = "20";
let fired = 0, seen = "";
function g(){
  const h=5000;
  let i=null,j=0,k=0,l=Date.now(),m="upstream connection lost",n=Date.now(),o="STREAM",p=()=>{i&&(clearTimeout(i),i=null)};
  const c={handleError:()=>{},abort:()=>{}};
  const e={s:(tag,msg)=>{ fired++; seen = msg; }};
  let %s r=0;
  q();  // upstream gọi q() ngay sau khi dựng r, đây là lúc arm stall timer -> i != null
  return { $tot, $td };
}
const r = g();
setTimeout(() => {
  if (fired !== 1) { console.error("timer chay " + fired + " lan"); process.exit(1); }
  if (!/total=\\d+ms/.test(seen)) { console.error("msg thieu so ms: " + seen); process.exit(1); }
  console.log("DEADLINE-OK");
}, 90);
""" % injected
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "DEADLINE-OK" in r.stdout, f"node failed: {r.stderr or r.stdout}"


def test_attempt_deadline_is_inert_after_stream_ends(patches):
    """$td không được clearTimeout ở đâu cả, nên timer của request đã xong vẫn nổ sau đó 240s.
    Guard `!i` phải làm nó thành no-op: nếu không, handleError()/abort() bắn lên attempt đã
    đóng → combo fallback + log lỗi ma cho một request thành công."""
    p15 = by_id(patches, "attempt-total-deadline")
    injected = p15.replace
    script = """
process.env["9R_ATTEMPT_DEADLINE_MS"] = "20";
let fired = 0, killed = 0;
function g(){
  const h=5000;
  let i=null,j=0,k=0,l=Date.now(),m="upstream connection lost",n=Date.now(),o="STREAM",p=()=>{i&&(clearTimeout(i),i=null)};
  const c={handleError:()=>{killed++},abort:()=>{killed++}};
  const e={s:(tag,msg)=>{ fired++; }};
  let %s r=0;
  q();  // stream bắt đầu -> i != null
  p();  // terminal (complete/error/disconnect/abort) -> i = null
  return { $tot, $td };
}
const r = g();
setTimeout(() => {
  if (fired !== 0) { console.error("timer van bao " + fired + " lan sau terminal"); process.exit(1); }
  if (killed !== 0) { console.error("da goi handleError/abort " + killed + " lan len attempt da dong"); process.exit(1); }
  console.log("INERT-OK");
}, 90);
""" % injected
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "INERT-OK" in r.stdout, f"node failed: {r.stderr or r.stdout}"


# ---------- log-render-improve group (p21-p24, cosmetic console format) ----------

def _patch_replace(patches, pid):
    return by_id(patches, pid).replace


def test_log_done_format_split_new_cache_ctx(patches):
    """P21 function k: no-cache shows [NEW x]; cache shows [NEW x] · CACHE ↻y · CTX x+y; z không phải
    replace — module 86171 giữ nguyên CTX hợp lệ khi có cache_creation."""
    rep = _patch_replace(patches, "log-done-format")
    inner = rep[rep.index("{usage:a,latency:b}){"):]          # rest of function body
    body = "return`" + "X"  # noqa: F841 — not used, we exec the actual function
    # extract the actual function declaration to test as JS
    script = """
%s
const enc = (v) => v;
function fmt(usage, latency) { return k({ usage, latency }); }
// no cache
let a = fmt({input_tokens:45789, output_tokens:91, cache_read_input_tokens:0}, {ttft:5182, total:6269});
if (!a.includes("[NEW 45789]")) throw new Error("no-cache NEW: " + a);
if (!a.includes("OUT 91")) throw new Error("no-cache OUT: " + a);
if (a.includes("CTX")) throw new Error("no-cache CTX should not appear: " + a);
// cache read
a = fmt({input_tokens:45789, output_tokens:91, cache_read_input_tokens:141952}, {ttft:5182, total:6269});
if (!a.includes("[NEW 45789]")) throw new Error("NEW: " + a);
if (!a.includes("CACHE ↻141952")) throw new Error("CACHE: " + a);
if (!a.includes("CTX 187741")) throw new Error("CTX: " + a);
console.log("DONE-FORMAT-OK");
""" % rep
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "DONE-FORMAT-OK" in r.stdout, r.stderr or r.stdout


def test_log_headroom_trim_compact_suffix(patches):
    """P22: m() token numbers become k/M suffix; n() hides body section when effective<10%, keeps
    it when >=10%. Parse the two function bodies from replace and eval in node."""
    rep = _patch_replace(patches, "log-headroom-trim")
    script = """
%s
const cases = [
  [{tokens_before:160097, tokens_after:124365, tokens_saved:35732}, "160.1k→124.4k"],
  [{tokens_before:8000, tokens_after:6000, tokens_saved:2000}, "8.0k→6.0k"],
  [{tokens_before:800, tokens_after:600, tokens_saved:200}, "800→600"],
];
for (const [c, want] of cases) {
  const s = m(c);
  if (!s.includes(want)) throw new Error("m(" + JSON.stringify(c) + ")=" + s + " want " + want);
}
let lo = n({ before: { bodyBytes: 100000, messageBytes: 50000, toolSchemaBytes: 100, toolHistoryBytes: 30000 }, after: { bodyBytes: 95000, messageBytes: 48000, toolSchemaBytes: 100, toolHistoryBytes: 28000 } });
if (!lo.startsWith("effective=") || lo.includes("body")) throw new Error("low-eff: " + lo);
let hi = n({ before: { bodyBytes: 1000000, messageBytes: 600000, toolSchemaBytes: 100, toolHistoryBytes: 300000 }, after: { bodyBytes: 700000, messageBytes: 400000, toolSchemaBytes: 100, toolHistoryBytes: 200000 } });
if (!hi.includes("body") || !hi.includes("MB")) throw new Error("high-eff: " + hi);
console.log("HEADROOM-TRIM-OK");
""" % rep
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "HEADROOM-TRIM-OK" in r.stdout, r.stderr or r.stdout


def test_log_post_trim_drops_uuid_caps_length(patches, tmp_path):
    """P23: POST line no longer embeds the `${ao}/${ap}` provider-UUID target, adds a 100-char
    cap and a 4-char ACC short label; the rebuilt statement must still parse as JS."""
    rep = _patch_replace(patches, "log-post-trim")
    assert "→ ${ao}/${ap}" not in rep          # provider-uuid target dropped
    assert "slice(0,100)" in rep                # length cap present
    assert "slice(0,4)" in rep                  # ACC short label
    # wrap the emitted statement so node can syntax-check it in isolation
    stmt = rep[rep.index("let aO=aN?at:aA;if(d?.line){"):]
    script = "function st(aN,at,aA,ah,a,d,ao,ap,c,O,g,t,aF,as){" + stmt + "}\n"
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    tmp = tmp_path / "stmt.js"
    tmp.write_text(script, encoding="utf-8")
    r = subprocess.run(["node", "--check", str(tmp)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr or r.stdout


def test_log_combo_drop_succeeded(patches):
    """P24: after replace, `Model ${e} succeeded` string no longer emitted on success; replace
    body is just `return b` so a caller returning the raw b (no log) is the new behavior."""
    rep = _patch_replace(patches, "log-combo-drop-succeeded")
    assert "succeeded" not in rep
    assert rep == "if(b.ok)return b"
    assert 'g.info("COMBO"' not in rep


# ---------- p25/p26: mcp spawn win fix (2026-09-09 incidents) ----------
# p27 max-tokens-floor is standalone (own group), anchored on `let aY=(0,s.SB)(ao);` in
# 8895.js — no other patch's find or replace contains that string, so the overlap invariant
# in test_no_find_inside_any_other_replace holds without folding it into p5.

def test_mcp_spawn_win_npx_command_is_platform_aware(patches):
    """P25: browsermcp command runs node with npx-cli.js — no bare `command:"npx"` left
    in the replacement, avoiding Windows ENOENT and .cmd EINVAL."""
    p = by_id(patches, "mcp-spawn-win-npx")
    assert p.find == 'command:"npx",args:["-y","@browsermcp/mcp@latest"]'
    assert 'process.execPath' in p.replace
    assert 'npx-cli.js' in p.replace
    assert p.replace.startswith("command:")


def test_mcp_spawn_error_guard_binds_error_listener(patches):
    """P26: getOrSpawn replacement wraps spawn in try/catch and attaches an f.on("error") handler,
    logs errors instead of crashing worker."""
    p = by_id(patches, "mcp-spawn-error-guard")
    assert 'try{f=d(e.command,e.args' in p.replace
    assert 'f.on("error",$x=>' in p.replace
    assert "console.error" in p.replace          # visible in router log, not silent
    assert 'return c={proc:f,sessions:new Map,buffer:""},b.set(a,c),' in p.replace


def test_mcp_spawn_pair_applies_together(patches, tmp_path):
    """p25+p26 hit the same files; whole-file apply must rewrite both in one pass and reverting
    BOTH restores stock — they are standalone patches, each reverts its own bytes."""
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    b = tmp_path / "build"
    stock = (
        'command:"npx",args:["-y","@browsermcp/mcp@latest"];'
        'let f=d(e.command,e.args,{stdio:["pipe","pipe","pipe"],env:process.env});'
        'return c={proc:f,sessions:new Map,buffer:""},b.set(a,c),c'
    )
    write(b, "chunks/9.js", stock)
    assert state_for(scan(b, patches), "mcp-spawn-win-npx").state == "clean"
    assert state_for(scan(b, patches), "mcp-spawn-error-guard").state == "clean"
    apply(b, patches, ["mcp-spawn-win-npx", "mcp-spawn-error-guard"], check=ok_check())
    t = read(b / "chunks/9.js")
    assert 'process.execPath' in t
    assert 'f.on("error",$x=>' in t
    assert state_for(scan(b, patches), "mcp-spawn-win-npx").state == "applied"
    assert state_for(scan(b, patches), "mcp-spawn-error-guard").state == "applied"
    revert(b, patches, "mcp-spawn-win-npx", check=ok_check())
    revert(b, patches, "mcp-spawn-error-guard", check=ok_check())
    assert read(b / "chunks/9.js") == stock


def test_max_tokens_floor_rewrites_small_values_only(patches):
    """P27: injected guard floors numeric max_tokens < 16 to 16 on the pre-dispatch body
    variable; guard reads `ah`, mutates in place, leaves the anchor statement intact."""
    p = by_id(patches, "max-tokens-floor")
    assert p.find == 'let aY=(0,s.SB)(ao);'
    assert p.replace.startswith('if(ah&&"number"==typeof ah.max_tokens&&ah.max_tokens<16)ah.max_tokens=16;')
    assert p.replace.endswith(p.find)


def test_max_tokens_floor_anchor_hits_real_build(patches):
    """P27 anchor must exist on the installed 0.5.69 build exactly once; skip when absent."""
    if not (Path(r"E:/Apps/npm-global/node_modules/9router/app/.next-cli-build") / "server").is_dir():
        pytest.skip("9router build not installed")
    p = by_id(patches, "max-tokens-floor")
    t = read(Path(
        r"E:/Apps/npm-global/node_modules/9router/app/"
        r".next-cli-build/server/chunks/8895.js"))
    assert t.count(p.find) == 1 or t.count(p.replace) == 1


def test_post_headroom_tool_result_remerge_patch_exists(patches):
    """P28 regression: a post-headroom merge must be anchored after compression, because
    Claude→OpenAI→Claude makes one user message per tool_result."""
    p = by_id(patches, "tool-result-remerge-post-headroom")
    assert p.find == 'let aZ=ah.messages?.length'
    assert '"tool_result"===$b2' in p.replace
    assert '$ms2.splice($i2+1,$mg2.length,$kp2)' in p.replace


def test_accept_text_plain_as_sse_relaxes_mime_guard(patches):
    """P29: MIME guard must let text/plain through (some providers send valid SSE bodies
    under text/plain); JSON + event-stream stays required, everything else still blocked."""
    p = by_id(patches, "accept-text-plain-as-sse")
    assert p.find == ('if(M&&!M.includes("text/event-stream")&&!M.includes("application/json"))')
    assert p.replace == ('if(M&&!M.includes("text/event-stream")&&!M.includes("application/json")'
                         '&&!M.includes("text/plain"))')


# ---------- p30: errbody-html-title (base parseError) ----------

def test_errbody_html_title_extracts_title_or_fallback(patches):
    """P30: incident path — base provider parseError returns raw body as message; HTML body
    must collapse to <title> before zL early-returns it into every log line."""
    p = by_id(patches, "errbody-html-title")
    assert p.find == 'parseError(a,b){return{status:a.status,message:b||`HTTP ${a.status}`}}'
    script = """
    const obj = { %s };
    const html = '<!DOCTYPE html><html><head><title>404: This page could not be found.</title></head><body></body></html>';
    let r = obj.parseError({status: 404}, html);
    if (r.message !== "404: This page could not be found.") throw new Error("bad: " + r.message);
    // non-HTML body unchanged; empty body falls back to HTTP <status>
    if (obj.parseError({status: 503}, "plain down").message !== "plain down") throw new Error("plain damaged");
    if (obj.parseError({status: 500}, "").message !== "HTTP 500") throw new Error("fallback broken");
    console.log("P30-HTML-TITLE-OK");
    """ % p.replace
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "P30-HTML-TITLE-OK" in r.stdout, r.stderr or r.stdout


def test_errbody_html_title_anchor_hits_real_build(patches):
    """P30 anchor must exist on the installed build exactly once (in 8499.js); skip if absent."""
    if not (Path(r"E:/Apps/npm-global/node_modules/9router/app/.next-cli-build") / "server").is_dir():
        pytest.skip("9router build not installed")
    p = by_id(patches, "errbody-html-title")
    t = read(Path(
        r"E:/Apps/npm-global/node_modules/9router/app/"
        r".next-cli-build/server/chunks/8499.js"))
    assert t.count(p.find) == 1 or t.count(p.replace) == 1


# ---------- p31: responses-thinking-history-400 ----------
# 2026-09-12 07:06/07:19 incidents: client posts openai-responses with tool history and no
# thinking/reasoning intent → translator 65377 emits reasoning_content-shaped assistant turns,
# the provider normalizer never injects a thinking param (its dummy-thinking path is gated on
# thinking.type==="enabled"), and the anthropic-compatible relay's DeepSeek branch runs in
# default thinking mode → 400 "The `content[].thinking` in the thinking mode must be passed
# back to the API" for every key, killing the whole combo chain.
# Measured on AgentRouter (anthropic-compatible-107f4d88, agentrouter.org):
#   toolhist + no thinking → 0 ok / 12 err;  toolhist + thinking → 136 ok / 0 err.

def test_responses_thinking_disabled_only_for_ambiguous_tool_history(patches):
    """Injected guard must inject thinking:{type:"disabled"} exactly for the failing shape:
    anthropic-compatible + tool history + NO thinking intent. Every explicit client intent
    (thinking param, reasoning_effort, reasoning, output_config.effort) and every other
    provider must pass through untouched — Claude Code's native requests stay adaptive."""
    p = by_id(patches, "responses-thinking-history-400")
    assert p.find == ('c.content=k,j&&!f&&a&&c.content.unshift(q(b))}}}}'
                      'if(a.tools&&Array.isArray(a.tools)){')
    i = p.replace.index(";(function(){")
    j = p.replace.index("})();if(a.tools", i) + len("})();")
    iife = p.replace[i + 1:j]
    assert p.find not in p.replace          # applied/clean are mutually exclusive states
    script = """
const b_claude = "anthropic-compatible-107f4d88";
const mk = (extra) => Object.assign({messages: [
  {role: "user", content: [{type: "text", text: "q"}]},
  {role: "assistant", content: [{type: "tool_use", id: "t1", name: "Read", input: {}}]},
  {role: "user", content: [{type: "tool_result", tool_use_id: "t1", content: "x"}]}
]}, extra);
const run = (a, b) => { %s; return a; };

// 1. the failing shape → disabled
let a = run(mk({}), b_claude);
if (JSON.stringify(a.thinking) !== '{"type":"disabled"}')
  throw new Error("failing shape not guarded: " + JSON.stringify(a.thinking));

// 2. no tool history (probe/classifier) → untouched
a = run({messages: [{role: "user", content: [{type: "text", text: "hi"}]}]}, b_claude);
if ("thinking" in a) throw new Error("probe body touched: " + JSON.stringify(a.thinking));

// 3. explicit thinking intent preserved (Claude Code native adaptive)
a = run(mk({thinking: {type: "adaptive"}}), b_claude);
if (JSON.stringify(a.thinking) !== '{"type":"adaptive"}')
  throw new Error("adaptive clobbered: " + JSON.stringify(a.thinking));

// 4. reasoning intent preserved (responses user asked for reasoning)
a = run(mk({reasoning_effort: "high"}), b_claude);
if ("thinking" in a) throw new Error("reasoning_effort body touched");
a = run(mk({reasoning: {effort: "high"}}), b_claude);
if ("thinking" in a) throw new Error("reasoning body touched");
a = run(mk({output_config: {effort: "max"}}), b_claude);
if ("thinking" in a) throw new Error("output_config.effort body touched");

// 5. other providers untouched
a = run(mk({}), "openai-compatible-chat-abc");
if ("thinking" in a) throw new Error("openai provider touched");
a = run(mk({}), "claude");
if ("thinking" in a) throw new Error("plain claude provider touched");

console.log("P31-THINKING-DISABLED-OK");
""" % iife
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "P31-THINKING-DISABLED-OK" in r.stdout, r.stderr or r.stdout


def test_responses_thinking_disabled_anchor_hits_real_build(patches):
    """P31 anchor must exist on the installed build exactly once (8499.js provider normalizer);
    skip when the build is absent or the anchor differs on this version."""
    build = Path(engine.build_dir()) if not _no_build() else None
    if build is None:
        pytest.skip("9router build not installed")
    p = by_id(patches, "responses-thinking-history-400")
    t = read(build / "server" / "chunks" / "8499.js")
    assert t.count(p.find) == 1 or t.count(p.replace) == 1


def _no_build():
    try:
        engine.build_dir()
    except Exception:
        return True
    return not (Path(engine.build_dir()) / "server").is_dir()


# ---------- p32: opencode free-tier tool signature ----------

def test_opencode_freetier_bumps_tool_signature_when_client_sends_tools(patches):
    """P32: `w(b,!0)` phải chạy cả khi client ĐÃ gửi tools. Upstream gate opencode.ai đòi
    tools array chứa cả `bash` và `read` chữ thường; guard cũ
    `Array.isArray(b.tools)&&0!==b.tools.length||w(b,!0)` bỏ qua nhánh bơm khi tools khác rỗng
    → Claude Code (77 tools, `Bash`/`Read` viết hoa) luôn 403 FreeTierError.
    Node unit mô phỏng nguyên văn hàm w() của upstream + body Claude Code."""
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    p32 = by_id(patches, "opencode-freetier-tool-signature")
    script = """
const u=[{type:"function",name:"bash",description:"This tool is currently unavailable and must not be used.",parameters:{type:"object",properties:{}}},{type:"function",name:"read",description:"This tool is currently unavailable and must not be used.",parameters:{type:"object",properties:{}}}];
const v=u;
function w(a,b){if(a&&"object"==typeof a)if(b){Array.isArray(a.tools)||(a.tools=[]);let b=new Set(a.tools.map(a=>a.name||a.function?.name));for(let c of v)b.has(c.name)||a.tools.push({...c});a.tool_choice||(a.tool_choice="auto")}else if(Array.isArray(a.tools)&&a.tools.length>0){let b=new Set(a.tools.map(a=>a.function?.name||a.name));for(let c of u)b.has(c.function.name)||a.tools.push({...c,function:{...c.function}})}else a.tools=u.map(a=>({...a,function:{...a.function}})),a.tool_choice||(a.tool_choice="none")}

// Claude Code payload: real tools, capitalised names, tool_choice already set
const real=[{type:"function",function:{name:"Agent"}},{type:"function",function:{name:"Bash"}},{type:"function",function:{name:"Read"}}];
const body={tools:JSON.parse(JSON.stringify(real)),tool_choice:"auto"};

// OLD guard: skipped w() entirely -> signature tools missing -> opencode.ai 403
const oldBody=JSON.parse(JSON.stringify(body));
Array.isArray(oldBody.tools)&&0!==oldBody.tools.length||w(oldBody,!0);
const oldNames=oldBody.tools.map(t=>t.name||t.function?.name);

// NEW guard (p32): always runs w() -> bash+read appended, real tools kept
const newBody=JSON.parse(JSON.stringify(body));
{ let b=newBody; %s
const newNames=newBody.tools.map(t=>t.name||t.function?.name);

if(oldNames.includes("bash")||oldNames.includes("read")) throw new Error("old guard unexpectedly bumped: "+oldNames);
if(!newNames.includes("bash")||!newNames.includes("read")) throw new Error("p32 did not bump signature: "+newNames);
for(const n of ["Agent","Bash","Read"]) if(!newNames.includes(n)) throw new Error("client tool dropped: "+n);
if(newNames.length!==5) throw new Error("unexpected tool count: "+newNames.length+" "+newNames);
if(newBody.tool_choice!=="auto") throw new Error("tool_choice clobbered: "+newBody.tool_choice);
console.log("P32-OK");
""" % p32.replace
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "P32-OK" in r.stdout, f"node failed: {r.stderr or r.stdout}"


def test_opencode_freetier_anchor_hits_real_build(patches):
    """P32 anchor must exist on the installed build exactly once (chunks/318.js opencode provider)."""
    build = Path(engine.build_dir()) if not _no_build() else None
    if build is None:
        pytest.skip("9router build not installed")
    p = by_id(patches, "opencode-freetier-tool-signature")
    t = read(build / "server" / "chunks" / "318.js")
    assert t.count(p.find) + t.count(p.replace) == 1


# ---------- locate (dead-anchor diagnosis) ----------

def test_stable_tokens_keeps_literals_and_long_idents():
    """Probe tokens = string literals >= 4 chars + identifiers >= 8 chars, order-preserving,
    de-duplicated. Short minified identifiers (a, b, i) are dropped: they rename freely."""
    find = 'function h({provider:a}){let i=d.xq[a]?.reasoningInject,j=e.find(a=>a.match(b));'
    toks = engine.stable_tokens(find)
    assert "function" in toks          # 8 chars -> kept
    assert "provider" in toks
    assert "reasoningInject" in toks
    assert "a" not in toks and "i" not in toks and "j" not in toks


def test_stable_tokens_keeps_long_string_literals():
    find = 'FETCH_CONNECT_TIMEOUT_MS",6e4'
    assert engine.stable_tokens(find) == ["FETCH_CONNECT_TIMEOUT_MS"]


def test_stable_tokens_drops_code_shaped_literals():
    """A 'literal' whose body contains code punctuation is a mis-lexed span, not a probe:
    it will not survive even a pure rename, so including it only adds noise."""
    find = '",headers:{"Content-Type":"application/json","x-api-key"'
    toks = engine.stable_tokens(find)
    assert '"application/json"' in toks
    assert '"x-api-key"' in toks
    assert not any("Content-Type" in t for t in toks)


def test_stable_tokens_is_deduped_and_ordered():
    a = engine.stable_tokens("alphaOne anotherIdent alphaOne")
    b = engine.stable_tokens("alphaOne anotherIdent alphaOne")
    assert a == b == ["alphaOne", "anotherIdent"]


def _locate_build(build: Path) -> list[engine.Patch]:
    """A one-patch patch set whose find lives in the fake build.

    NOTE for anyone editing these fixtures: the find must yield >= VERDICT_MIN_TOKENS (3)
    probe tokens, or `locate` reports `unknown` and every graded assertion below fails. A
    string literal containing `:` `/` `.` is NOT a probe token (see `_CODE_SHAPED`), so a
    fixture built around a URL yields exactly ONE token and silently degrades to `unknown`.
    The three identifiers below are the tokens this fixture is designed to produce."""
    return [engine.Patch(
        id="probe", order=1, group="probe", summary="s", why="w",
        find='let aY=(0,s.SB)(ao);reasoningInject(providerName,connectionId)',
        replace="X",
    )]


def test_locate_finds_the_file_with_the_most_surviving_tokens(tmp_path):
    """The patch's own file is the one holding the tightest cluster of its probe tokens."""
    build = tmp_path / "build"
    write(build, "server/chunks/noise.js", "reasoningInject but nothing else here")
    write(build, "server/chunks/home.js",
          'x' * 500 + "reasoningInject(providerName,connectionId)" + 'y' * 500)
    res = engine.locate(build, _locate_build(build))
    assert len(res) == 1
    loc = res[0]
    assert loc.patch_id == "probe"
    assert loc.file == "server/chunks/home.js"
    assert loc.matched_tokens == len(loc.tokens) == 3
    assert "reasoningInject" in loc.snippet


def test_locate_prefers_surviving_tokens_over_earlier_partial_hit(tmp_path):
    """A file matching only one token must lose to a file matching the whole cluster,
    even when the partial hit comes first in sort order."""
    build = tmp_path / "build"
    write(build, "a_partial.js", "reasoningInject")
    write(build, "z_full.js", "reasoningInject(providerName,connectionId)")
    loc = engine.locate(build, _locate_build(build))[0]
    assert loc.file == "z_full.js"


def test_locate_verdict_rename_likely_when_tokens_survive(tmp_path):
    build = tmp_path / "build"
    write(build, "server/chunks/home.js", "reasoningInject(providerName,connectionId)")
    loc = engine.locate(build, _locate_build(build))[0]
    assert loc.verdict == "rename-likely"


def test_locate_verdict_fixed_likely_when_tokens_are_gone(tmp_path):
    """Upstream rewrote the call site: no probe token survives -> the patch is obsolete."""
    build = tmp_path / "build"
    write(build, "server/chunks/home.js", "completely different code now")
    loc = engine.locate(build, _locate_build(build))[0]
    assert loc.verdict == "fixed-likely"
    assert loc.file is None and loc.snippet is None


def test_locate_verdict_unknown_below_three_probe_tokens(tmp_path):
    """A 20-char anchor of minified single-letter names yields almost no probe tokens.
    Guessing there would be worse than saying nothing, so report `unknown` + raw context."""
    build = tmp_path / "build"
    write(build, "server/chunks/home.js", "let aY=(0,s.SB)(ao);")
    ps = [engine.Patch(id="tiny", order=1, group="tiny", summary="s", why="w",
                       find='let aY=(0,s.SB)(ao);', replace="X")]
    loc = engine.locate(build, ps)[0]
    assert loc.tokens == []
    assert loc.verdict == "unknown"
    assert loc.snippet is not None          # raw find, so the human still has something


def test_locate_never_touches_the_build(tmp_path):
    build = tmp_path / "build"
    p = write(build, "server/chunks/home.js", "reasoningInject(providerName,connectionId)")
    before = p.read_text(encoding="utf-8"), p.stat().st_mtime_ns
    engine.locate(build, _locate_build(build))
    assert (p.read_text(encoding="utf-8"), p.stat().st_mtime_ns) == before


def test_locate_unknown_patch_id_raises(tmp_path):
    build = tmp_path / "build"
    write(build, "a.js", "x")
    with pytest.raises(PatchError, match="unknown patch id"):
        engine.locate(build, _locate_build(build), ids=["nope"])


def test_locate_ids_expand_to_whole_groups(tmp_path):
    """`_select` semantics carry over: asking about one sse-hang member reports the group."""
    build = tmp_path / "build"
    write(build, "a.js", "reasoningInject(providerName,connectionId)")
    res = engine.locate(build, engine.load_patches(), ids=["sse-close-translate"])
    assert {r.patch_id for r in res} == {
        "sse-close-translate", "sse-close-passthrough", "gauge-guard", "gauge-flush-route",
    }


def test_locate_reports_unknown_for_one_token_patch(tmp_path):
    """`connect-timeout-180s` has a single probe token (`FETCH_CONNECT_TIMEOUT_MS`), so even
    when that token is present the verdict stays `unknown` — below VERDICT_MIN_TOKENS."""
    build = tmp_path / "build"
    write(build, "a.js", 'FETCH_CONNECT_TIMEOUT_MS",6e4')
    loc = engine.locate(build, engine.load_patches(), ids=["connect-timeout-180s"])[0]
    assert loc.tokens == ["FETCH_CONNECT_TIMEOUT_MS"]
    assert loc.verdict == "unknown"


def test_locate_live_anchor_patch_is_reported_as_not_dead(tmp_path, patches):
    """An applied patch has nothing to diagnose; locate reports it without a verdict so the
    caller can list every patch state in one pass."""
    build = tmp_path / "build"
    p = by_id(patches, "connect-timeout-180s")
    write(build, "a.js", p.replace)
    res = engine.locate(build, patches, ids=["connect-timeout-180s"])
    assert res[-1].verdict == "applied"
    assert all(r.verdict == "applied" for r in res)   # ids pull the whole group

