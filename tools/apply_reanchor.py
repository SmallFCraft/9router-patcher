"""Apply re-anchored find/replace into patches.toml, by line, refusing anything partial.

Input: { "<patch-id>": ["<find>", "<replace>"], ... }

Each patch block must match its `find = ` and `replace = ` lines exactly once; a missing
id or a double hit aborts before writing. Round-trips both TOML string forms.

    python tools/apply_reanchor.py <candidates.json>
"""
from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path


def toml_literal(s: str) -> str:
    if "'" not in s and "\n" not in s and "\r" not in s:
        return f"'{s}'"
    body = (s.replace("\\", "\\\\").replace('"', '\\"')
             .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))
    return f'"{body}"'


def patch_id(line: str) -> str | None:
    rest = line.split("=", 1)[1].strip() if "=" in line else ""
    if len(rest) >= 2 and rest[0] == rest[-1] and rest[0] in "\"'":
        return rest[1:-1]
    return None


def main(argv: list[str]) -> int:
    cands: dict[str, list[str]] = json.loads(Path(argv[0]).read_text("utf-8"))
    lines = Path("patches.toml").read_text("utf-8").splitlines(keepends=True)

    hits: dict[str, int] = {}
    cur_id = None
    in_block = False
    for line in lines:
        if line.startswith("[[patch]]"):
            in_block, cur_id = True, None
            continue
        if not in_block:
            continue
        if line.startswith("id = "):
            cur_id = patch_id(line)
            continue
        if cur_id in cands and (line.startswith("find = ") or line.startswith("replace = ")):
            hits[cur_id] = hits.get(cur_id, 0) + 1

    parsed = {p["id"] for p in tomllib.loads("".join(lines))["patch"]}
    missing = [pid for pid in cands if pid not in parsed]
    bad = [pid for pid in cands if hits.get(pid) != 2]
    if missing or bad:
        print("REFUSING TO WRITE: missing:", missing, "bad counts:", bad)
        return 1

    cur_id = None
    in_block = False
    for idx, line in enumerate(lines):
        if line.startswith("[[patch]]"):
            in_block, cur_id = True, None
            continue
        if not in_block:
            continue
        if line.startswith("id = "):
            cur_id = patch_id(line)
            continue
        if cur_id not in cands:
            continue
        nf, nr = cands[cur_id]
        if line.startswith("find = "):
            lines[idx] = f"find = {toml_literal(nf)}\n"
        elif line.startswith("replace = "):
            lines[idx] = f"replace = {toml_literal(nr)}\n"

    body = "".join(lines)
    check = {p["id"]: p for p in tomllib.loads(body)["patch"]}
    for pid, (nf, nr) in cands.items():
        assert check[pid]["find"] == nf and check[pid]["replace"] == nr, pid

    Path("patches.toml").write_text(body, encoding="utf-8", newline="")
    print(f"wrote {len(cands)} patches")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
