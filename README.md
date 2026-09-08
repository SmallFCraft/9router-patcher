# 9router Patch Manager

Local-only management dashboard and patch engine for the [9router](https://www.npmjs.com/package/9router) npm package — an AI proxy that routes Claude-Code-compatible clients (Claude Code, ZCode, …) across multiple LLM providers, keys and combo fallback chains.

This repo does **not** contain the proxy itself. It manages a locally installed copy of it:

- **`main.py`** — FastAPI dashboard (binds `127.0.0.1:20129`): provider/key/combo overview, live usage stats, patch apply/revert buttons, and an update job with an SSE console.
- **`updater.py`** — update pipeline: version probe → lock probe → stop stack → `npm install` → re-apply patches → restart. Owns the port-based lifecycle of the managed stack (router `:20128`, headroom `:8787`).
- **`engine.py`** — the patch engine: loads `patches.toml`, scans the installed build, applies/reverts find-and-replace patches atomically with per-operation backups, `node --check` verification and automatic rollback.
- **`patches.toml`** — the single source of truth for all patches (currently 19), each measured byte-exact against a specific build of `9router`.

```
┌──────────────┐   manages    ┌─────────────────────────────┐
│ dashboard    │─────────────▶│ npm 9router (installed copy)│
│ 127.0.0.1:   │              │ router  :20128              │
│ 20129        │              │ headroom:8787               │
└──────┬───────┘              └──────────┬──────────────────┘
       │ engine.apply / revert           │ serves Claude-Code-
       ▼                                 ▼ clients
┌──────────────┐    patches    ┌─────────────────────────────┐
│ patches.toml │──────────────▶│ app/.next-cli-build/*.js    │
└──────────────┘  find/replace └─────────────────────────────┘
```

## Requirements

- **Windows** (lifecycle uses `taskkill /T /F`, drive-letter paths; other platforms untested)
- **Python 3.11+** (developed on 3.14)
- **Node.js + npm** with the target package installed globally: `npm install -g 9router`
- Python packages: `fastapi`, `uvicorn`, `jinja2`

## Installation

```bat
git clone <this-repo> 9router
cd 9router
pip install fastapi uvicorn jinja2
```

The engine auto-detects the global npm install directory. If the target package is not installed yet:

```bat
npm install -g 9router
```

## Usage

### Dashboard

```bat
python main.py
```

Then open `http://127.0.0.1:20129`. The dashboard is local-only: it refuses any request not addressed to `127.0.0.1:20129`, and every mutating POST must carry the page's own `Origin`/`Referer`. It never reads the proxy's `data.sqlite` and never logs `.env` contents.

From the UI you can:

- inspect the installed build, patch states (applied / clean / dead-anchor) and the diff of every patch
- apply or revert patches — individually or as atomic groups
- run the update job (`version probe → lock → stop → npm install → re-apply → restart`) while watching the live SSE console

### Applying patches from the CLI

```python
import engine
patches = engine.load_patches()          # parses patches.toml
engine.scan(engine.build_dir(), patches) # per-patch state against the installed build
engine.apply(engine.build_dir(), patches, ids=["empty-stream-fallback"])
engine.revert(engine.build_dir(), patches, group="sse-hang")
```

`ids` accepts patch ids or group names and always expands to whole groups. Every operation: preflight (no dead/partial anchors, group must live in one file) → snapshot to the backup root → write all files → `node --check` every written file → roll **every** file back on any failure.

### Managed stack lifecycle

```python
import updater
updater.stop_router_stack(lambda m: print(m))   # port-based taskkill /T /F (router + headroom)
updater.start_router_stack(lambda m: print(m))
```

Always restart the stack through these functions. Starting the server by hand can silently fail to take the port while the old process keeps serving stale code (a stale process holds `:20128` and the new one dies quietly leaving an empty log).

## The patch system

`patches.toml` is the single source of truth. Each patch is a byte-exact `find`/`replace` pair measured against one specific build of the package:

```toml
[[patch]]
id = "service-error-fast-recover"
order = 13
summary = "400 service_error: fallback immediately, lock model 5s instead of 30s"
why   = "transient provider 400 hit the default 30s model lock and killed the whole provider"
find    = 'function e(a,b,c=0){let f=b?(…).toLowerCase():"";for(let b of d.t2)'
replace = 'function e(a,b,c=0){let f=…;if(400===a&&f.includes("service_error"))return{shouldFallback:!0,cooldownMs:5e3};for(let b of d.t2)'
```

Conventions that keep this sane on minified bundles:

- `order` — explicit apply sequence.
- `group` — patches of one feature live in the same file and apply/revert atomically; `defines` marks a patch that must apply first / revert last when it defines a symbol others call.
- Injected identifiers always use a `$` prefix so they can never collide with minified single-letter names.
- When the upstream package updates, identifier remaps must change `find` and `replace` **together** — a remapped `find` with an old `replace` passes `node --check` and still breaks at runtime.
- Patch state is version-locked: measurements taken on one build are re-verified by the test suite against the live install.

## Current patch set (19)

| # | id | what it does |
|---|----|--------------|
| 1 | connect-timeout-180s | upstream connect timeout 60s → 180s |
| 2–4 | ua-* | send `claude-cli` User-Agent on messages/models routes (provider 401s otherwise) |
| 5 | tools-strip-custom | drop `type:"custom"` tools some providers reject with 400 |
| 6–9 | sse-hang (group) | close SSE streams after terminal events, keep the in-flight gauge consistent |
| 10–11 | nonstream-sse-retry (group) | retry stream:false requests that receive garbage SSE and aggregate the answer instead of 502 |
| 12 | claude-system-hoist | hoist `role:"system"` messages out of `messages[]` to top-level `system` (strict backend validators) |
| 13 | service-error-fast-recover | `400 service_error` / `upstream non-SSE` → fallback now, 5s lock instead of 30s |
| 14 | reasoning-effort-body-cap | bodies > 448 KB with `xhigh/max` reasoning → cap to `high` (huge compact payloads) |
| 15 | attempt-total-deadline | per-attempt total deadline, default 240s, cuts slow-drip streams the stall timer can't see |
| 16 | empty-stream-fallback | a stream that ends with zero content becomes a 502 → key/model fallback, instead of a silent empty "success" |
| 17 | errbody-read-timeout | bound error-body reads to 8s (some providers hang chunked error bodies) |
| 19 | claude-tool-result-canonicalize | merge tool_result blocks a client split across consecutive user messages into one, matching Anthropic adjacency (stops flaky 400 tool_use-without-tool_result) |
| 18 | non-sse-failure-metadata | non-SSE 200 blocker now carries status+error → lock classifier takes the 5s transient branch instead of the 30s default |

## Environment variables

| variable | default | effect |
|----------|---------|--------|
| `9R_ATTEMPT_DEADLINE_MS` | `240000` | total wall-clock budget for one upstream attempt (`<=0` disables) |
| `9R_ERRBODY_TIMEOUT_MS` | `8000` | max time spent reading a non-2xx response body |
| `FETCH_CONNECT_TIMEOUT_MS` | `180000` | (patched) upstream connect timeout |

## Testing

```bat
python -m pytest tests/ -q
```

174 tests: engine apply/revert/rollback semantics (including link-repoint and partial-anchor hazards), updater pipeline steps, and dashboard routes. Tests that need the real installed build or `node` skip automatically when absent.

## Project structure

```
main.py         FastAPI dashboard (127.0.0.1:20129)
updater.py      update pipeline + stack lifecycle (ports 20128 / 8787)
engine.py       patch engine: scan / apply / revert, backups, node --check
patches.toml    the 19 patches (single source of truth)
templates/      dashboard pages (Jinja2)
tests/          pytest suite
```

Backups live **outside** the repository (default `<repo-parent>/9router-backups/`): they are byte-exact snapshots used for rollback, never executed, and kept out of the tree so repo-wide security scanners don't flag them.

## Notes & disclaimer

This tool patches a locally installed copy of a third-party npm package for personal use. Provider behaviour (quota, content moderation, client restrictions) is outside its control — patches only make the router react correctly (fail fast, fall back, lock briefly). Use at your own discretion and in accordance with your providers' terms.
