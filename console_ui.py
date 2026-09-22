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
    "dot": "·", "check": "✓", "cross": "✗", "caret": "›",
    "info": "·", "warn": "!", "skip": "○",
}
_ASCII_GLYPHS = {
    "tl": "+", "tr": "+", "bl": "+", "br": "+", "lt": "+", "rt": "+",
    "h": "-", "v": "|", "dot": ".", "check": "v", "cross": "x", "caret": ">",
    "info": "-", "warn": "!", "skip": "o",
}


def glyphs() -> dict[str, str]:
    """Bộ khung vẽ: Unicode nếu encoding console chịu được, không thì ASCII."""
    enc = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        "".join(_UNICODE_GLYPHS.values()).encode(enc)
        return _UNICODE_GLYPHS
    except (UnicodeEncodeError, LookupError):
        return _ASCII_GLYPHS


def unicode_ok() -> bool:
    return glyphs() is _UNICODE_GLYPHS


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
    """Chấm dẫn + badge trạng thái, đóng dòng bước đang mở.

    Dòng step luôn đúng `width()`: note dài thì chấm thu về 2 và note cắt gọn,
    không bao giờ tràn qua mép box (đo 2026-09-23: badge+note từng tràn +95 cột)."""
    p, g = palette(), glyphs()
    color = {"ok": p.green, "warn": p.yellow, "bad": p.red}.get(kind, p.cyan)
    head_room = _STEP_HEAD + 1 + 2 + len(status)
    avail = width() - head_room
    budget = max(0, avail - 2)          # chỗ cho dot + note
    if note:
        max_note = budget - 2
        if max_note < 12:
            note, dots = "", g["dot"] * max(0, budget)
        else:
            short = note if len(note) <= max_note else note[:max_note - 1] + "…"
            dots = g["dot"] * max(2, budget - len(short) - 1)
            note = short
    else:
        dots = g["dot"] * max(0, min(avail, width() - _STEP_HEAD - len(status)))
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
    """Prompt idle của app quản lý (boot_doctor và app.py dùng chung một chỗ duy nhất).

    Chữ `patch` + `›`: phân biệt với CLI 9router thật (ai cũng biết npm package tên
    gì), giữ style caret xám của cả console."""
    p, g = palette(), glyphs()
    return f"  {p.cyan}patch{p.reset} {p.gray}{g['caret']}{p.reset} "


def status_line(text: str) -> None:
    """Một dòng trạng thái ngay trên prompt idle.

    Cắt theo `width()`: console nghỉ vẫn phải gọn trong khung, không đẩy
    prompt xuống dòng khi cửa sổ hẹp (đo 2026-09-23: dòng 102 ký tự tràn)."""
    p = palette()
    limit = width() - 4
    if len(text) > limit:
        text = text[:limit - 1].rstrip() + "…"
    print(f"  {p.gray}{text}{p.reset}", flush=True)


def detail(icon: str, text: str) -> None:
    """Dòng phụ dưới một step: canh lề với khung, màu icon theo ngữ nghĩa."""
    p, g = palette(), glyphs()
    color = {g["cross"]: p.red, g["check"]: p.green,
             g["info"]: p.gray, g["warn"]: p.yellow, g["skip"]: p.gray}.get(icon.strip(), p.gray)
    print(f"  {p.gray}{g['v']}{p.reset} {color}{icon}{p.reset} {text}", flush=True)


def prompt(icon: str, question: str) -> str:
    """Hỏi trong khung step: lồng dưới rail `│`, trả raw input. EOF → ""."""
    p, g = palette(), glyphs()
    try:
        return input(f"  {p.gray}{g['v']}{p.reset} {p.cyan}{icon}{p.reset} {question}").strip()
    except EOFError:
        return ""


def npm_run(target: str, installer, log_fn=None, label: str = "npm install -g") -> tuple[bool, str]:
    """Chạy npm cài 9router@target: spinner 1 dòng + kết quả gọn trong khung step.

    `installer` là hàm (on_output=None) -> (ok, msg); tách ra để caller
    (boot_doctor) giữ logic stop-stack/retry, console_ui chỉ lo vẽ.
    """
    import sys as _sys
    p, g = palette(), glyphs()
    live = _sys.stdout.isatty() if hasattr(_sys.stdout, "isatty") else False
    frames = (["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"] if unicode_ok()
              else ["-", "\\", "|", "/"])
    print(f"  {p.gray}{g['v']}{p.reset} {p.cyan}{label} 9router@{target}{p.reset}", flush=True)
    i, last = [0], [""]

    def tick(line: str) -> None:
        if not live:
            log = line if line == last[0] else line  # pipe: log thật, không spinner
            last[0] = line
            print(f"  {p.gray}{g['v']}{p.reset} {p.gray}{log[:100]}{p.reset}", flush=True)
            return
        i[0] += 1
        short = line[:66] + ("…" if len(line) > 66 else "")
        frame = (f"\r  {p.gray}{g['v']}{p.reset} "
                 f"{p.cyan}{frames[i[0] % len(frames)]}{p.reset} {short}")
        _sys.stdout.write(frame + " " * max(0, len(last[0]) - len(frame)))
        last[0] = frame
        _sys.stdout.flush()

    ok, msg = installer(on_output=tick)
    if live:
        _sys.stdout.write("\r" + " " * (width() - 2) + "\r")
    if not ok:
        if log_fn:
            log_fn(f"ERROR: {msg}")
        print(f"  {p.gray}{g['v']}{p.reset} {p.red}✗{p.reset} {msg}", flush=True)
        print(f"  {p.gray}{g['v']}{p.reset} {p.gray}Mở Dashboard → trang Update để cài lại thủ công.{p.reset}",
              flush=True)
    else:
        print(f"  {p.gray}{g['v']}{p.reset} {p.green}✓{p.reset} 9router@{target} đã sẵn sàng",
              flush=True)
    return ok, msg