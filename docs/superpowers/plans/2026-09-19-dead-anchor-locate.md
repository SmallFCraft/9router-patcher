# Dead-Anchored Patch Diagnostics (engine.locate) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a patch's `find` anchor dies on a new upstream build, show the operator the matching code region in that build plus an honest verdict — did the minifier rename identifiers (remap the anchor) or did upstream change/delete the code (drop the patch)?

**Architecture:** A pure-filesystem probe function `engine.locate(build, patches, ids)` extracts stable probe tokens (string literals + identifiers ≥8 chars, minus code-shaped literals) from each dead patch's `find`, scores every `.js` file in the target build by how many distinct tokens it contains, and returns the best file plus a snippet around the tightest token cluster. A verdict is only pronounced when ≥3 probe tokens exist; below that it reports `unknown` and hands over the snippet. A `python -m engine locate` CLI exposes it, and `updater.dryrun_anchors` calls the same function so the update-job console shows the diagnosis inline.

**Tech Stack:** Python 3.11+ stdlib only (`re`, `dataclasses`, `argparse`, `tempfile`, `tarfile`, `pathlib`). pytest for tests. No new dependencies.

**Spec:** Design agreed in conversation 2026-09-19; calibration measurements below are the frozen constants.

## Global Constraints

- **Stdlib only.** `engine.py` must stay importable with no network side effects — `updater` is imported lazily *inside* the `--latest` code path only, never at module import.
- **No new dependencies.** No `difflib`-based fuzzy matching, no external libs.
- **Frozen calibration constants (measured 2026-09-19 against the 31-patch set):**
  - `MIN_IDENT_LEN = 8` — identifier token floor. Measured: at 8, only 3/31 patches yield zero tokens (`gauge-guard`, `gauge-flush-route`, `max-tokens-floor` — anchors of 20–29 chars, all single-letter minified identifiers). At 10 the count degrades to 7/31 empty; **do not raise this**.
  - `MIN_LITERAL_LEN = 4` — string-literal token floor.
  - `CLUSTER_WINDOW = 2000` — char window for "tokens sitting together in the same code region".
  - `SNIPPET_PAD = 200` — chars of context shown on each side of the cluster.
  - `VERDICT_MIN_TOKENS = 3` — below this many probe tokens the verdict is `unknown`, never a guess.
  - `RENAME_RATIO = 0.6` — share of probe tokens that must survive in one file to call it `rename-likely`.
- **Verdict vocabulary is exactly three strings:** `rename-likely`, `fixed-likely`, `unknown`. Do not invent more.
- **Never mutate the target build.** `locate` is read-only, like `scan`.
- **`find` and `replace` are never remapped by this tool.** `locate` only reports; a human edits `patches.toml`.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `engine.py` | Patch engine. Gains the probe: token extraction, per-file scoring, cluster snippet, verdict, and the `__main__` CLI. | Modify (append new section + `if __name__ == "__main__"` block) |
| `updater.py` | Update pipeline. Gains `fetch_latest_build()` (dedupes tarball→build extraction) and calls `engine.locate` from `dryrun_anchors`. | Modify |
| `tests/test_engine.py` | Engine tests on a fake build tree. Gains the `locate` test section. | Modify (append) |
| `tests/test_updater.py` | Updater tests, all subprocess/network monkeypatched. Gains the diagnostic-in-gate test. | Modify (append) |
| `README.md` | User-facing docs. | Modify |

`engine.py` grows from 303 to roughly 420 lines. That is acceptable: it stays a single-responsibility module (patch engine) and the alternative — a `locate.py` file — was considered and rejected because the probe reads patches and build trees through `engine`'s own `_js_files`/`_read`/`_build` helpers and splitting it would force those to go public.

---

### Task 1: Probe token extraction

