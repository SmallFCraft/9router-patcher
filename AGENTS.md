<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **9router-patcher** (934 symbols, 2039 relationships, 84 execution flows). See `CLAUDE.md` for full operating rules and CLI/MCP fallbacks.

> If any GitNexus tool warns the index is stale, run `npx gitnexus analyze` in terminal first.

## Always Do

- **MUST run impact analysis before editing any symbol.** Run `impact({target: "symbolName", direction: "upstream", repo: "9router-patcher"})` (CLI: `gitnexus impact <symbolName> --repo 9router-patcher`).
- **MUST run `detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `query({search_query: "concept", repo: "9router-patcher"})` to find execution flows.
- When you need full context on a specific symbol, use `context({name: "symbolName", repo: "9router-patcher"})`.

## Never Do

- NEVER edit a function, class, or method without first running `impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `rename` tool.
- NEVER commit changes without running `detect_changes()` to check affected scope.

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
