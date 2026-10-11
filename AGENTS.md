# 9router Patch Manager — Operating Rules

Authoritative rules live in [CLAUDE.md](CLAUDE.md). Read it first; this file is only the quick pointer.

## Quick Reference

- **Test Suite**: `python -m pytest tests/ -q` (439 passed, 5 skipped — verified 2026-10-11)
- **Engine Tests**: `python -m pytest tests/test_engine.py -q` (117 passed, 2 skipped)
- **Autostart**: `shell:startup\9router-patch.vbs` (wscript ẩn); không dùng Run key / schtasks (xem [CLAUDE.md](CLAUDE.md))
- **Build Executable**: `python build_app.py` -> `dist\9router-patch.exe` (publish ~18 MB zstd; dev loop: add `--fast`)
- **Dashboard Port**: `127.0.0.1:20129` (local-only, CSRF-guarded)
- **GitNexus Repo**: `--repo 9router-patcher` on every call (16 repos indexed; bare tool names `impact`/`context`/`query`/`detect_changes` — never `gitnexus_*` prefix. CLI: prefer `npx gitnexus` / explicit `node E:\Apps\npm-global\node_modules\gitnexus\dist\cli\index.js`; PATH `gitnexus.CMD` is stale 1.6.4-rc.48)
- **Patches**: `patches.toml` single source (50 patches, 46 groups); after edit run `python tools\make_patches_blob.py`

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **9router-patcher** (1358 symbols, 3788 relationships, 120 execution flows).

> Index stale? Run `node .gitnexus/run.cjs analyze --index-only` from the project root — it auto-selects an available runner. No `.gitnexus/run.cjs` yet? Bootstrap with `npx`, `bunx`, or `pnpm dlx` — e.g. `bunx gitnexus@latest analyze` (npm 11 npx crash; #1939).

## Always Do

- **MUST run impact before editing.** Use `impact({target: "symbolName", direction: "upstream"})` or `node .gitnexus/run.cjs impact "symbolName" --direction upstream --repo .`; report callers, processes, and risk. Never substitute grep for graph analysis.
- **MUST analyze graph changes before committing.** Use `detect_changes({scope: "all"})` (MCP) or `node .gitnexus/run.cjs detect-changes --scope all --repo .` (CLI fallback). `partial: true` or `truncated: true` is not a clean check — a zero means unseen, not unaffected; re-run it. For regression review: `detect_changes({scope: "compare", base_ref: "main"})` or `node .gitnexus/run.cjs detect-changes --scope compare --base-ref "main" --repo .`.
- MUST warn on HIGH/CRITICAL `risk` pre-edit; never use `riskSharedAxes` to waive a HIGH/CRITICAL `risk` warning. Compare File/symbol: MCP File omits axes; Graph-RAG expands File.
- **MUST treat `risk: UNKNOWN` as unresolved, not as low.** An empty caller set is not evidence the symbol is unused — it can also mean the callers are not resolvable by the index (plain-object property access, dynamic dispatch, cross-language calls). `impact` pairs `UNKNOWN` with a `riskNote` saying so. Confirm with a text search before treating the symbol as safe to change or delete; do not proceed on the strength of a zero.
- **MUST use `query({search_query: "concept"})` for concepts/flows, `context({name: "symbolName"})` for a named symbol, or `impact` for blast radius, on read-only callers, dependencies, imports, or execution flow.** Graph first; text search only for empty/`UNKNOWN`/literals.
- For security review, `explain({target: "fileOrSymbol"})` lists taint findings (source→sink flows; needs `analyze --pdg`).

## Never Do

- NEVER edit a function, class, or method before MCP/CLI impact analysis.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis, and never read `UNKNOWN` as an all-clear — it means the walk could not answer, which is the one verdict that requires confirming by other means.
- NEVER rename symbols with find-and-replace — use `rename` which understands the call graph.
- NEVER commit before MCP/CLI graph change analysis.

## Resources

| Resource | Use for |
| --- | --- |
| `gitnexus://repo/9router-patcher/context` | Codebase overview, check index freshness |
| `gitnexus://repo/9router-patcher/clusters` | All functional areas |
| `gitnexus://repo/9router-patcher/processes` | All execution flows |
| `gitnexus://repo/9router-patcher/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
| --- | --- |
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
(Plugin regenerates content between the markers above. Authoritative GitNexus rules are in CLAUDE.md — if the regenerated block disagrees, CLAUDE.md wins.)