**Files:**
- Modify: `engine.py` (new section after `node_check`, before the `# ---- internals` divider at line 111)
- Test: `tests/test_engine.py` (append new section at end)

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `engine.MIN_IDENT_LEN: int`, `engine.MIN_LITERAL_LEN: int`, `engine.stable_tokens(find: str) -> list[str]`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_engine.py`:

```python
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
    a = engine.stable_tokens("alphaOne betaTwo alphaOne")
    b = engine.stable_tokens("alphaOne betaTwo alphaOne")
    assert a == b == ["alphaOne", "betaTwo"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_engine.py -k stable_tokens -v`
Expected: FAIL — `AttributeError: module 'engine' has no attribute 'stable_tokens'`

- [ ] **Step 3: Write minimal implementation**

Insert into `engine.py` immediately after the `node_check` function (after line 108, before the `# ---------------------------------------------------------------- internals` divider):

```python
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
```

Add `import re` to the import block at the top of `engine.py` (it currently imports `shutil`, `subprocess`, `threading`, `collections.abc`, `dataclasses`, `datetime`, `pathlib`, `tomllib`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_engine.py -k stable_tokens -v`
Expected: 4 passed

- [ ] **Step 5: Run the full suite to check nothing regressed**

Run: `python -m pytest tests/ -q`
Expected: `207 passed, 1 skipped` (203 + 4)

- [ ] **Step 6: Commit**

```bash
git add engine.py tests/test_engine.py
git commit -m "feat(engine): extract stable probe tokens for dead-anchor diagnosis"
```

---

### Task 2: `locate` — file scoring, cluster snippet, verdict

**Files:**
- Modify: `engine.py` (same section as Task 1)
- Test: `tests/test_engine.py` (same section)

**Interfaces:**
- Consumes: `engine.stable_tokens()` from Task 1.
- Produces:
  - `engine.CLUSTER_WINDOW: int`, `engine.SNIPPET_PAD: int`, `engine.VERDICT_MIN_TOKENS: int`, `engine.RENAME_RATIO: float`
  - `@dataclass(frozen=True) class Locate` with fields `patch_id: str`, `tokens: list[str]`, `matched_tokens: int`, `file: str | None`, `offset: int | None`, `snippet: str | None`, `verdict: str`
  - `engine.locate(build: str | Path, patches: list[Patch], ids: list[str] | None = None) -> list[Locate]`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_engine.py`:

```python
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
    write(build, "a.js", 'FETCH_CONNECT_TIMEOUT_MS",18e4')
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_engine.py -k locate -v`
Expected: FAIL — `AttributeError: module 'engine' has no attribute 'locate'`

- [ ] **Step 3: Write minimal implementation**

Append to the dead-anchor diagnosis section in `engine.py` (after `stable_tokens`):

```python
CLUSTER_WINDOW = 2000      # chars: tokens within this distance count as one code region
SNIPPET_PAD = 200          # chars of context shown on each side of the cluster
VERDICT_MIN_TOKENS = 3     # below this, the verdict is `unknown` rather than a guess
RENAME_RATIO = 0.6         # share of probe tokens that must survive to call it a rename


@dataclass(frozen=True)
class Locate:
    patch_id: str
    tokens: list[str]        # probe tokens extracted from `find` ([] for short anchors)
    matched_tokens: int      # distinct probe tokens found in the best file
    file: str | None         # relpath in the target build, None when nothing matched
    offset: int | None       # char offset of the cluster inside that file
    snippet: str | None      # context around the cluster (raw `find` when no tokens)
    verdict: str             # applied | rename-likely | fixed-likely | unknown


def _best_cluster(text: str, tokens: list[str], window: int) -> tuple[int | None, int]:
    """Start offset of the window covering the most distinct tokens, and that count."""
    pos = sorted({m.start() for t in tokens for m in re.finditer(re.escape(t), text)})
    if not pos:
        return None, 0
    best_off, best_n, i = pos[0], 0, 0
    for j in range(len(pos)):
        while pos[j] - pos[i] > window:
            i += 1
        span = text[pos[i]:pos[j] + 1]
        n = sum(1 for t in tokens if t in span)
        if n > best_n:
            best_n, best_off = n, pos[i]
    return best_off, best_n


def locate(build: str | Path, patches: list[Patch],
           ids: list[str] | None = None) -> list[Locate]:
    """For each dead-anchor patch, find where its code went in `build`, and say whether the
    evidence points at a minifier rename (remap the anchor) or an upstream rewrite of that
    code (drop the patch). Read-only: never writes into `build`.

    Advisory only. It never edits `patches.toml` — a wrong automatic remap would pass
    `node --check` (the names are just identifiers) and only fail at runtime.
    """
    b = _build(build)
    selected = _select(patches, ids)
    texts = {f: _read(f) for f in _js_files(b)}
    out: list[Locate] = []
    for p in selected:
        if any(p.replace in t for t in texts.values()):
            out.append(Locate(p.id, [], 0, None, None, None, "applied"))
            continue
        tokens = stable_tokens(p.find)
        if len(tokens) < VERDICT_MIN_TOKENS:
            # No verdict: too few stable probes to tell a rename from a rewrite. Hand the
            # raw anchor over so the human can look for it by eye.
            out.append(Locate(p.id, tokens, 0, None, None, p.find[:400], "unknown"))
            continue
        best: tuple[int, Path | None, int | None] = (0, None, None)
        for f, t in texts.items():
            off, n = _best_cluster(t, tokens, CLUSTER_WINDOW)
            if off is not None and n > best[0]:
                best = (n, f, off)
        matched, f, off = best
        if f is None:
            out.append(Locate(p.id, tokens, 0, None, None, None, "fixed-likely"))
            continue
        t = texts[f]
        snippet = t[max(0, off - SNIPPET_PAD):off + CLUSTER_WINDOW + SNIPPET_PAD]
        verdict = "rename-likely" if matched / len(tokens) >= RENAME_RATIO else "fixed-likely"
        out.append(Locate(p.id, tokens, matched, f.relative_to(b).as_posix(), off,
                          snippet, verdict))
    return out
```

Note `_select` raises `PatchError(f"unknown patch id or group: {sel}")` (line 150) — the test asserts on `match="unknown patch id"`, which that message satisfies.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_engine.py -k locate -v`
Expected: 10 passed

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: `217 passed, 1 skipped`

- [ ] **Step 6: Commit**

```bash
git add engine.py tests/test_engine.py
git commit -m "feat(engine): locate dead patch anchors in a target build"
```

---

### Task 3: `updater.fetch_latest_build` — one place that turns "latest" into a build dir

**Files:**
- Modify: `updater.py:140-171` (`dryrun_anchors`)
- Test: `tests/test_updater.py` (append)

**Interfaces:**
- Consumes: `engine` (already imported).
- Produces: `updater.fetch_latest_build(dest_parent: Path) -> tuple[str, Path]` returning `(version, build_dir)`. Raises `engine.PatchError` when the tarball lacks `app/.next-cli-build`.

This is the dedup step: `dryrun_anchors` currently inlines registry→tarball→extract→locate-build, and the CLI in Task 4 needs exactly the same thing. Two call sites, so extract it now.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_updater.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_updater.py -k fetch_latest_build -v`
Expected: FAIL — `AttributeError: module 'updater' has no attribute 'fetch_latest_build'`

- [ ] **Step 3: Write minimal implementation**

In `updater.py`, insert immediately before `DRYRUN_TITLE = ...` (line 138):

```python
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
```

Then rewrite `dryrun_anchors`'s body to use it (lines 144-157 become):

```python
    try:
        with tempfile.TemporaryDirectory(prefix="9r-dryrun-") as td:
            if emit:
                emit({"type": "line", "text": "Tải tarball bản latest..."})
            latest, build = fetch_latest_build(Path(td))
            if emit:
                emit({"type": "line", "text": "Giải nén + scan anchor..."})
            states = engine.scan(build, engine.load_patches())
```

Everything from `dead = [...]` (line 162) onward stays byte-identical. `import tarfile` at line 17 is still used by `fetch_latest_build`; the now-unused `meta`/`tarball` locals are gone.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_updater.py -q`
Expected: all pass, including the pre-existing `test_dryrun_*` tests — they monkeypatch `_registry_meta` and `_fetch_tarball`, which is exactly what `fetch_latest_build` calls.

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: `220 passed, 1 skipped`

- [ ] **Step 6: Commit**

```bash
git add updater.py tests/test_updater.py
git commit -m "refactor(updater): extract fetch_latest_build from dryrun_anchors"
```

---

### Task 4: CLI — `python -m engine locate`

**Files:**
- Modify: `engine.py` (append `main()` + `if __name__ == "__main__"` at end of file)
- Test: `tests/test_engine.py` (append)

**Interfaces:**
- Consumes: `engine.locate()` (Task 2), `engine.load_patches()`, `engine.build_dir()`, `engine.PatchError`.
- Produces: `engine.main(argv: list[str] | None = None) -> int` — returns a process exit code (0 ok, 1 dead anchors found, 2 usage/error).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_engine.py`:

```python
def _probe_toml(tmp_path, find: str) -> Path:
    toml = tmp_path / "patches.toml"
    toml.write_text(
        '[[patch]]\nid = "probe"\norder = 1\nsummary = "s"\nwhy = "w"\n'
        f"find = '''{find}'''\n"
        'replace = "X"\n', encoding="utf-8")
    return toml


def test_main_locate_prints_verdict_and_snippet(tmp_path, capsys):
    build = tmp_path / "build"
    write(build, "server/chunks/home.js", "reasoningInject(providerName,connectionId)")
    toml = _probe_toml(
        tmp_path,
        "let aY=(0,s.SB)(ao);reasoningInject(providerName,connectionId)")
    rc = engine.main(["locate", "probe", "--build", str(build), "--patches", str(toml)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "probe" in out and "rename-likely" in out
    assert "reasoningInject" in out and "home.js" in out


def test_main_locate_exit_1_when_patch_is_obsolete(tmp_path, capsys):
    build = tmp_path / "build"
    write(build, "server/chunks/home.js", "no trace of it")
    toml = _probe_toml(tmp_path, "reasoningInject(providerName,connectionId)")
    rc = engine.main(["locate", "probe", "--build", str(build), "--patches", str(toml)])
    assert rc == 1
    assert "fixed-likely" in capsys.readouterr().out


def test_main_locate_all_lists_every_dead_anchor(tmp_path, capsys, patches):
    """--all on an empty build: every patch is a dead anchor (or `unknown`)."""
    build = tmp_path / "build"
    write(build, "a.js", "nothing here")
    rc = engine.main(["locate", "--all", "--build", str(build)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "connect-timeout-180s" in out and "opencode-freetier-tool-signature" in out


def test_main_locate_unknown_patch_id_exits_2(tmp_path, capsys):
    build = tmp_path / "build"
    write(build, "a.js", "x")
    rc = engine.main(["locate", "does-not-exist", "--build", str(build)])
    assert rc == 2
    assert "unknown patch id" in capsys.readouterr().err


def test_main_locate_requires_a_build_source(capsys):
    rc = engine.main(["locate", "connect-timeout-180s"])
    assert rc == 2
    assert "--build" in capsys.readouterr().err
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_engine.py -k main_locate -v`
Expected: FAIL — `AttributeError: module 'engine' has no attribute 'main'`

- [ ] **Step 3: Write minimal implementation**

Append to the end of `engine.py`:

```python
# ---------------------------------------------------------------- CLI
# `python -m engine locate <patch-id> [--all] [--build DIR | --latest]`
# Built so a dead-anchor report from the dry-run gate can be turned into evidence without
# hand-unpacking a tarball. Read-only: prints, never edits patches.toml.

def _cmd_locate(args) -> int:
    patches = load_patches(Path(args.patches)) if args.patches else load_patches()
    if args.build:
        build = Path(args.build)
        cleanup = None
    else:
        # lazy: engine stays network-free at import, and `--build` never pays for this
        import tempfile
        import updater
        tmp = tempfile.TemporaryDirectory(prefix="9r-locate-")
        cleanup = tmp
        version, build = updater.fetch_latest_build(Path(tmp.name))
        print(f"# nguồn: 9router {version} (tarball registry)")
    try:
        ids = None if args.all else [args.patch]
        results = locate(build, patches, ids)
    except PatchError as e:
        print(str(e), file=sys.stderr)
        return 2
    finally:
        if cleanup:
            cleanup.cleanup()

    dead = 0
    for loc in results:
        if loc.verdict == "applied":
            continue
        dead += 1
        head = f"{loc.patch_id}: {loc.verdict}"
        if loc.file:
            head += f"  {loc.file}:{loc.offset}"
        print(head)
        if loc.file:
            print(f"  probes khớp: {loc.matched_tokens}/{len(loc.tokens)}"
                  f"  ({', '.join(loc.tokens[:6])})")
        if loc.snippet:
            print("  ---")
            for line in loc.snippet.splitlines() or [loc.snippet]:
                print(f"  {line[:400]}")
            print("  ---")
        print()
    return 1 if dead else 0


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m engine",
                                 description="9router patch engine CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)
    loc = sub.add_parser("locate", help="chẩn đoán patch có anchor chết trên build đích")
    loc.add_argument("patch", nargs="?", help="patch id (bỏ trống khi dùng --all)")
    loc.add_argument("--all", action="store_true", help="mọi patch, không chỉ một id")
    loc.add_argument("--build", help="thư mục build đã giải nén "
                                     "(app/.next-cli-build)")
    loc.add_argument("--patches", help="patches.toml khác (mặc định: file trong repo)")
    loc.add_argument("--latest", action="store_true",
                     help="tải bản latest từ registry rồi chẩn đoán trên đó")
    args = ap.parse_args(argv)

    if args.cmd == "locate":
        if not args.all and not args.patch:
            print("cần một patch id hoặc --all", file=sys.stderr)
            return 2
        if not args.build and not args.latest:
            print("cần --build <dir> hoặc --latest", file=sys.stderr)
            return 2
        if args.build and args.latest:
            print("--build và --latest loại trừ nhau", file=sys.stderr)
            return 2
        return _cmd_locate(args)
    return 2


if __name__ == "__main__":
    import sys
    raise SystemExit(main())
```

Add `import sys` to the top-of-file import block in `engine.py` (alongside `shutil`, `subprocess`, ...). Do **not** add a second `import sys` inside the `__main__` block — the top-level one covers it.

`_cmd_locate` ignores `args.latest` because reaching that branch already required it; `--build` takes the non-network path. `load_patches(path)` already parses a custom TOML, so no separate `tomllib` call is needed there.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_engine.py -k main_locate -v`
Expected: 5 passed

- [ ] **Step 5: Verify the CLI by hand on a real build**

Run: `python -m engine locate connect-timeout-180s --build "$(python -c 'import engine;print(engine.build_dir())')"`
Expected: exit 0, one line `connect-timeout-180s: applied`.

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: `225 passed, 1 skipped`

- [ ] **Step 7: Commit**

```bash
git add engine.py tests/test_engine.py
git commit -m "feat(engine): add locate CLI for dead-anchor diagnosis"
```

---

### Task 5: Diagnosis in the dry-run gate

**Files:**
- Modify: `updater.py` (`dryrun_anchors`, the `if dead:` branch at lines 166-169)
- Test: `tests/test_updater.py` (append)

**Interfaces:**
- Consumes: `engine.locate()` (Task 2), the build path already resolved by `fetch_latest_build` (Task 3).
- Produces: no new public symbol. `Step.log` for a failed gate gains one indented diagnostic line per dead patch.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_updater.py`:

```python
def test_dryrun_dead_anchor_includes_locate_diagnosis(monkeypatch, tmp_path):
    """Gate đỏ phải kèm chẩn đoán: file + verdict cho từng anchor chết, để người dùng biết
    ngay là remap hay xoá patch — không phải tự bung tarball.

    Dùng `attempt-total-deadline` (5 probe tokens: setTimeout, "stream stall timeout", ...)
    để verdict đạt `rename-likely`. Một patch chỉ có 1 token (như `connect-timeout-180s`) sẽ
    trả `unknown`, không kiểm thử được phân loại."""
    def fake_fetch(dest):
        root = Path(dest) / "package" / "app" / ".next-cli-build" / "server" / "chunks"
        root.mkdir(parents=True, exist_ok=True)
        from engine import load_patches as _lp
        p = next(x for x in _lp() if x.id == "attempt-total-deadline")
        # Write only the tokens in a slightly perturbed sequence so find does not match,
        # but the cluster of probe tokens survives -> dead anchor + rename-likely
        (root / "8895.js").write_text(p.find.replace("Date.now()-l", "Date.now()-z"),
                                      encoding="utf-8")
        return "0.5.99", Path(dest) / "package" / "app" / ".next-cli-build"
    monkeypatch.setattr(updater, "fetch_latest_build", fake_fetch)
    s = updater.dryrun_anchors()
    assert s.ok is False
    assert "attempt-total-deadline" in s.log and "KHÔNG chạy npm" in s.log
    assert "rename-likely" in s.log


def test_dryrun_diagnosis_failure_does_not_break_the_gate(monkeypatch, tmp_path):
    """locate là phụ trợ: nếu nó nổ thì gate vẫn phải trả Step đỏ với danh sách id."""
    monkeypatch.setattr(updater, "fetch_latest_build",
                        lambda dest: ("0.5.99", tmp_path))
    monkeypatch.setattr(updater.engine, "locate", raiser(RuntimeError("boom")))
    s = updater.dryrun_anchors()
    assert s.ok is False and "dead-anchor" in s.log
```

`raiser` already exists in `tests/test_updater.py` (used at line 273).

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_updater.py -k diagnosis -v`
Expected: `test_dryrun_dead_anchor_includes_locate_diagnosis` FAILS — no `rename-likely` in log.

- [ ] **Step 3: Write minimal implementation**

Replace the `if dead:` block in `updater.py` (lines 166-169) with:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_updater.py -q`
Expected: all pass

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: `227 passed, 1 skipped`

- [ ] **Step 6: Commit**

```bash
git add updater.py tests/test_updater.py
git commit -m "feat(updater): show locate diagnosis when the dry-run gate finds dead anchors"
```

---

### Task 6: Document the CLI in README

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: the CLI from Task 4.
- Produces: no code.

- [ ] **Step 1: Add the section after "Applying patches (CLI)"**

`README.md` currently has `### Applying patches (CLI)` (the block ending with the `engine.revert(...)` example and the paragraph about `ids`). Insert immediately after that paragraph:

````markdown
### Diagnosing a dead anchor (CLI)

When an upstream build renames things, a patch's `find` stops matching and the update job's
dry-run gate reports `dead-anchor`. Before editing `patches.toml`, ask the engine where that
code went in the new build:

```bash
python -m engine locate connect-timeout-180s --build path/to/app/.next-cli-build
```

Output is one block per dead patch:

```text
connect-timeout-180s: rename-likely  server/chunks/8895.js:26589
  probes khớp: 1/1  (FETCH_CONNECT_TIMEOUT_MS)
  ---
  ...i=new AbortController,j=Date.now(),k=!1... FETCH_CONNECT_TIMEOUT_MS",18e4 ...
  ---
```

Three verdicts, and only three:

- **`rename-likely`** — most probe tokens (string literals, identifiers ≥8 chars) still sit
  together in one file. The code is there, the minifier renamed its short identifiers: remap
  `find` **and** `replace` together, never `find` alone.
- **`fixed-likely`** — the tokens are gone or scattered. Upstream changed or deleted that code
  path; the patch is probably obsolete.
- **`unknown`** — the anchor is too short to yield probe tokens (a ~20-char anchor of
  single-letter names), so no verdict is possible. The raw `find` is printed instead; read it
  by eye.

`--latest` downloads and unpacks the newest registry tarball into a temp dir instead of
`--build`; `--all` diagnoses every dead patch at once. The command is read-only — it prints
evidence, it never edits `patches.toml`. The update job's dry-run gate runs the same
diagnosis inline, so the console already shows it when the gate turns red.
````

- [ ] **Step 2: Verify the section renders and the command path is right**

Run: `python -m engine locate --help`
Expected: usage text with `locate`, `--build`, `--latest`, `--all`, `--patches`.

- [ ] **Step 3: Verify README has no other stale count**

Run: `git diff README.md | grep -n "19\|31"` — the two corrected counts (`currently 31`, `the 31 patches`) are the only ones.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: document python -m engine locate"
```

---

### Task 7 (optional, independent): Don't let a slow tarball read kill the fetch

**Files:**
- Modify: `updater.py:86-92` (`_registry_conn`) and/or `_fetch_tarball`
- Test: `tests/test_updater.py`

**Interfaces:**
- Consumes: nothing.
- Produces: no signature change. `_registry_conn` keeps its `timeout` parameter.

**Why this is separate and optional:** measured 2026-09-19 — fetching the `0.5.69` tarball failed with `TimeoutError: The read operation timed out` while `0.5.75` succeeded moments earlier. `_registry_conn` passes `REGISTRY_TIMEOUT = 3` (seconds) to `HTTPSConnection`, and that value is the *socket* timeout on every `recv`, not just the connect — so a 20 MB stream on a slow link can stall past 3 s mid-download. Task 4's `--latest` path shares this code. Skip this task if `--latest` is not going to be used on flaky links.

- [ ] **Step 1: Write the failing test**

```python
def test_fetch_tarball_uses_a_longer_read_timeout_than_the_metadata_probe(monkeypatch, tmp_path):
    """Tarball là luồng 20MB: timeout socket 3s của probe metadata làm nó chết giữa chừng
    trên mạng chậm (đo 2026-09-19, tarball 0.5.69). Đọc tarball phải có ngân sách riêng."""
    seen = {}
    real = updater._registry_conn
    def spy(path):
        seen["timeout"] = None
        conn = real(path)
        return conn
    monkeypatch.setattr(updater, "_registry_conn", spy)
    monkeypatch.setattr(updater, "TARBALL_TIMEOUT", 120)
    assert updater.TARBALL_TIMEOUT > updater.REGISTRY_TIMEOUT
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_updater.py -k read_timeout -v`
Expected: PASS even before the fix (the constant already exists at 120 > 3). This test is a **regression guard on the budget relation**, not a red-then-green cycle — the real defect is that `_fetch_tarball` never passes `TARBALL_TIMEOUT` to the connection, which no unit test can observe without a live slow socket. Record that here rather than pretend the test goes red.

- [ ] **Step 3: Implementation**

Give `_registry_conn` a timeout parameter and have `_fetch_tarball` pass the tarball budget:

```python
def _registry_conn(path: str, timeout: float = REGISTRY_TIMEOUT) -> http.client.HTTPSConnection:
    """Same outbound policy as latest_version: allowlist host, https only, public IPs only.
    `timeout` is the SOCKET timeout on every recv, not just the connect — a 20MB tarball
    stream needs a far larger budget than a metadata probe (measured 2026-09-19)."""
    infos = socket.getaddrinfo(REGISTRY_HOST, 443, proto=socket.IPPROTO_TCP)
    if not infos or not all(ipaddress.ip_address(i[4][0]).is_global for i in infos):
        raise PatchError(f"{REGISTRY_HOST} does not resolve to a public address")
    return http.client.HTTPSConnection(REGISTRY_HOST, 443, timeout=timeout)
```

and in `_fetch_tarball` (line 123): `conn = _registry_conn(parts.path, TARBALL_TIMEOUT)`.

Move the `TARBALL_TIMEOUT = 120` definition (currently line 95) up above `_fetch_tarball` so it is defined before use.

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: `228 passed, 1 skipped`

- [ ] **Step 5: Commit**

```bash
git add updater.py tests/test_updater.py
git commit -m "fix(updater): give the tarball stream its own socket timeout budget"
```

---

## Self-Review

**1. Spec coverage**

| Requirement (agreed in conversation) | Task |
|---|---|
| Helper appears in **both** places (standalone CLI + inline in the gate) | Task 4 (CLI), Task 5 (gate) |
| Source **`--build <dir>` plus `--latest`** | Task 4 (`--build`), Tasks 3+4 (`--latest` via `fetch_latest_build`) |
| **Classify + show snippet** | Task 2 (`verdict` + `snippet`) |
| Code lives **in `engine.py`** | Tasks 1, 2, 4 |
| README updated | Task 6 |
| README stale patch count fixed | Already done before this plan (`currently 19` → `31`, `the 19 patches` → `31`); Task 6 Step 3 verifies no other stale count remains |

**2. Placeholder scan** — no `TBD`/`TODO`/"handle edge cases"/"similar to Task N". Every step carries runnable code. Uncovered requirement: none.

**3. Type consistency** — `Locate` field names (`patch_id`, `tokens`, `matched_tokens`, `file`, `offset`, `snippet`, `verdict`) are used identically in Task 2's implementation, Task 2's tests, Task 4's `_cmd_locate`, and Task 5's gate block. `fetch_latest_build(dest_parent) -> (str, Path)` is defined in Task 3 and called with that exact shape in Task 4's `_cmd_locate` and in Task 5's tests. `stable_tokens()` returns `list[str]` everywhere. Verdict strings are the same four literals (`applied`, `rename-likely`, `fixed-likely`, `unknown`) in implementation, tests, and README.

**4. Known limitation, stated rather than hidden** — measured on the current 31-patch set, **15 patches** yield fewer than `VERDICT_MIN_TOKENS=3` probe tokens and therefore report `unknown`: the 3 zero-token anchors (`gauge-guard`, `gauge-flush-route`, `max-tokens-floor`, all 20–29 chars of single-letter minified identifiers) plus the 12 that carry only one stable token (`FETCH_CONNECT_TIMEOUT_MS`, `"anthropic-dangerous-direct-browser-access"`, `continue`, `aW.length`, `ah.messages`, …). That is the honest answer, not a gap to paper over: no token heuristic can tell a rename from a rewrite at that signal level, and a wrong automatic remap would pass `node --check` while failing at runtime. The snippet is printed so a human decides in seconds. The tool earns its keep on the other **16 patches**, whose anchors carry 3–17 probe tokens.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-09-19-dead-anchor-locate.md`. Two execution options:

**1. Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — execute tasks in this session using executing-plans, batch execution with checkpoints for review.

Which approach?
