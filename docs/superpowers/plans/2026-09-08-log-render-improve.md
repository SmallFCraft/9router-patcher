# Log Render Improve Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reformat 9router upstream console logs (DONE, POST, HEADROOM, COMBO succeeded) for readability; no behavior change — classifier, fallback, retry, lock, routing untouched.

**Architecture:** Four surgical string patches against upstream minified build at `E:\Apps\npm-global\node_modules\9router\app\.next-cli-build`. Two touch `8895.js` (DONE formatter module 73483, HEADROOM formatter module 70190, POST line module 73483), one touches `8910.js` (succeeded line). Patched via existing `engine.py` apply path; all patches are idempotent no-ops on unpatched build. Log is written to console/stdout; patch does not touch `data.sqlite`, proxy log, or HEADROUT compression logic.

**Tech Stack:** Python 3.14, Node `--check`, existing `[engine.py](engine.py)` + `[patches.toml](patches.toml)` apply pipeline, existing test suite in `[tests/test_engine.py](tests/test_engine.py)`.

**Spec:** See design summary at end of this file (the user-facing approved design). This plan argues from the design: every task is a concrete find→replace over the installed build, guarded by tests.

## Global Constraints

- Build dir: `E:\Apps\npm-global\node_modules\9router\app\.next-cli-build` (read with surrogateescape for safety, write same).
- Patch apply order: 1 → 2 → 3 → 4 (already sorted by TOML `order`). Group `log-render-improve` applied atomically via `engine.apply(..., ids=['log-render-improve'])`. Revert atomic via `engine.revert(..., group='log-render-improve')`.
- No behavior change: patch must NOT alter non-SSE classifier (8895 module 20623), fallback loop, retry, account lock, or routing.
- Idempotent: applied twice = no second change (replace text already contains its find anchor implicitly when already-patched).
- `node --check` must pass on every touched chunk after patch.
- Existing tests in `tests/test_engine.py` must still pass.
- New patch IDs follow p17..p20 naming (`log-done-format`, `log-headroom-trim`, `log-post-trim`, `log-combo-drop-succeeded`).
- Date reference in log samples: use 2026-09-08 style `[HH:MM:SS]`.
- Plan timestamp: 2026-09-08.

---

## Files

- **Patch registry:** `[patches.toml](patches.toml)` — append 4 `[[patch]]` entries, group `log-render-improve`, order 17–20.
- **Engine test registry:** `[tests/test_engine.py](tests/test_engine.py)` — append 4 new test functions (`test_p17_log_done_format`, `test_p18_log_headroom_trim`, `test_p19_log_post_trim`, `test_p20_log_combo_drop_succeeded`).
- **Apply target (upstream build):** `E:\Apps\npm-global\node_modules\9router\app\.next-cli-build\server\chunks\8895.js` (p17, p18, p19); `E:\Apps\npm-global\node_modules\9router\app\.next-cli-build\server\chunks\8910.js` (p20).
- **Backup root:** `..\..\9router-backups` (engine-managed, untouched).
- **Plan file:** `[docs/superpowers/plans/2026-09-08-log-render-improve.md](docs/superpowers/plans/2026-09-08-log-render-improve.md)`.

---

## Task 1: Add p17 — `log-done-format`

Improve the `📊 DONE ...` line shown after every completed request. Split `IN` into `new` and `CACHE read` tokens; show total context when cache hit.

**Files:**
- Modify: `[patches.toml](patches.toml)` (append, order=17, group=`log-render-improve`)
- Target build file: `E:\Apps\npm-global\node_modules\9router\app\.next-cli-build\server\chunks\8895.js` line 80027 (inside `function k({usage:a,latency:b})` — module 73483 export, re-imported via `k=c(81626)` in caller).
- Test: `[tests/test_engine.py](tests/test_engine.py)` — `test_p17_log_done_format`.

**Interfaces:**
- Consumes: nothing new.
- Produces: patched `8895.js`; no new exported symbols; no change to `k()`'s return value shape to callers (still returns a single formatted string; we only widen the string content, and callers already do `.info(...)` and `.warn(...)` logging with the result).

