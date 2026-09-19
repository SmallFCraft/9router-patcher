# 9router Patch Manager — Operating Rules

See [CLAUDE.md](CLAUDE.md) for full architecture, verified test baselines, and operating rules.

## GitNexus Critical Traps
- **Tool names**: Use `impact`, `context`, `detect_changes`, `query` (or `mcp__plugin_gitnexus_gitnexus__*`). Do NOT use `gitnexus_*` prefixed names.
- **CLI requires `--repo`**: Always pass `--repo 9router-patcher`. 16 repos indexed globally; omitting `--repo` throws `Multiple repositories indexed`.
- **CLI path**: Use `node E:\Apps\npm-global\node_modules\gitnexus\dist\cli\index.js` or `npx gitnexus`. `%APPDATA%\npm\gitnexus` on PATH is stale (1.6.4-rc.48).

<!-- gitnexus:start -->
<!-- gitnexus:end -->
