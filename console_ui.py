"""Console presentation: ANSI colors, box drawing, step printer.

Không thêm dependency: `rich` nằm trong `--nofollow-import-to` của build_app.py (đã bị
loại khỏi exe), nên tự vẽ ANSI rẻ hơn nhiều so với kéo nó trở lại build.
Mọi thứ tự tắt khi stdout không phải TTY hoặc có NO_COLOR — log pipe/CI/test giữ
nguyên text thuần, không lẫn escape code.
"""
from __future__ import annotations

import ctypes
import os
import re
import shutil
import sys

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")

_VT: bool | None = None
_STEP_HEAD = 0          # độ dài hiển thị của dòng bước đang in dở


def _probe_vt() -> bool:
    """Bật VT processing cho console Windows; non-Windows coi như có sẵn."""
    if sys.platform != "win32":
        return True
    try:
        k32 = ctypes.windll.kernel32
        handle = k32.GetStdHandle(-11)                  # STD_OUTPUT_HANDLE
        mode = ctypes.c_ulong()
        if handle in (0, -1, None) or not k32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(k32.SetConsoleMode(handle, mode.value | 0x0004))    # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        return False


def enable_vt() -> bool:
    global _VT
    if _VT is None:
        _VT = _probe_vt()
    return _VT


def color_enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    try:
        if not sys.stdout.isatty():
            return False
    except Exception:
        return False
    return enable_vt()


class Palette:
    """Mã màu rỗng khi tắt màu — caller nối chuỗi thẳng, không cần rẽ nhánh."""

    def __init__(self, on: bool) -> None:
        for name, code in (
            ("reset", "\033[0m"), ("bold", "\033[1m"), ("dim", "\033[2m"),
            ("cyan", "\033[96m"), ("green", "\033[92m"), ("yellow", "\033[93m"),
            ("red", "\033[91m"), ("gray", "\033[90m"), ("white", "\033[97m"),
        ):
            setattr(self, name, code if on else "")


_PALETTES: dict[bool, Palette] = {}


def palette() -> Palette:
    """Tính lại mỗi lần gọi: test đổi sys.stdout (capsys) sau khi module import."""
    on = color_enabled()
    if on not in _PALETTES:
        _PALETTES[on] = Palette(on)
    return _PALETTES[on]


_UNICODE_GLYPHS = {
    "tl": "╭", "tr": "╮", "bl": "╰", "br": "╯",
    "lt": "├", "rt": "┤", "h": "─", "v": "│",
    "dot": "·", "check": "✓", "caret": "›",
}
_ASCII_GLYPHS = {
    "tl": "+", "tr": "+", "bl": "+", "br": "+", "lt": "+", "rt": "+",
    "h": "-", "v": "|", "dot": ".", "check": "v", "caret": ">",
}


def glyphs() -> dict[str, str]:
    """Bộ khung vẽ: Unicode nếu encoding console chịu được, không thì ASCII."""
    enc = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        "".join(_UNICODE_GLYPHS.values()).encode(enc)
        return _UNICODE_GLYPHS
    except (UnicodeEncodeError, LookupError):
        return _ASCII_GLYPHS


def width() -> int:
    try:
        cols = shutil.get_terminal_size((80, 24)).columns
    except Exception:
        cols = 80
    return max(56, min(cols - 4, 78))


def _vis(text: str) -> int:
    return len(_ANSI_RE.sub("", text))


def header(title: str, version: str = "") -> None:
    """Khối tiêu đề bo góc, tên app + version."""
    p, g, w = palette(), glyphs(), width()
    inner = w - 2
    label = f"  {p.bold}{p.white}{title}{p.reset}" + (f"  {p.gray}{version}{p.reset}" if version else "")
    print()
    print(f"  {p.cyan}{g['tl']}{g['h'] * inner}{g['tr']}{p.reset}")
    print(f"  {p.cyan}{g['v']}{p.reset}{label}{' ' * max(1, inner - _vis(label))}{p.cyan}{g['v']}{p.reset}")
    print(f"  {p.cyan}{g['bl']}{g['h'] * inner}{g['br']}{p.reset}")
    print(flush=True)


def step_begin(label: str, text: str) -> None:
    """In nhãn bước NGAY trước khi chạy việc nặng (handle64 có thể mất ~40s)."""
    global _STEP_HEAD
    p = palette()
    _STEP_HEAD = len(label) + len(text) + 4
    print(f"  {p.gray}{label}{p.reset}  {p.white}{text}{p.reset}", end="", flush=True)


def step_end(status: str, kind: str = "info", note: str = "") -> None:
    """Chấm dẫn + badge trạng thái, đóng dòng bước đang mở."""
    p, g = palette(), glyphs()
    color = {"ok": p.green, "warn": p.yellow, "bad": p.red}.get(kind, p.cyan)
    dots = g["dot"] * max(2, width() - _STEP_HEAD - len(status))
    tail = f"{color}{status}{p.reset}" + (f" {p.gray}{note}{p.reset}" if note else "")
    print(f" {p.gray}{dots}{p.reset}  {tail}", flush=True)


def success(text: str, note: str = "") -> None:
    p, g = palette(), glyphs()
    print(f"\n  {p.green}{g['check']}{p.reset} {p.bold}{text}{p.reset}"
          + (f" {p.gray}{note}{p.reset}" if note else ""))
    print(flush=True)


def panel(rows: list[tuple[str, str]], keys: list[tuple[str, str]] | None = None) -> None:
    """Hộp thông tin: các dòng nhãn/giá trị, tuỳ chọn khối phím tắt ngăn bằng gạch ngang."""
    p, g, w = palette(), glyphs(), width()
    inner = w - 2
    print(f"  {p.gray}{g['tl']}{g['h'] * inner}{g['tr']}{p.reset}")
    for label, value in rows:
        line = f"  {p.bold}{label}{p.reset}  {p.cyan}{value}{p.reset}"
        print(f"  {p.gray}{g['v']}{p.reset}{line}{' ' * max(1, inner - _vis(line))}{p.gray}{g['v']}{p.reset}")
    if keys:
        print(f"  {p.gray}{g['lt']}{g['h'] * inner}{g['rt']}{p.reset}")
        line = "   ".join(f"{p.yellow}[{key}]{p.reset}{p.gray} {desc}{p.reset}" for key, desc in keys)
        print(f"  {p.gray}{g['v']}{p.reset}  {line}{' ' * max(1, inner - _vis(line) - 2)}{p.gray}{g['v']}{p.reset}")
    print(f"  {p.gray}{g['bl']}{g['h'] * inner}{g['br']}{p.reset}", flush=True)


def prompt_label() -> str:
    p, g = palette(), glyphs()
    return f"  {p.cyan}9router{p.reset} {p.gray}{g['caret']}{p.reset} "