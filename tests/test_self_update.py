"""Tests for self-update engine and version comparison."""
from __future__ import annotations

import pytest


def test_parse_version_normalizes_and_pads():
    import self_update
    assert self_update.parse_version("2.0.1") == (2, 0, 1, 0)
    assert self_update.parse_version("2.0") == (2, 0, 0, 0)
    assert self_update.parse_version("v2.0.1.4") == (2, 0, 1, 4)
    assert self_update.parse_version("invalid") == (0, 0, 0, 0)


def test_is_newer_compares_semver():
    import self_update
    assert self_update.is_newer("2.0.1", "2.0.0") is True
    assert self_update.is_newer("2.1.0", "2.0.9") is True
    assert self_update.is_newer("2.0.0", "2.0.0") is False
    assert self_update.is_newer("1.9.9", "2.0.0") is False
    assert self_update.is_newer("junk", "2.0.0") is False
