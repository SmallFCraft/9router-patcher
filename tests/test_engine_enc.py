"""Tests for AES-GCM encrypted patches blob loading and generation."""
import os
import subprocess
import sys
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import pytest

import engine


def test_blob_roundtrip_decrypts_to_valid_patches(tmp_path, monkeypatch):
    """Tool-created blob is decrypted in RAM and produces identical Patch objects."""
    key = engine._BLOB_KEY
    nonce = os.urandom(12)
    fake_toml = (
        '[[patch]]\n'
        'id = "test-enc-patch"\n'
        'order = 1\n'
        'summary = "encrypted test"\n'
        'why = "unit test"\n'
        'find = "FIND_ME"\n'
        'replace = "REPLACED"\n'
    ).encode("utf-8")
    ciphertext = AESGCM(key).encrypt(nonce, fake_toml, None)
    enc_file = tmp_path / "patches.enc"
    enc_file.write_bytes(nonce + ciphertext)

    monkeypatch.setattr(engine, "PATCHES_ENC_FILE", enc_file)
    patches = engine.load_patches()
    assert len(patches) == 1
    assert patches[0].id == "test-enc-patch"
    assert patches[0].find == "FIND_ME"
    assert patches[0].replace == "REPLACED"


def test_load_patches_explicit_path_overrides_enc(tmp_path, monkeypatch):
    """Passing path explicitly must read that file as plaintext TOML, ignoring .enc."""
    enc_file = tmp_path / "patches.enc"
    enc_file.write_bytes(b"corrupt-data-not-a-valid-blob")
    monkeypatch.setattr(engine, "PATCHES_ENC_FILE", enc_file)

    plain = tmp_path / "custom.toml"
    plain.write_text(
        '[[patch]]\n'
        'id = "plain-patch"\n'
        'order = 1\n'
        'summary = "s"\nwhy = "w"\nfind = "f"\nreplace = "r"\n',
        encoding="utf-8",
    )
    patches = engine.load_patches(plain)
    assert len(patches) == 1
    assert patches[0].id == "plain-patch"


def test_load_patches_falls_back_to_toml_when_no_enc(tmp_path, monkeypatch):
    """When patches.enc does not exist, load_patches reads patches.toml normally."""
    missing = tmp_path / "nonexistent.enc"
    monkeypatch.setattr(engine, "PATCHES_ENC_FILE", missing)
    patches = engine.load_patches()
    assert len(patches) == 34
    assert patches[0].id == "connect-timeout-180s"


def test_make_patches_blob_cli(tmp_path):
    """tools/make_patches_blob.py CLI encrypts source toml to output file."""
    out = tmp_path / "out.enc"
    cmd = [sys.executable, "tools/make_patches_blob.py", "--out", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert out.is_file()
    assert out.stat().st_size > 40000
    blob = out.read_bytes()
    nonce, ct = blob[:12], blob[12:]
    plain = AESGCM(engine._BLOB_KEY).decrypt(nonce, ct, None).decode("utf-8")
    assert "connect-timeout-180s" in plain
