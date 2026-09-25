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
<!-- gitnexus:end -->
(Plugin regenerates content between the markers above. Authoritative GitNexus rules are in CLAUDE.md — if the regenerated block disagrees, CLAUDE.md wins.)
