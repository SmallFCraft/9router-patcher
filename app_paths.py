"""Centralized path resolution for 9router Patch Manager.

Handles differences between running from source (.py) and running as a Nuitka
onefile compiled executable (.exe). Read-only data files resolve from bundle dir
(temp dir in onefile); writable state resolves to %APPDATA%/9router-patch/ in frozen mode.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_BUNDLE_DIR = Path(__file__).resolve().parent


def is_frozen() -> bool:
    """True when running inside Nuitka compiled binary."""
    # Nuitka injects __compiled__ into module globals; sys.frozen is None
    return "__compiled__" in globals()


def get_bundle_dir() -> Path:
    """Directory containing code and embedded read-only assets (templates, patches.enc)."""
    return _BUNDLE_DIR


def get_app_data_dir() -> Path:
    """User-writable state directory in %APPDATA%."""
    base = Path(os.environ.get("APPDATA") or Path.home())
    app_data = base / "9router-patch"
    app_data.mkdir(parents=True, exist_ok=True)
    return app_data


def get_log_dir() -> Path:
    """Directory for restart logs and history."""
    if is_frozen():
        d = get_app_data_dir() / "logs"
    else:
        d = get_bundle_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_history_file() -> Path:
    return get_log_dir() / "update-history.jsonl"


def get_stack_state_file() -> Path:
    return get_log_dir() / "router-stack.json"


def get_backup_root() -> Path:
    """Backups directory. Sits outside the repo / outside the exe directory."""
    if is_frozen():
        # sys.argv[0] is the true path to 9router-patch.exe on disk
        exe_dir = Path(sys.argv[0]).resolve().parent
        return exe_dir.parent / "9router-backups"
    return get_bundle_dir().parent / "9router-backups"
