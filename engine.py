"""9router patch engine: load patches, scan a build tree, apply/revert with backup + node --check.

No HTTP, no FastAPI here — pure data + filesystem so it is testable without a web stack.
Byte semantics match 9router-patch.ps1: str.replace (all occurrences), and a patch is a
no-op on a file that already contains its replacement (p2's replacement embeds its own find).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tomllib import loads as toml_loads

HERE = Path(__file__).resolve().parent
PATCHES_FILE = HERE / "patches.toml"
# ngoài project: security scanner quét repo flag backup là SSRF (false positive trên code upstream);
# snapshots chỉ phục vụ rollback, không bao giờ được execute
BACKUP_ROOT = HERE.parent / "9router-backups"
BUILD_RELPATH = "app/.next-cli-build"
BACKUP_KEEP = 5
BACKUP_STAMP = "%Y%m%dT%H%M%S.%fZ"    # also the prune filter: only our own dirs match it
NODE_TIMEOUT = 30

# ponytail: one global lock, this is a single-user tool; per-build locks if it ever grows
LOCK = threading.RLock()


class PatchError(Exception):
    pass


@dataclass(frozen=True)
class Patch:
    id: str
    order: int
    group: str
    summary: str
    why: str
    find: str
    replace: str
    # name this patch's replacement DEFINES and other members of its group CALL: it must
    # be applied FIRST (calls never exist before the definition) and reverted LAST
    # (definition is only removed once every call is gone). E.g. p8 defines $G for p6/p7/p9.
    defines: str | None = None


@dataclass
class PatchState:
    patch: Patch
    state: str                    # applied | clean | partial | dead-anchor
    applied_files: list[str]      # relpaths containing `replace`
    clean_files: list[str]        # relpaths containing `find` but not `replace`


def load_patches(path: str | Path = PATCHES_FILE) -> list[Patch]:
    raw = toml_loads(Path(path).read_text(encoding="utf-8"))
    patches = [
        Patch(
            id=d["id"],
            order=d["order"],
            group=d.get("group", d["id"]),   # standalone patch is its own group
            summary=d["summary"],
            why=d["why"],
            find=d["find"],
            replace=d["replace"],
            defines=d.get("defines"),
        )
        for d in raw["patch"]
    ]
    return sorted(patches, key=lambda p: p.order)


def groups(patches: list[Patch]) -> dict[str, list[Patch]]:
    """{group name: members in apply order}, groups ordered by their first patch."""
    out: dict[str, list[Patch]] = {}
    for p in sorted(patches, key=lambda x: x.order):
        out.setdefault(p.group, []).append(p)
    return out


def install_dir() -> Path:
    """Global 9router install, derived at runtime from `npm root -g` (never hardcoded)."""
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm:
        raise PatchError("npm not found on PATH")
    r = subprocess.run([npm, "root", "-g"], capture_output=True, text=True, timeout=30)
    if r.returncode != 0 or not r.stdout.strip():
        raise PatchError(f"`npm root -g` failed: {(r.stderr or r.stdout).strip()}")
    return Path(r.stdout.strip()) / "9router"


def build_dir() -> Path:
    return install_dir() / BUILD_RELPATH


def node_check(path: str | Path) -> None:
    """Raise PatchError if `node --check` rejects the file. List-arg, no shell (paths hold `[id]`)."""
    node = shutil.which("node")
    if not node:
        raise PatchError("node not found on PATH")
    r = subprocess.run([node, "--check", str(path)],
                       capture_output=True, text=True, timeout=NODE_TIMEOUT)
    if r.returncode != 0:
        raise PatchError(f"node --check failed for {path}:\n{(r.stderr or r.stdout).strip()}")


# ------------------------------------------------- dead-anchor diagnosis
# Calibrated 2026-09-19 against the 31-patch set: at MIN_IDENT_LEN=8 only 3 patches yield
# zero probe tokens; at 10 it degrades to 7. Do not raise it.
MIN_IDENT_LEN = 8
MIN_LITERAL_LEN = 4

# A string literal whose body holds code punctuation is a mis-lexed span of source, not a
# stable probe — a rename would not preserve it, so counting it only adds noise.
_CODE_SHAPED = set("{}():;=[]\\,")

_TOKEN_RE = re.compile(
    r'"[^"\n]{%d,}"' % MIN_LITERAL_LEN
    + r"|'[^'\n]{%d,}'" % MIN_LITERAL_LEN
    + r"|[A-Za-z_$][\w$.]{%d,}" % (MIN_IDENT_LEN - 1)
)


def stable_tokens(find: str) -> list[str]:
    """Identifiers and literals from `find` that a minifier rename would preserve.

    Single-letter minified names rename freely and are useless as probes; string literals and
    long identifiers survive a rename, so their presence in a new build is evidence that the
    same code region still exists there. Order-preserving, de-duplicated.
    """
    out: list[str] = []
    for t in _TOKEN_RE.findall(find):
        if t[0] in "\"'" and any(c in _CODE_SHAPED for c in t[1:-1]):
            continue
        if t not in out:
            out.append(t)
    return out


# ---------------------------------------------------------------- internals

def _read(f: Path) -> str:
    # newline="": no CRLF translation. surrogateescape: stray non-UTF8 bytes survive a
    # read->write round trip untouched (minified bundles are not guaranteed clean UTF-8).
    return f.read_text(encoding="utf-8", errors="surrogateescape", newline="")


def _write(f: Path, text: str) -> None:
    f.write_text(text, encoding="utf-8", errors="surrogateescape", newline="")


def _build(build: str | Path) -> Path:
    b = Path(build).resolve()
    if not b.is_dir():
        raise PatchError(f"build dir not found: {b}")
    return b


def _js_files(b: Path) -> list[Path]:
    # b is already resolved; return RESOLVED children only, so the containment check that
    # passed here is the path every later write/backup/rollback reuses. A directory link
    # inside the build can be repointed outside between enumeration and write, so the
    # unresolved alias path is not safe to carry (FINDING 3).
    return sorted({f.resolve() for f in b.rglob("*.js")
                   if f.is_file() and f.resolve().is_relative_to(b)})


def _select(patches: list[Patch], ids: list[str] | None) -> list[Patch]:
    """Expand ids (patch id or group name) to whole groups, sorted by order. Validates unknowns."""
    ordered = sorted(patches, key=lambda p: p.order)
    if ids is None:
        return ordered
    wanted: set[str] = set()
    for sel in ids:
        hit = next((p.group for p in ordered if p.id == sel), None)
        if hit is None and any(p.group == sel for p in ordered):
            hit = sel
        if hit is None:
            raise PatchError(f"unknown patch id or group: {sel}")
        wanted.add(hit)
    return [p for p in ordered if p.group in wanted]


def _preflight(b: Path, texts: dict[Path, str], selected: list[Patch]) -> None:
    """Operation-level check before ANY write.

    Every selected patch must be satisfiable somewhere: anchor present (appliable) or
    replacement present (already applied, no-op). A group is only editable in files that
    satisfy the WHOLE group, and no file may carry a proper subset of its anchors: p6-p9 all
    live in 8895.js and p6/p7 call `$G()` that p8 defines, so rewriting p9's generic anchor
    in a chunk without p8 still passes `node --check` (it is only an identifier) and throws
    ReferenceError at runtime.
    """
    for g, members in groups(selected).items():
        hits = {f: [p for p in members if p.find in t or p.replace in t]
                for f, t in texts.items()}
        dead = [p.id for p in members
                if not any(p.id in (q.id for q in h) for h in hits.values())]
        if dead:
            raise PatchError("dead anchor, nothing written: " + ", ".join(dead))
        if not any(len(h) == len(members) for h in hits.values()):
            raise PatchError(f"group {g} anchors split across files, nothing written: "
                             + ", ".join(p.id for p in members))
        partial = sorted(f for f, h in hits.items() if 0 < len(h) < len(members))
        if partial:
            detail = "; ".join(f"{f.relative_to(b).as_posix()} has only "
                               + ", ".join(p.id for p in hits[f]) for f in partial)
            raise PatchError(f"group {g} anchors partially present, nothing written: {detail}")


def _is_snapshot(d: Path) -> bool:
    """True only for dirs this engine created, so a hand-kept backup survives retention."""
    try:
        datetime.strptime(d.name, BACKUP_STAMP)
    except ValueError:
        return False
    return True


def _snapshot(b: Path, files: list[Path]) -> Path:
    """One backup dir per operation; keep BACKUP_KEEP newest of OUR OWN dirs."""
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    snap = BACKUP_ROOT / datetime.now(timezone.utc).strftime(BACKUP_STAMP)
    for f in files:
        dst = snap / f.relative_to(b)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dst)
    ours = sorted(d for d in BACKUP_ROOT.iterdir() if d.is_dir() and _is_snapshot(d))
    for old in ours[:-BACKUP_KEEP]:
        shutil.rmtree(old)
    return snap


def _commit(b: Path, new: dict[Path, str], check: Callable[[Path], None]) -> list[str]:
    """Backup once, write once, verify all, roll every file back on any failure."""
    if not new:
        return []
    files = sorted(new)
    snap = _snapshot(b, files)
    try:
        for f in files:
            _write(f, new[f])
        for f in files:
            check(f)
    except BaseException as verify_error:
        dirty = []
        for f in files:                       # try EVERY file: one locked file must not
            src = snap / f.relative_to(b)     # leave the rest silently patched
            try:
                shutil.copy2(src, f)
            except OSError as e:
                dirty.append(f"  {f}  <-  {src}  ({type(e).__name__}: {e})")
        if not dirty:
            raise
        orig = (str(verify_error) if isinstance(verify_error, PatchError)
                else f"{type(verify_error).__name__}: {verify_error}")
        raise PatchError(f"{orig}\nROLLBACK INCOMPLETE - still patched, restore by hand:\n"
                         + "\n".join(dirty)) from verify_error
    return [f.relative_to(b).as_posix() for f in files]


# ---------------------------------------------------------------- public ops

def scan(build: str | Path, patches: list[Patch]) -> list[PatchState]:
    b = _build(build)
    texts = {f: _read(f) for f in _js_files(b)}
    out = []
    for p in sorted(patches, key=lambda x: x.order):
        applied, clean = [], []
        for f, t in texts.items():
            rel = f.relative_to(b).as_posix()
            if p.replace in t:          # replacement first: p2.replace contains p2.find
                applied.append(rel)
            elif p.find in t:
                clean.append(rel)
        state = ("partial" if applied and clean else
                 "applied" if applied else
                 "clean" if clean else "dead-anchor")
        out.append(PatchState(patch=p, state=state, applied_files=applied, clean_files=clean))
    return out


def _group_order(patches: list[Patch], undo: bool = False) -> list[Patch]:
    """Composition order within a group. A patch whose replacement DEFINES a symbol its
    group-mates call (p8, `defines = "$G"`) must be applied FIRST — no `$G()` call lands
    before the definition — and undone LAST, once every call is already gone.
    (Plan.md measurement: safe removal is p6, p7, p9, p8 last; apply is p8 first.)
    Byte result is order-independent; order only matters if an operation dies mid-way."""
    return sorted(patches, key=lambda p: ((p.defines is not None) == undo, p.order))


def apply(build: str | Path, patches: list[Patch], ids: list[str] | None = None,
          check: Callable[[Path], None] | None = None) -> list[str]:
    """Apply selected patches (an id pulls in its whole group). Returns changed relpaths."""
    b = _build(build)
    selected = _select(patches, ids)
    check = check or node_check
    with LOCK:
        texts = {f: _read(f) for f in _js_files(b)}
        _preflight(b, texts, selected)
        new = {}
        for f, t in texts.items():
            out = t
            for p in _group_order(selected):            # definer (p8) first, else by order
                if p.replace not in out and p.find in out:
                    out = out.replace(p.find, p.replace)
            if out != t:
                new[f] = out
        return _commit(b, new, check)


def revert(build: str | Path, patches: list[Patch], group: str,
           check: Callable[[Path], None] | None = None) -> list[str]:
    """Revert a whole group (sse-hang p6-p9 must move together). Returns changed relpaths."""
    b = _build(build)
    members = sorted((p for p in patches if p.group == group), key=lambda p: p.order)
    if not members:
        raise PatchError(f"unknown group: {group}")
    check = check or node_check
    with LOCK:
        texts = {f: _read(f) for f in _js_files(b)}
        _preflight(b, texts, members)
        new = {}
        for f, t in texts.items():
            out = t
            for p in _group_order(members, undo=True):  # definer (p8) undone last: calls gone
                if p.replace in out:
                    out = out.replace(p.replace, p.find)
            if out != t:
                new[f] = out
        return _commit(b, new, check)