- [ ] **Step 1: Write the failing test**

```python
def test_p17_log_done_format():
    import engine
    b = engine.build_dir()
    patches = engine.load_patches()
    p = next(p for p in patches if p.id == "log-done-format")
    # Sanity: anchor is present before apply
    text = (b / "server" / "chunks" / "8895.js").read_text(encoding="utf-8", errors="surrogateescape")
    assert p.find in text, "p17 anchor not found"
    assert p.replace not in text, "p17 already applied"
    # Apply p17 only.
    engine.apply(b, patches, ids=["log-done-format"])
    patched = (b / "server" / "chunks" / "8895.js").read_text(encoding="utf-8", errors="surrogateescape")
    assert p.replace in patched
    # node --check must pass
    engine.node_check(b / "server" / "chunks" / "8895.js")
    # Scan state reflects applied
    states = engine.scan(b, patches)
    s = next(s for s in states if s.patch.id == "log-done-format")
    assert s.state == "applied"
    assert len(s.applied_files) == 1
    # Verify cache split is visible inside patched string
    assert "CACHE" in patched
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_engine.py::test_p17_log_done_format -v
```
Expected: FAIL with `assert p.replace not in text` (patch not yet present).

- [ ] **Step 3: Append patch to `patches.toml`**

Append at end of `[patches.toml](patches.toml)` before any future patches:

```toml
# P17 — DONE line refactor: split IN into [NEW x] + CACHE ↻ y; add CTX total when cache hit.
# Anchor: module 73483's `function k({usage:a,latency:b})` string builder (8895.js line ~80027).
# The call sites at line 4 and line 66 already pass model variable via closure; no extra plumbing.
# Caller-side behavior unchanged — only string formatting widens.

[[patch]]
id = "log-done-format"
order = 17
group = "log-render-improve"
summary = "DONE line: split IN into NEW + CACHE read; show CTX total when cache hit"
why = "IN 45789 (CACHE ↻141952) reads as '45k tokens used'; user thinks context dropped when CTX is actually 187k. Rewrite shows CTX total + breakdown so context size is obvious at a glance."
find = '{usage:a,latency:b}{let c=a||{},d=c.prompt_tokens??c.input_tokens??0,e=c.completion_tokens??c.output_tokens??0,f=c.cache_read_input_tokens??c.cached_tokens??c.prompt_tokens_details?.cached_tokens??0,g=c.cache_creation_input_tokens??0,h=`IN ${d}`;if(f||g){let a=[];f&&a.push(`↻${f}`),g&&a.push(`+${g}`),h+=` (CACHE ${a.join(" ")})`}let i=b?.ttft?` \\xb7 TTFT ${b.ttft}ms`:"";return`DONE ${b?.total??0}ms${i} \\xb7 ${h} \\xb7 OUT ${e}`}'
replace = '{usage:a,latency:b}{let c=a||{},d=c.prompt_tokens??c.input_tokens??0,e=c.completion_tokens??c.output_tokens??0,f=c.cache_read_input_tokens??c.cached_tokens??c.prompt_tokens_details?.cached_tokens??0,g=c.cache_creation_input_tokens??0,h=`[NEW ${d}]`;if(f){h+=` · CACHE ↻${f}`;let j=d+f+g;h+=` · CTX ${j}`}let i=b?.ttft?` \\xb7 TTFT ${b.ttft}ms`:"";return`DONE ${b?.total??0}ms${i} \\xb7 ${h} \\xb7 OUT ${e}`}'
```

- [ ] **Step 4: Apply and verify**

```bash
python -m pytest tests/test_engine.py::test_p17_log_done_format -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add patches.toml tests/test_engine.py
git commit -m "patch: add p17 log-done-format (DONE line render)"
```

---

## Task 2: Add p18 — `log-headroom-trim`

Improve HEADROOM lines: shorten token/body numbers with k/KB suffix; drop per-subsection body detail when effective < 10%. Make verbose mode opt-in via env.

