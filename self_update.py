"""Self-update engine for 9router Patch Manager standalone executable."""
from __future__ import annotations

import re


def parse_version(v: str) -> tuple[int, ...]:
    """Chuyển chuỗi version thành tuple 4 số nguyên để so sánh chính xác."""
    cleaned = re.sub(r"^[^\d]*", "", (v or "").strip())
    parts = []
    for chunk in cleaned.split("."):
        m = re.match(r"^\d+", chunk)
        if m:
            parts.append(int(m.group(0)))
        else:
            break
    if not parts:
        return (0, 0, 0, 0)
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])


def is_newer(remote: str, local: str) -> bool:
    """True nếu phiên bản remote lớn hơn local."""
    return parse_version(remote) > parse_version(local)
