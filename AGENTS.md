# 9router Patch Manager — Operating Rules

See [CLAUDE.md](CLAUDE.md) for full architecture, verified test baselines, and operating rules.

## Quick Reference

- **Test Suite**: `python -m pytest tests/ -q` (245 passed, 1 skipped)
- **Engine Tests**: `python -m pytest tests/test_engine.py -q` (98 passed, 1 skipped)
- **Build Executable**: `python build_app.py` -> `dist\9router-patch.exe`
- **Dashboard Port**: `127.0.0.1:20129` (local-only, CSRF-guarded)
- **GitNexus Repo**: `--repo 9router-patcher` (CLI: use `npx gitnexus`, not `.gitnexus/run.cjs`)

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **9router-patcher** (736 symbols, 2839 relationships, 66 execution flows).

> Index stale? Run `npx gitnexus analyze` from the project root. (Note: `.gitnexus/run.cjs` has storage-version drift; always use `npx gitnexus`).

## Always Do

- **MUST run impact analysis before editing.** Use `impact({target: "symbolName", direction: "upstream", repo: "9router-patcher"})` (MCP) or `npx gitnexus impact "symbolName" --direction upstream --repo 9router-patcher` (CLI fallback); report callers, processes, and risk.
- **MUST analyze graph changes before committing.** Use `detect_changes({repo: "9router-patcher"})` (MCP) or `npx gitnexus detect-changes --repo 9router-patcher` (CLI fallback).
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- **MUST treat `risk: UNKNOWN` as unresolved, not as low.** Confirm with text search before treating symbol as safe.
- When exploring unfamiliar code, use `query({search_query: "concept", repo: "9router-patcher"})`.
- When you need full context on a symbol, use `context({name: "symbolName", repo: "9router-patcher"})`.
- For security review, `explain({target: "fileOrSymbol", repo: "9router-patcher"})` lists taint findings.

## Never Do

- NEVER edit a function, class, or method before MCP/CLI impact analysis.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `rename` which understands the call graph.
- NEVER commit before MCP/CLI graph change analysis.
<!-- gitnexus:end -->