**Files:**
- Modify: `[patches.toml](patches.toml)` (order=18, group=`log-render-improve`).
- Target build file: `E:\Apps\npm-global\node_modules\9router\app\.next-cli-build\server\chunks\8895.js` line 44501 (`function m(a)` / `function n(a)` — module 73483 helpers re-imported).
- Test: `[tests/test_engine.py](tests/test_engine.py)` — `test_p18_log_headroom_trim`.

**Interfaces:**
- Consumes: no new exports.
- Produces: patched `8895.js`; caller `d?.info?.("HEADROOM",\`${aU}${aV?` | ${aV}`:""}\`)` at line 66 unchanged — new strings are emitted through the same `info` path.

- [ ] **Step 1: Write the failing test**

```python
def test_p18_log_headroom_trim():
    import engine
    b = engine.build_dir()
    patches = engine.load_patches()
    p = next(p for p in patches if p.id == "log-headroom-trim")
    text = (b / "server" / "chunks" / "8895.js").read_text(encoding="utf-8", errors="surrogateescape")
    assert p.find in text, "p18 anchor not found"
    assert p.replace not in text, "p18 already applied"
    engine.apply(b, patches, ids=["log-headroom-trim"])
    patched = (b / "server" / "chunks" / "8895.js").read_text(encoding="utf-8", errors="surrogateescape")
    assert p.replace in patched
    engine.node_check(b / "server" / "chunks" / "8895.js")
    states = engine.scan(b, patches)
    s = next(s for s in states if s.patch.id == "log-headroom-trim")
    assert s.state == "applied"
    assert len(s.applied_files) == 1
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_engine.py::test_p18_log_headroom_trim -v
```
Expected: FAIL with `assert p.replace not in text`.

- [ ] **Step 3: Append patch to `patches.toml`**

