# 9router Patch Manager — Operating Rules

Authoritative rules live in [CLAUDE.md](CLAUDE.md). Read it first; this file is only the quick pointer.

## Quick Reference

- **Test Suite**: `python -m pytest tests/ -q` (378 passed, 5 skipped — verified 2026-09-25)
- **Engine Tests**: `python -m pytest tests/test_engine.py -q` (104 passed, 2 skipped)
- **Build Executable**: `python build_app.py` -> `dist\9router-patch.exe` (publish ~18 MB zstd; dev loop: add `--fast`)
- **Dashboard Port**: `127.0.0.1:20129` (local-only, CSRF-guarded)
- **GitNexus Repo**: `--repo 9router-patcher` on every call (16 repos indexed; bare tool names `impact`/`context`/`query`/`detect_changes` — never `gitnexus_*` prefix. CLI: prefer `npx gitnexus` / explicit `node E:\Apps\npm-global\node_modules\gitnexus\dist\cli\index.js`; PATH `gitnexus.CMD` is stale 1.6.4-rc.48)
- **Patches**: `patches.toml` single source (35 patches, 31 groups); after edit run `python tools\make_patches_blob.py`

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **9router-patcher** (2137 symbols, 6828 relationships, 190 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> If any GitNexus tool warns the index is stale, run `npx gitnexus analyze` in terminal first.

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `gitnexus_impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `gitnexus_detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `gitnexus_query({query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `gitnexus_context({name: "symbolName"})`.

## Never Do

- NEVER edit a function, class, or method without first running `gitnexus_impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `gitnexus_rename` which understands the call graph.
- NEVER commit changes without running `gitnexus_detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/9router-patcher/context` | Codebase overview, check index freshness |
| `gitnexus://repo/9router-patcher/clusters` | All functional areas |
| `gitnexus://repo/9router-patcher/processes` | All execution flows |
| `gitnexus://repo/9router-patcher/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
(Plugin regenerates content between the markers above. Authoritative GitNexus rules are in CLAUDE.md — if the regenerated block disagrees, CLAUDE.md wins.)
