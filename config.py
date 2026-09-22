"""Centralized configuration for 9router Patch Manager self-update."""
from __future__ import annotations

UPDATE_BASE_URL = "https://phmyhu1710.dev/update/9router-patcher"
VERSION_CHECK_URL = f"{UPDATE_BASE_URL}/version.json"
AUTO_UPDATE_INTERVAL_SECONDS = 300  # 5 phút
TARGET_9ROUTER_VERSION = "0.5.85"