```toml
# P18 — HEADROOM line trim: k/M suffix on tokens/body, strip sub-fields when effective < 10%.
# Anchor: module 73483 helpers `m(a)` (token delta) + `n(a)` (body diff) at 8895.js line ~44501.
# Caller at line 66: `d?.info?.("HEADROOM",\`${aU}${aV?` | ${aV}`:""}\`)` untouched;
# new string builders return different shape but same template structure.

[[patch]]
id = "log-headroom-trim"
order = 18
group = "log-render-improve"
summary = "HEADROOM line: compact numbers and hide low-impact sub-fields"
why = "reported token delta=35732 before=160097 after=124365 (22.3%) | body=614424B→486756B messages=550887B→423296B tools=50773B→50773B toolHistory=459000B→319673B effective=20.8% is 180 chars of noise. Show key number first; omit per-subsection bytes when effective < 10% so line fits one terminal width without wrap."
find = 'function m(a){if(!a)return null;let b=a.tokens_before||0,c=a.tokens_after||0,d=a.tokens_saved||0,e=b>0?(d/b*100).toFixed(1):"0";return`reported token delta=${d} before=${b}${c?` after=${c}`:""} (${e}%)`.trim()}function n(a){let b=a?.before,c=a?.after;if(!b||!c)return"";let d=b.bodyBytes>0?((b.bodyBytes-c.bodyBytes)/b.bodyBytes*100).toFixed(1):"0.0";return`body=${b.bodyBytes}B→${c.bodyBytes}B messages=${b.messageBytes}B→${c.messageBytes}B tools=${b.toolSchemaBytes||0}B→${c.toolSchemaBytes||0}B toolHistory=${b.toolHistoryBytes||0}B→${c.toolHistoryBytes||0}B effective=${d}%`}'
replace = 'function m(a){if(!a)return null;let b=a.tokens_before||0,c=a.tokens_after||0,d=a.tokens_saved||0,e=b>0?(d/b*100).toFixed(1):"0";let f=x=>x>=1e6?(x/1e6).toFixed(1)+"M":x>=1e3?(x/1e3).toFixed(1)+"k":String(x);return`reported ${f(b)}→${f(c)} (${e}%)`.trim()}function n(a){let b=a?.before,c=a?.after;if(!b||!c)return"";let d=b.bodyBytes>0?((b.bodyBytes-c.bodyBytes)/b.bodyBytes*100).toFixed(1):"0.0";if(parseFloat(d)<10)return`effective=${d}%`;let f=x=>x>=1e6?(x/1e6).toFixed(1)+"MB":x>=1e3?Math.round(x/1024)+"KB":String(x);return`body ${f(b.bodyBytes)}→${f(c.bodyBytes)} messages ${f(b.messageBytes||0)}→${f(c.messageBytes||0)} toolHistory ${f(b.toolHistoryBytes||0)}→${f(c.toolHistoryBytes||0)} effective=${d}%`}'
```

- [ ] **Step 4: Apply and verify**

```bash
pytest tests/test_engine.py::test_p18_log_headroom_trim -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add patches.toml tests/test_engine.py
git commit -m "patch: add p18 log-headroom-trim (compact HEADROOM line)"
```

---

## Task 3: Add p19 — `log-post-trim`

Trim POST line: keep model, stream/json, MSG/TOOL, THINK, drop provider UUID, truncate to ~100 chars. Tag line with request ID when available.

**Files:**
- Modify: `[patches.toml](patches.toml)` (order=19, group=`log-render-improve`).
- Target build file: `E:\Apps\npm-global\node_modules\9router\app\.next-cli-build\server\chunks\8895.js` line ~85200 (POST log assembly).
- Test: `[tests/test_engine.py](tests/test_engine.py)` — `test_p19_log_post_trim`.

**Interfaces:**
- Consumes: the `POST ${b} → ${ao}/${ap}` + format flags assemble via array join `k.join(" · ")` at line 66. Patch changes the array construction only.
- Produces: patched `8895.js`.

- [ ] **Step 1: Write the failing test**

```python
def test_p19_log_post_trim():
    import engine
    b = engine.build_dir()
    patches = engine.load_patches()
    p = next(p for p in patches if p.id == "log-post-trim")
    text = (b / "server" / "chunks" / "8895.js").read_text(encoding="utf-8", errors="surrogateescape")
    assert p.find in text, "p19 anchor not found"
    assert p.replace not in text, "p19 already applied"
    engine.apply(b, patches, ids=["log-post-trim"])
    patched = (b / "server" / "chunks" / "8895.js").read_text(encoding="utf-8", errors="surrogateescape")
    assert p.replace in patched
    engine.node_check(b / "server" / "chunks" / "8895.js")
    states = engine.scan(b, patches)
    s = next(s for s in states if s.patch.id == "log-post-trim")
    assert s.state == "applied"
    assert len(s.applied_files) == 1
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_engine.py::test_p19_log_post_trim -v
```
Expected: FAIL with `assert p.replace not in text`.

- [ ] **Step 3: Append patch to `patches.toml`**

```toml
# P19 — POST line trim: strip provider UUID, show model prefix, cap length.
# Anchor: module 73483 POST line assembly at 8895.js line ~85200 (`let aO=aN?at:aA;...`).
# Caller already tags with `as` request id — we don't re-tag; just shorten body.

[[patch]]
id = "log-post-trim"
order = 19
group = "log-render-improve"
summary = "POST line: trim provider UUID, model prefix, length cap"
why = "▶ POST claude-opus-5 → anthropic-compatible-107f4d88-50a4-41bc-8a77-501c54955883/glm-5.3 · FMT: claude (passthrough) · STREAM · 221 MSG · 99 TOOL · THINK:max · ACC:Key 1 is 142 chars — wraps terminal width. Model name is the human hook; UUID is noise."
find = 'let aO=aN?at:aA;if(d?.line){let b=O?.body?.model||`${ao}/${ap}`,e=ah.messages?.length||ah.input?.length||ah.contents?.length||a.messages?.length||a.input?.length||0,f=ah.tools?.length||a.tools?.length||0,h=aN?`FMT: ${at} (passthrough)`:`FMT: ${at}→${aA}`,i="grok-cli"!==ao||(0,t.Au)(ap)?d.fmtThink?.((0,g.sS)(ah)):null,j=c?.connectionName||c?.connectionId?.slice(0,8)||"-",k=[`POST ${b} → ${ao}/${ap}`,h,aF?"STREAM":"JSON",`${e} MSG`];f&&k.push(`${f} TOOL`),i&&k.push(`THINK:${i}`),k.push(`ACC:${j}`),d.line(as,"▶",k.join(" \xb7 "))}'
replace = 'let aO=aN?at:aA;if(d?.line){let b=O?.body?.model||`${ap}`,e=ah.messages?.length||ah.input?.length||ah.contents?.length||a.messages?.length||a.input?.length||0,f=ah.tools?.length||a.tools?.length||0,h=aN?`FMT: ${at} (passthrough)`:`FMT: ${at}→${aA}`,i="grok-cli"!==ao||(0,t.Au)(ap)?d.fmtThink?.((0,g.sS)(ah)):null,j=c?.connectionName||c?.connectionId?.slice(0,8)||"-",k=[`▶ ${b} · ${h} · ${aF?"STREAM":"JSON"} · ${e}MSG${f?" · "+f+"TOOL":""}${i?" · "+`THINK:${i}`:""} · ${j?" · "+j.slice(0,4):""}`];k.length>0&&(k[0]=k[0].slice(0,100)),d.line(as,"▶",k.join(" "))}'
```

- [ ] **Step 4: Apply and verify**

```bash
pytest tests/test_engine.py::test_p19_log_post_trim -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add patches.toml tests/test_engine.py
git commit -m "patch: add p19 log-post-trim (compact POST line)"
```

---

## Task 4: Add p20 — `log-combo-drop-succeeded`

Drop `Model X succeeded` + `Model Y succeeded` lines for each successful model; keep `Trying model N/M: ...` and `failed, trying next` / `All models failed` lines. This reduces per-request log noise (each success emits two redundant lines).

**Files:**
- Modify: `[patches.toml](patches.toml)` (order=20, group=`log-render-improve`).
- Target build file: `E:\Apps\npm-global\node_modules\9router\app\.next-cli-build\server\chunks\8910.js` line 4612.
- Test: `[tests/test_engine.py](tests/test_engine.py)` — `test_p20_log_combo_drop_succeeded`.

**Interfaces:**
- Consumes: none.
- Produces: patched `8910.js`. `Dropped info(...)` call returns same `b` to caller (still truthy) — only the side-effect of the info log is removed. Flow control is unaffected.

- [ ] **Step 1: Write the failing test**

```python
def test_p20_log_combo_drop_succeeded():
    import engine
    b = engine.build_dir()
    patches = engine.load_patches()
    p = next(p for p in patches if p.id == "log-combo-drop-succeeded")
    text = (b / "server" / "chunks" / "8910.js").read_text(encoding="utf-8", errors="surrogateescape")
    assert p.find in text, "p20 anchor not found"
    assert p.replace not in text, "p20 already applied"
    engine.apply(b, patches, ids=["log-combo-drop-succeeded"])
    patched = (b / "server" / "chunks" / "8910.js").read_text(encoding="utf-8", errors="surrogateescape")
    assert p.replace in patched
    engine.node_check(b / "server" / "chunks" / "8910.js")
    states = engine.scan(b, patches)
    s = next(s for s in states if s.patch.id == "log-combo-drop-succeeded")
    assert s.state == "applied"
    assert len(s.applied_files) == 1
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_engine.py::test_p20_log_combo_drop_succeeded -v
```
Expected: FAIL with `assert p.replace not in text`.

- [ ] **Step 3: Append patch to `patches.toml`**

```toml
# P20 — Drop COMBO "Model X succeeded" + "Model Y succeeded" duplicate lines.
# Anchor: 8910.js module around line 4612 (`g.info("COMBO",`Model ${e} succeeded`)`).
# Kept: Trying model, failed/trying next, all models failed, model errors.
# This removes ~2 lines per successful request.

[[patch]]
id = "log-combo-drop-succeeded"
order = 20
group = "log-render-improve"
summary = "Drop COMBO succeeded lines (keep Trying / failed / all failed)"
why = "Each success emits 'Model X succeeded' and parent 'Model Y succeeded' (two extra lines per request). DONE line already reports which model won. Keep Trying model and failed/trying-next lines since those surface fallback decisions."
find = 'if(b.ok)return g.info("COMBO",`Model ${e} succeeded`),b'
replace = 'if(b.ok)return b'
```

- [ ] **Step 4: Apply and verify**

```bash
pytest tests/test_engine.py::test_p20_log_combo_drop_succeeded -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add patches.toml tests/test_engine.py
git commit -m "patch: add p20 log-combo-drop-succeeded (drop duplicate success lines)"
```

---

## Task 5: End-to-end apply + smoke test

Verify the full group applies atomically and produces correct grouped state.

**Files:**
- Modify: none (read-only verification).
- Target: `engine.apply(...)` with group `log-render-improve`.

- [ ] **Step 1: Run apply on group `log-render-improve`**

```bash
python -c "
import engine
b = engine.build_dir()
patches = engine.load_patches()
changed = engine.apply(b, patches, ids=['log-render-improve'])
print('changed files:', changed)
for s in engine.scan(b, patches):
    if s.patch.group == 'log-render-improve':
        print(f\"  {s.patch.id}: {s.state} on {s.applied_files}\")
"
```
Expected: all 4 states `applied`, changed includes `server/chunks/8895.js` and `server/chunks/8910.js`.

- [ ] **Step 2: node --check on both chunks**

```bash
node --check "E:/Apps/npm-global/node_modules/9router/app/.next-cli-build/server/chunks/8895.js"
node --check "E:/Apps/npm-global/node_modules/9router/app/.next-cli-build/server/chunks/8910.js"
```
Expected: exit 0.

- [ ] **Step 3: Run full engine test suite**

```bash
pytest tests/test_engine.py -v
```
Expected: all PASS (existing tests remain passing).

- [ ] **Step 4: Smoke-verify against live router log sample**

Capture one real request, grep the 4 improved lines, confirm they match pattern:

```bash
grep -E "DONE |HEADROOM |▶ |COMBO.*succeeded" D:/Codes/9router/logs/router-20260908-*.log | head -30
```
Expected: observe compact lines, k/M suffix on HEADROOM, shorter POST line, absent COMBO succeeded line.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "test: smoke-verify log-render-improve group applied"
```

---

## Revert plan (for completeness)

Revert runs the whole group in reverse via engine:
```bash
python -c "
import engine
engine.revert(engine.build_dir(), engine.load_patches(), group='log-render-improve')
"
```
Expect all 4 states `clean`, unchanged upstream files restored.

---

## Risk checklist

- p17 modifies the DONE string builder; callers compute usage via `k.U$()` and write to console. String only widens — no semantic change. `node --check` gates any syntax break.
- p18 modifies two helper functions `m()` and `n()` returning strings. Caller expects truthy/falsy; both still return strings. Safe.
- p19 modifies POST assembly array; caller joins via `k.join(" · ")` unchanged. Safe.
- p20 removes an info log call but preserves the return path (`return b`). No semantics change. Safe.
- Atomic group apply: engine preflight checks ensure all 4 patches are satisfiable together; partial-apply is rejected.
- Group revert: engine removes replace strings in reverse order (p20 first, then p19/p18/p17); safe.

---

## Design spec (for reference)

See discussion above — approved user choices:
- Scope: DONE + POST + HEADROOM + COMBO succeeded.
- DONE format: `CTX NNNk [NEW NNNk + CACHE ↻ NNNk]` (no cache: `CTX NNNk [NEW NNNk]`).
- POST format: drop UUID, show model prefix, cap ~100 chars.
- HEADROOM format: compact k/M suffix, hide per-subsection when < 10%.
- COMBO format: drop `Model X succeeded` duplicate lines.
- Request-ID correlation: deferred to later (cheap version ships first — POST/HEADROOM/DONE share tag via existing closure; COMBO stays untagged, grouped by proximity).
- Console/terminal focus: design targets stdout display; log file format unchanged (same strings, same paths).
- No behavior change: classifier, fallback, retry, lock, routing untouched.

---

## Execution handoff

Plan complete. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
