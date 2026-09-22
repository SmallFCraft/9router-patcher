"""Self-update engine for 9router Patch Manager standalone executable."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import app_paths


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


def get_settings_file() -> Path:
    """Đường dẫn file settings.json trong %APPDATA%."""
    return app_paths.get_app_data_dir() / "settings.json"


def is_enabled() -> bool:
    """Đọc cấu hình auto_update từ settings.json, mặc định là True."""
    sf = get_settings_file()
    if not sf.is_file():
        return True
    try:
        data = json.loads(sf.read_text(encoding="utf-8"))
        return bool(data.get("auto_update", True))
    except Exception:
        return True


def set_enabled(on: bool) -> None:
    """Lưu cấu hình auto_update nguyên tử bằng file tạm + replace."""
    sf = get_settings_file()
    sf.parent.mkdir(parents=True, exist_ok=True)
    tmp = sf.with_suffix(".tmp")
    payload = {"auto_update": bool(on)}
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, sf)

