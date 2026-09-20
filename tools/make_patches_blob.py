"""Mã hóa patches.toml thành assets/patches.enc bằng AES-256-GCM.

Chạy trước khi build Nuitka exe để nhúng patches dưới dạng ciphertext.
Key cố định trùng với engine._BLOB_KEY.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import engine


def encrypt_toml(src: Path, dest: Path, key: bytes = engine._BLOB_KEY) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    raw = src.read_bytes()
    nonce = os.urandom(12)
    blob = nonce + AESGCM(key).encrypt(nonce, raw, None)
    dest.write_bytes(blob)
    return len(blob)


def main() -> int:
    ap = argparse.ArgumentParser(description="Encrypt patches.toml to patches.enc")
    ap.add_argument("--toml", default=str(ROOT / "patches.toml"), help="Source TOML file")
    ap.add_argument("--out", default=str(ROOT / "assets" / "patches.enc"), help="Destination .enc file")
    args = ap.parse_args()

    src = Path(args.toml)
    dest = Path(args.out)
    if not src.is_file():
        sys.stderr.write(f"Error: {src} not found\n")
        return 1
    size = encrypt_toml(src, dest)
    print(f"Encrypted {src.name} ({src.stat().st_size} bytes) -> {dest} ({size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
