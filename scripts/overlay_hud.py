"""Native Win32 System Cursor Takeover, Dedicated Pitch-Black Top Pill & Screen-Border Glow HUD.

Architecture (v3.1.0):
  1. True OS Hardware Cursor Takeover (`SetSystemCursor` + `CreateIconIndirect`):
     - Replaces `OCR_NORMAL` (32512), `OCR_HAND` (32649), `OCR_IBEAM` (32513),
       `OCR_WAIT` (32514), and `OCR_APPSTARTING` (32650) with crisp 32-bit ARGB
       Antigravity cursors while active — ZERO overlapping double cursors, zero
       window-follow lag (composited on the GPU hardware cursor plane at native
       144Hz/240Hz refresh), and automatically omitted from screenshots.
     - Fully isolated in `try/except` so cursor creation can NEVER crash or hide
       the safety HUD windows, with single-shot `SPI_SETCURSORS` restoration on exit.
  2. `hwnd_pill` (dedicated DPI-aware top-center `WS_EX_LAYERED | WS_EX_TOPMOST` surface):
     - Pure pitch-black (`#08090c` -> `#000000`) capsule pill rendered at solid
       `source_alpha=255` (never dimmed by border breathing) displaying the
       4-color Antigravity 'A' mark + a 3-stage caption (`Active/Stage` ->
       `Thinking...` -> `Waiting for model...`) with a constant 2.15s shimmer
       sweep and `Esc` cancel badge.
  3. `hwnd_border` (full-screen click-through `WDA_EXCLUDEFROMCAPTURE` surface):
     - Lush frosty screen-border glow breathing smoothly via cached GDI DIBSection
       (`<0.1ms` per frame, zero per-frame 8.3 MB allocations).
  4. `hwnd_ripple` (`96x96` DPI-scaled click-through `WDA_EXCLUDEFROMCAPTURE` surface):
     - Stays hidden (`SW_HIDE`) when idle and displays concentric click rings (`0.38s`)
       on click events.
  5. Session-Grant Persistence & Cancel:
     - Remains visible across multi-minute LLM reasoning turns and file downloads
       as long as `session_state.json` holds active grants and the MCP server
       `parentPid` is alive, shutting down cleanly on `active=False` or physical
       `Esc` cancel.
"""

from __future__ import annotations

import atexit
import ctypes
from ctypes import wintypes
import json
import math
import os
from pathlib import Path
import re
import signal
import sys
import threading
import time
import traceback
import uuid

try:
    _u32_dpi = ctypes.WinDLL("user32", use_last_error=True)
    _u32_dpi.SetProcessDpiAwarenessContext.argtypes = [wintypes.HANDLE]
    _u32_dpi.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
    if not _u32_dpi.SetProcessDpiAwarenessContext(wintypes.HANDLE(-4)):
        raise OSError("SetProcessDpiAwarenessContext failed")
except Exception:
    try:
        _sh_dpi = ctypes.WinDLL("shcore", use_last_error=True)
        _sh_dpi.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
        _sh_dpi.SetProcessDpiAwareness.restype = ctypes.c_long
        _sh_dpi.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.WinDLL("user32", use_last_error=True).SetProcessDPIAware()
        except Exception:
            pass

_pylibs_env = os.environ.get("CUA_PYLIBS_DIR", "").strip()
if _pylibs_env and _pylibs_env not in sys.path:
    sys.path.insert(0, _pylibs_env)

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from antigravity_brand import load_antigravity_mark, load_dart_cursor_mask_192  # noqa: E402

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000

ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
WDA_EXCLUDEFROMCAPTURE = 0x00000011
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
HWND_TOPMOST = wintypes.HWND(-1)
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
PM_REMOVE = 0x0001
WM_DESTROY = 0x0002
WM_QUIT = 0x0012
WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
VK_ESCAPE = 0x1B
DIB_RGB_COLORS = 0
BI_RGB = 0
ERROR_INVALID_PARAMETER = 87
ERROR_ALREADY_EXISTS = 183
SYNCHRONIZE = 0x00100000

SPI_SETCURSORS = 0x0057
OCR_NORMAL = 32512
OCR_IBEAM = 32513
OCR_WAIT = 32514
OCR_HAND = 32649
OCR_APPSTARTING = 32650

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
PHANDLER_ROUTINE = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", wintypes.DWORD),
    ]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("dwTime", wintypes.DWORD),
    ]


MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(RECT), wintypes.LPARAM
)


class SIZE(ctypes.Structure):
    _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", wintypes.BOOL),
        ("xHotspot", wintypes.DWORD),
        ("yHotspot", wintypes.DWORD),
        ("hbmMask", wintypes.HBITMAP),
        ("hbmColor", wintypes.HBITMAP),
    ]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", POINT),
    ]


# Explicit 64-bit ctypes signatures
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.RegisterClassExW.restype = wintypes.ATOM
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.restype = wintypes.BOOL
user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.SetWindowPos.restype = wintypes.BOOL
user32.UpdateLayeredWindow.argtypes = [
    wintypes.HWND, wintypes.HDC, ctypes.POINTER(POINT), ctypes.POINTER(SIZE),
    wintypes.HDC, ctypes.POINTER(POINT), wintypes.COLORREF,
    ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD,
]
user32.UpdateLayeredWindow.restype = wintypes.BOOL
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.ReleaseDC.restype = ctypes.c_int
user32.PeekMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
user32.PeekMessageW.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
user32.TranslateMessage.restype = wintypes.BOOL
user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = wintypes.SHORT
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.CreateIconIndirect.argtypes = [ctypes.POINTER(ICONINFO)]
user32.CreateIconIndirect.restype = wintypes.HICON
user32.CopyIcon.argtypes = [wintypes.HICON]
user32.CopyIcon.restype = wintypes.HICON
user32.SetSystemCursor.argtypes = [wintypes.HICON, wintypes.DWORD]
user32.SetSystemCursor.restype = wintypes.BOOL
user32.DestroyCursor.argtypes = [wintypes.HICON]
user32.DestroyCursor.restype = wintypes.BOOL
user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT]
user32.SystemParametersInfoW.restype = wintypes.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
user32.GetLastInputInfo.restype = wintypes.BOOL
user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.POINTER(RECT), MONITORENUMPROC, wintypes.LPARAM]
user32.EnumDisplayMonitors.restype = wintypes.BOOL
user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]
user32.GetMonitorInfoW.restype = wintypes.BOOL
try:
    user32.GetDpiForSystem.argtypes = []
    user32.GetDpiForSystem.restype = wintypes.UINT
except Exception:
    pass
try:
    shcore = ctypes.WinDLL("shcore", use_last_error=True)
    shcore.GetDpiForMonitor.argtypes = [
        wintypes.HMONITOR, ctypes.c_int, ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT)
    ]
    shcore.GetDpiForMonitor.restype = ctypes.c_long
except Exception:
    shcore = None

kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.SetConsoleCtrlHandler.argtypes = [PHANDLER_ROUTINE, wintypes.BOOL]
kernel32.SetConsoleCtrlHandler.restype = wintypes.BOOL

gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD,
]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.UINT, wintypes.UINT, ctypes.c_void_p]
gdi32.CreateBitmap.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.restype = wintypes.BOOL

def _is_dir_writable(path: Path) -> bool:
    probe = path / f".cua_write_probe_{os.getpid()}"
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe.write_bytes(b"")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def _resolve_runtime_dir(default_dir: Path) -> Path:
    candidates: list[Path] = []
    for var in ("CUA_RUNTIME_DIR", "PLUGIN_DATA"):
        val = os.environ.get(var, "").strip().strip('"')
        if val and not val.startswith("${"):
            candidates.append(Path(val))
    candidates.append(default_dir)
    local_app = os.environ.get("LOCALAPPDATA", "").strip().strip('"')
    if local_app:
        candidates.append(Path(local_app) / "computer-use-win32")
    temp_dir = (os.environ.get("TEMP") or os.environ.get("TMP") or "").strip().strip('"')
    if temp_dir:
        candidates.append(Path(temp_dir) / "computer-use-win32")
    for cand in candidates:
        if _is_dir_writable(cand):
            return cand
    return default_dir


RUNTIME_DIR = _resolve_runtime_dir(SCRIPT_DIR.parent / ".runtime")
OVERLAY_STATE_FILE = RUNTIME_DIR / "overlay_state.json"
SESSION_STATE_FILE = RUNTIME_DIR / "session_state.json"
OVERLAY_PID_FILE = RUNTIME_DIR / "overlay_pid.json"
OVERLAY_ERROR_LOG = RUNTIME_DIR / "overlay_error.log"

ARROW_PTS = [
    (2.5, 2.5),
    (2.5, 21.5),
    (7.1, 17.0),
    (10.3, 24.0),
    (12.9, 22.8),
    (9.7, 15.8),
    (15.8, 15.4),
]

_CURSORS_OVERRIDDEN = False
_MASTER_CURSORS: dict[str, int] = {}


def get_system_dpi_scale() -> float:
    try:
        dpi = int(user32.GetDpiForSystem() or 96)
        return max(1.0, min(float(dpi) / 96.0, 3.0))
    except Exception:
        return 1.0


def get_cursor_pos() -> tuple[int, int]:
    pt = POINT(0, 0)
    user32.GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)


def get_last_input_tick() -> int:
    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if user32.GetLastInputInfo(ctypes.byref(lii)):
        return int(lii.dwTime)
    return 0


def get_monitors() -> list[tuple[int, int, int, int, float]]:
    mons: list[tuple[int, int, int, int, float]] = []

    def _cb(hmon: int, _hdc: int, _rect: object, _data: int) -> bool:
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(wintypes.HMONITOR(hmon), ctypes.byref(mi)):
            r = mi.rcMonitor
            w_m = max(1, int(r.right - r.left))
            h_m = max(1, int(r.bottom - r.top))
            sc = get_system_dpi_scale()
            if shcore is not None:
                try:
                    dx, dy = wintypes.UINT(96), wintypes.UINT(96)
                    if shcore.GetDpiForMonitor(wintypes.HMONITOR(hmon), 0, ctypes.byref(dx), ctypes.byref(dy)) == 0 and dx.value > 0:
                        sc = max(1.0, min(float(dx.value) / 96.0, 3.0))
                except Exception:
                    pass
            mons.append((int(r.left), int(r.top), w_m, h_m, sc))
        return True

    cb_ref = MONITORENUMPROC(_cb)
    try:
        user32.EnumDisplayMonitors(None, None, cb_ref, 0)
    except Exception:
        pass
    if not mons:
        w = int(user32.GetSystemMetrics(0) or 1920)
        h = int(user32.GetSystemMetrics(1) or 1080)
        mons.append((0, 0, w, h, get_system_dpi_scale()))
    return mons


def get_monitor_for_point(x: int, y: int) -> tuple[int, int, int, int, float]:
    mons = get_monitors()
    for m in mons:
        if m[0] <= x < m[0] + m[2] and m[1] <= y < m[1] + m[3]:
            return m
    return mons[0]


_MAX_ERROR_LOG_BYTES = 262144
_TRIM_ERROR_LOG_BYTES = 131072


def _append_error_log(context: str) -> None:
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        if OVERLAY_ERROR_LOG.is_file() and OVERLAY_ERROR_LOG.stat().st_size > _MAX_ERROR_LOG_BYTES:
            tail = OVERLAY_ERROR_LOG.read_bytes()[-_TRIM_ERROR_LOG_BYTES:]
            nl = tail.find(b"\n")
            if nl >= 0 and nl + 1 < len(tail):
                tail = tail[nl + 1 :]
            OVERLAY_ERROR_LOG.write_bytes(tail)
        with OVERLAY_ERROR_LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] {context}:\n{traceback.format_exc()}\n")
    except Exception:
        pass


def restore_system_cursors() -> bool:
    """Reload the user's original Windows cursor scheme once when overridden."""
    global _CURSORS_OVERRIDDEN
    if not _CURSORS_OVERRIDDEN and not _MASTER_CURSORS:
        return True
    ok = False
    try:
        ok = bool(user32.SystemParametersInfoW(SPI_SETCURSORS, 0, None, 0))
    except Exception:
        pass
    _CURSORS_OVERRIDDEN = False
    for k, hcur in list(_MASTER_CURSORS.items()):
        if hcur:
            try:
                user32.DestroyCursor(wintypes.HICON(hcur))
            except Exception:
                pass
        _MASTER_CURSORS.pop(k, None)
    return ok


def _ctrl_handler(_ctrl_type: int) -> bool:
    restore_system_cursors()
    return False


_CTRL_HANDLER_REF = PHANDLER_ROUTINE(_ctrl_handler)


def _register_exit_guards() -> None:
    atexit.register(restore_system_cursors)
    try:
        kernel32.SetConsoleCtrlHandler(_CTRL_HANDLER_REF, True)
    except Exception:
        pass
    for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, lambda _s, _f: (restore_system_cursors(), sys.exit(0)))
            except Exception:
                pass


def load_font(size: int, semibold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    fonts_dir = Path(os.environ.get("WINDIR") or r"C:\Windows") / "Fonts"
    candidates = (
        [str(fonts_dir / "seguisb.ttf"), str(fonts_dir / "segoeuib.ttf")]
        if semibold
        else [str(fonts_dir / "segoeui.ttf"), str(fonts_dir / "arial.ttf")]
    )
    for path in candidates:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()


def pil_to_hcursor(img: Image.Image, hot_x: int, hot_y: int) -> int:
    """Convert straight RGBA image to a crisp 32-bit ARGB Win32 HCURSOR."""
    w, h = img.size
    arr = np.asarray(img.convert("RGBA"), dtype=np.uint8)
    bgra = np.empty((h, w, 4), dtype=np.uint8)
    bgra[..., 0] = arr[..., 2]
    bgra[..., 1] = arr[..., 1]
    bgra[..., 2] = arr[..., 0]
    bgra[..., 3] = arr[..., 3]

    hdc = user32.GetDC(None)
    hbmp_color = None
    hbmp_mask = None
    try:
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h  # top-down 32bpp ARGB DIB
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        bits = ctypes.c_void_p()
        hbmp_color = gdi32.CreateDIBSection(hdc, ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(bits), None, 0)
        if not hbmp_color or not bits.value:
            return 0
        ctypes.memmove(bits.value, bgra.tobytes(), w * h * 4)

        row_bytes = ((w + 15) // 16) * 2
        mask_buf = ctypes.create_string_buffer(row_bytes * h)
        hbmp_mask = gdi32.CreateBitmap(w, h, 1, 1, mask_buf)
        if not hbmp_mask:
            return 0

        ii = ICONINFO(False, max(0, int(hot_x)), max(0, int(hot_y)), hbmp_mask, hbmp_color)
        hcur = user32.CreateIconIndirect(ctypes.byref(ii))
        return int(hcur or 0)
    finally:
        if hbmp_color:
            gdi32.DeleteObject(hbmp_color)
        if hbmp_mask:
            gdi32.DeleteObject(hbmp_mask)
        if hdc:
            user32.ReleaseDC(None, hdc)


def render_cursor_pointer(size: int = 32, style: str = "sleek_dart", hover_accent: bool = False) -> tuple[Image.Image, int, int]:
    """Render supersampled Antigravity OS cursor image + exact hotspot (hx, hy)."""
    sc = size / 32.0
    if style in ("sleek_dart", "default", ""):
        m = load_dart_cursor_mask_192()
        if size != 32:
            m = m.resize((size * 6, size * 6), Image.Resampling.LANCZOS)
        big = m.size[0]
        canvas = Image.new("RGBA", (big, big), (0, 0, 0, 0))

        # 1. Soft drop shadow
        sh = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        sh_alpha = m.point(lambda p: int(p * 0.44)).filter(ImageFilter.GaussianBlur(radius=1.5 * 6 * sc))
        sh.putalpha(sh_alpha)
        canvas.alpha_composite(sh, dest=(int(round(0.8 * 6 * sc)), int(round(1.3 * 6 * sc))))

        # 2. Crisp pure-white outer contrast border (subtle ice-blue tint on link hover)
        dil = m.filter(ImageFilter.MaxFilter(max(3, int(round(1.25 * 6 * sc)) * 2 + 1)))
        dil_smooth = dil.filter(ImageFilter.GaussianBlur(radius=0.65 * 6 * sc))
        rim_a = np.clip((np.asarray(dil_smooth, dtype=np.float32) - 45.0) * 1.45, 0.0, 255.0).astype(np.uint8)
        rim_rgb = (224, 244, 255, 255) if hover_accent else (255, 255, 255, 255)
        rim = Image.new("RGBA", (big, big), rim_rgb)
        rim.putalpha(Image.fromarray(rim_a, mode="L"))
        canvas.alpha_composite(rim)

        # 3. Sleek obsidian-black split-tail dart body matching the installed brand mark
        yy = np.linspace(0.0, 1.0, big, dtype=np.float32)[:, None]
        body_arr = np.zeros((big, big, 4), dtype=np.uint8)
        body_arr[..., 0] = (18.0 + 12.0 * yy).astype(np.uint8)
        body_arr[..., 1] = (19.0 + 13.0 * yy).astype(np.uint8)
        body_arr[..., 2] = (23.0 + 15.0 * yy).astype(np.uint8)
        body_arr[..., 3] = np.asarray(m, dtype=np.uint8)
        canvas.alpha_composite(Image.fromarray(body_arr, mode="RGBA"))

        hx = max(1, int(round(3.0 * sc)))
        hy = max(1, int(round(3.0 * sc)))
        return canvas.resize((size, size), Image.Resampling.LANCZOS), hx, hy

    brand_sq = load_antigravity_mark()
    ss = 8
    big = size * ss

    if style == "pure_brand":
        target = int(round(23.0 * sc * ss))
        mark = brand_sq.resize((target, target), Image.Resampling.LANCZOS)
        rot = mark.rotate(26, resample=Image.Resampling.BICUBIC, expand=True)
        alpha = rot.getchannel("A")
        dil_white = alpha.filter(ImageFilter.MaxFilter(max(3, int(round(1.5 * sc * ss)) * 2 + 1)))
        dil_dark = alpha.filter(ImageFilter.MaxFilter(max(5, int(round(2.6 * sc * ss)) * 2 + 1)))
        dark_rim = Image.new("RGBA", rot.size, (10, 12, 18, 235))
        dark_rim.putalpha(dil_dark)
        white_rim = Image.new("RGBA", rot.size, (255, 255, 255, 250))
        white_rim.putalpha(dil_white)
        comp = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        dx, dy = int(round(1.0 * sc * ss)), int(round(1.0 * sc * ss))
        comp.alpha_composite(dark_rim, dest=(dx, dy))
        comp.alpha_composite(white_rim, dest=(dx, dy))
        comp.alpha_composite(rot, dest=(dx, dy))
        return comp.resize((size, size), Image.Resampling.LANCZOS), max(1, int(round(5 * sc))), max(1, int(round(4 * sc)))

    pts = [(x * sc * ss, y * sc * ss) for x, y in ARROW_PTS]
    sh_pts = [((x + 0.8) * sc * ss, (y + 1.3) * sc * ss) for x, y in ARROW_PTS]

    canvas = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    sh = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    ImageDraw.Draw(sh).polygon(sh_pts, fill=(0, 0, 0, 108))
    sh = sh.filter(ImageFilter.GaussianBlur(radius=1.2 * sc * ss))
    canvas.alpha_composite(sh)

    is_frost = style == "frost_badge"
    rim_col = (14, 18, 28, 255) if is_frost else (255, 255, 255, 255)
    rim = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    dr = ImageDraw.Draw(rim)
    dr.polygon(pts, fill=rim_col)
    dr.line(pts + [pts[0]], fill=rim_col, width=int(round(2.6 * sc * ss)), joint="curve")
    canvas.alpha_composite(rim)

    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).polygon(pts, fill=255)
    yy = np.linspace(0.0, 1.0, big, dtype=np.float32)[:, None]
    grad = np.zeros((big, big, 4), dtype=np.uint8)
    if is_frost:
        grad[..., 0] = (255.0 - 16.0 * yy).astype(np.uint8)
        grad[..., 1] = (255.0 - 10.0 * yy).astype(np.uint8)
        grad[..., 2] = 255
    else:
        grad[..., 0] = (12.0 + 16.0 * yy).astype(np.uint8)
        grad[..., 1] = (14.0 + 18.0 * yy).astype(np.uint8)
        grad[..., 2] = (20.0 + 20.0 * yy).astype(np.uint8)
    grad[..., 3] = 255
    body = Image.fromarray(grad, mode="RGBA")
    body.putalpha(mask)
    canvas.alpha_composite(body)

    # Circular obsidian micro-badge with official 4-color Antigravity 'A'
    bx0, by0 = int(round(16.2 * sc * ss)), int(round(16.2 * sc * ss))
    bs = int(round(14.0 * sc * ss))
    rw = int(round(1.15 * sc * ss))
    ring_col = (130, 212, 255, 255) if hover_accent else (255, 255, 255, 252)
    badge = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    bd = ImageDraw.Draw(badge)
    bd.ellipse((bx0 - rw, by0 - rw, bx0 + bs + rw, by0 + bs + rw), fill=ring_col)
    bd.ellipse((bx0, by0, bx0 + bs, by0 + bs), fill=(8, 10, 15, 255))
    canvas.alpha_composite(badge)

    pad = int(round(2.3 * sc * ss))
    a_sz = max(4, bs - pad * 2)
    canvas.alpha_composite(brand_sq.resize((a_sz, a_sz), Image.Resampling.LANCZOS), dest=(bx0 + pad, by0 + pad))
    hx = max(1, int(round(2.5 * sc)))
    hy = max(1, int(round(2.5 * sc)))
    return canvas.resize((size, size), Image.Resampling.LANCZOS), hx, hy


def render_cursor_ibeam(size: int = 32) -> tuple[Image.Image, int, int]:
    """Render dual-contrast text I-beam with Antigravity 'A' micro-badge."""
    brand_sq = load_antigravity_mark()
    ss = 8
    big = size * ss
    sc = size / 32.0
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx = int(round(15 * sc * ss))
    top_y = int(round(5 * sc * ss))
    bot_y = int(round(27 * sc * ss))
    arm = int(round(3.5 * sc * ss))
    for stroke_w, col in [
        (int(round(3.8 * sc * ss)), (255, 255, 255, 245)),
        (int(round(1.8 * sc * ss)), (12, 15, 22, 255)),
    ]:
        d.line([(cx, top_y), (cx, bot_y)], fill=col, width=stroke_w)
        d.line([(cx - arm, top_y), (cx + arm, top_y)], fill=col, width=stroke_w)
        d.line([(cx - arm, bot_y), (cx + arm, bot_y)], fill=col, width=stroke_w)

    bx0, by0 = int(round(19.0 * sc * ss)), int(round(19.0 * sc * ss))
    bs = int(round(11.0 * sc * ss))
    rw = int(round(0.95 * sc * ss))
    badge = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    bd = ImageDraw.Draw(badge)
    bd.ellipse((bx0 - rw, by0 - rw, bx0 + bs + rw, by0 + bs + rw), fill=(255, 255, 255, 248))
    bd.ellipse((bx0, by0, bx0 + bs, by0 + bs), fill=(8, 10, 15, 255))
    img.alpha_composite(badge)
    pad = int(round(1.9 * sc * ss))
    a_sz = max(4, bs - pad * 2)
    img.alpha_composite(brand_sq.resize((a_sz, a_sz), Image.Resampling.LANCZOS), dest=(bx0 + pad, by0 + pad))
    return img.resize((size, size), Image.Resampling.LANCZOS), int(round(15 * sc)), int(round(16 * sc))


def engage_system_cursors(style: str = "sleek_dart") -> bool:
    """Install Antigravity cursors onto the native Windows hardware cursor plane."""
    global _CURSORS_OVERRIDDEN
    cx = max(32, int(user32.GetSystemMetrics(13) or 32))
    cy = max(32, int(user32.GetSystemMetrics(14) or 32))
    sz = min(max(cx, cy), 64)

    norm_img, nhx, nhy = render_cursor_pointer(sz, style=style, hover_accent=False)
    hand_img, hhx, hhy = render_cursor_pointer(sz, style=style, hover_accent=True)
    beam_img, bhx, bhy = render_cursor_ibeam(sz)

    for k, old_h in list(_MASTER_CURSORS.items()):
        if old_h:
            user32.DestroyCursor(wintypes.HICON(old_h))
        _MASTER_CURSORS.pop(k, None)

    _MASTER_CURSORS["normal"] = pil_to_hcursor(norm_img, nhx, nhy)
    _MASTER_CURSORS["hand"] = pil_to_hcursor(hand_img, hhx, hhy)
    _MASTER_CURSORS["ibeam"] = pil_to_hcursor(beam_img, bhx, bhy)

    if not _MASTER_CURSORS["normal"]:
        return False

    mappings = [
        (OCR_NORMAL, _MASTER_CURSORS["normal"]),
        (OCR_WAIT, _MASTER_CURSORS["normal"]),
        (OCR_APPSTARTING, _MASTER_CURSORS["normal"]),
        (OCR_HAND, _MASTER_CURSORS["hand"] or _MASTER_CURSORS["normal"]),
        (OCR_IBEAM, _MASTER_CURSORS["ibeam"] or _MASTER_CURSORS["normal"]),
    ]
    any_ok = False
    for ocr_id, master in mappings:
        if not master:
            continue
        dup = user32.CopyIcon(wintypes.HICON(master))
        if dup and user32.SetSystemCursor(wintypes.HICON(dup), ocr_id):
            any_ok = True
        elif dup:
            user32.DestroyCursor(wintypes.HICON(dup))
    _CURSORS_OVERRIDDEN = any_ok
    return any_ok


def engage_system_cursors_safe(style: str = "sleek_dart") -> bool:
    """Non-fatal wrapper so cursor creation can NEVER crash or hide the HUD windows."""
    try:
        return engage_system_cursors(style)
    except Exception:
        _append_error_log(f"engage_system_cursors({style!r})")
        try:
            if style != "frost_badge":
                return engage_system_cursors("frost_badge")
        except Exception:
            _append_error_log("engage_system_cursors('frost_badge') fallback")
    return False


def build_base_glow_frame(width: int, height: int) -> Image.Image:
    """Build a lush, wide (~135px on 1080p) frosty screen-border glow."""
    glow_px = max(130.0, min(float(width), float(height)) * 0.128)

    x = np.arange(width, dtype=np.float32)[None, :]
    y = np.arange(height, dtype=np.float32)[:, None]
    dx = np.minimum(x, (width - 1.0) - x)
    dy = np.minimum(y, (height - 1.0) - y)

    aura_x = np.clip(1.0 - dx / glow_px, 0.0, 1.0) ** 2.1
    aura_y = np.clip(1.0 - dy / glow_px, 0.0, 1.0) ** 2.1
    aura = 1.0 - (1.0 - aura_x) * (1.0 - aura_y)

    core_px = glow_px * 0.30
    core_x = np.clip(1.0 - dx / core_px, 0.0, 1.0) ** 1.65
    core_y = np.clip(1.0 - dy / core_px, 0.0, 1.0) ** 1.65
    core = 1.0 - (1.0 - core_x) * (1.0 - core_y)

    alpha_f = np.clip(aura * 165.0 + core * 65.0, 0.0, 234.0)

    rgba = np.zeros((height, width, 4), dtype=np.uint8)
    rgba[..., 0] = np.clip(152.0 + 84.0 * core, 0.0, 255.0).astype(np.uint8)
    rgba[..., 1] = np.clip(203.0 + 45.0 * core, 0.0, 255.0).astype(np.uint8)
    rgba[..., 2] = 255
    rgba[..., 3] = alpha_f.astype(np.uint8)

    full = Image.fromarray(rgba, mode="RGBA")
    rim_draw = ImageDraw.Draw(full)
    rim_draw.rectangle((0, 0, width - 1, height - 1), outline=(242, 250, 255, 245), width=2)
    rim_draw.rectangle((2, 2, width - 3, height - 3), outline=(196, 229, 255, 200), width=1)
    return full


def _finite_float(val: object, default: float) -> float:
    if val is None or isinstance(val, bool):
        return default
    try:
        num = float(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return num if math.isfinite(num) else default


HUD_BRAND = (os.environ.get("CUA_HUD_BRAND") or "Antigravity").strip() or "Antigravity"
DEFAULT_PILL_CAPTION = f"{HUD_BRAND} is using your computer"
THINKING_PILL_CAPTION = f"{HUD_BRAND} • Thinking..."
WAITING_PILL_CAPTION = f"{HUD_BRAND} • Waiting for model..."
THINKING_DELAY_SEC = 1.6  # Brief grace window after a tool finishes before showing Thinking...
WAITING_DELAY_SEC = max(10.0, min(_finite_float(os.environ.get("CUA_THINKING_WAIT_SEC"), 90.0), 300.0))
PILL_SHIMMER_FRAMES = 28
PILL_SHIMMER_CYCLE_SEC = 2.15  # Constant calm glimmer sweep regardless of frame count or action speed

_RAW_TELEMETRY_RE = re.compile(
    r"(?:\.(?:exe|dll|ps1|py|cmd|bat)\b|\bhwnd\b|0x[0-9a-f]+|[\\]|"
    r"[a-z]:/|(?:[a-z_][a-z0-9_.-]*/[a-z_][a-z0-9_.-]*)|"
    r"^(?:inspecting|ui tree|focused|invoke|click|set value|set text|typing in|typed|key)\b)",
    re.I,
)
_STAGE_STEP_RE = re.compile(r"^(\d{1,2}/\d{1,2})\s+(.+)$")


def _format_pill_caption(raw_label: str | None) -> str:
    """Return clean human-readable pill caption, blocking process names, HWNDs, and raw telemetry."""
    s = str(raw_label or "").strip()
    if not s or s.lower() in ("active", "idle"):
        return DEFAULT_PILL_CAPTION
    if _RAW_TELEMETRY_RE.search(s):
        return DEFAULT_PILL_CAPTION
    return f"{HUD_BRAND} • {s[:46]}"


def build_black_pill_frames(
    dpi_scale: float = 1.0, num_frames: int = 28, caption: str = DEFAULT_PILL_CAPTION
) -> tuple[list[Image.Image], int]:
    """Pre-render 4x-supersampled smooth typography + 28-frame glimmer capsule (with optional subtle step chip)."""
    s = max(1.0, float(dpi_scale))
    ss = 4  # 4x supersampling + LANCZOS downsampling eliminates all 14px TrueType jaggedness
    brand_sq = load_antigravity_mark()

    txt = caption or DEFAULT_PILL_CAPTION
    key_txt = "Esc"

    # Detect clean stage caption with optional leading N/M step counter (e.g. "Antigravity • 3/5 Opening app")
    brand_prefix = f"{HUD_BRAND} • "
    step_badge: str | None = None
    brand_lead: str | None = None
    main_txt = txt
    if txt.startswith(brand_prefix):
        rest = txt[len(brand_prefix) :].strip()
        m_step = _STAGE_STEP_RE.match(rest)
        if m_step:
            brand_lead = HUD_BRAND
            step_badge = m_step.group(1)
            main_txt = m_step.group(2).strip()

    font_main_hi = load_font(max(48, int(round(14.5 * s * ss))), semibold=True)
    font_key_hi = load_font(max(36, int(round(11.0 * s * ss))), semibold=True)

    dummy = Image.new("L", (32, 32), 0)
    dd = ImageDraw.Draw(dummy)
    tb1_hi = dd.textbbox((0, 0), main_txt, font=font_main_hi)
    tbk_hi = dd.textbbox((0, 0), key_txt, font=font_key_hi)

    tw1 = int(math.ceil((tb1_hi[2] - tb1_hi[0]) / ss))
    twk = int(math.ceil((tbk_hi[2] - tbk_hi[0]) / ss))

    lead_w = 0
    badge_w = 0
    badge_gap = int(round(8 * s))
    tb_lead_hi = (0, 0, 0, 0)
    tb_badge_hi = (0, 0, 0, 0)
    if brand_lead and step_badge:
        tb_lead_hi = dd.textbbox((0, 0), brand_lead, font=font_main_hi)
        tb_badge_hi = dd.textbbox((0, 0), step_badge, font=font_key_hi)
        lead_w = int(math.ceil((tb_lead_hi[2] - tb_lead_hi[0]) / ss))
        badge_w = int(math.ceil((tb_badge_hi[2] - tb_badge_hi[0]) / ss)) + int(round(12 * s))

    icon_sz = int(round(17 * s))
    icon_gap = int(round(10 * s))
    key_pad_x = int(round(7 * s))
    key_w = twk + key_pad_x * 2
    key_h = int(round(20 * s))
    div_gap = int(round(12 * s))
    pad_l = int(round(15 * s))
    pad_r = int(round(13 * s))
    extra_lead = (lead_w + badge_gap + badge_w + badge_gap) if (brand_lead and step_badge) else 0
    pill_w = pad_l + icon_sz + icon_gap + extra_lead + tw1 + div_gap + 1 + div_gap + key_w + pad_r
    pill_h = int(round(38 * s))
    radius = pill_h // 2
    margin = int(round(24 * s))
    cw, ch = pill_w + margin * 2, pill_h + margin * 2

    pw_hi, ph_hi = pill_w * ss, pill_h * ss
    rad_hi = radius * ss

    cap_mask_hi = Image.new("L", (pw_hi, ph_hi), 0)
    ImageDraw.Draw(cap_mask_hi).rounded_rectangle((0, 0, pw_hi - 1, ph_hi - 1), radius=rad_hi, fill=255)
    cap_mask = cap_mask_hi.resize((pill_w, pill_h), Image.Resampling.LANCZOS)

    rim_outer_hi = Image.new("L", (pw_hi, ph_hi), 0)
    ImageDraw.Draw(rim_outer_hi).rounded_rectangle(
        (ss // 2, ss // 2, pw_hi - 1 - ss // 2, ph_hi - 1 - ss // 2),
        radius=rad_hi,
        outline=255,
        width=max(4, ss),
    )
    rim_outer_mask = np.asarray(rim_outer_hi.resize((pill_w, pill_h), Image.Resampling.LANCZOS), dtype=np.float32) / 255.0

    # 4x supersampled main text mask -> silky-smooth 8-bit LANCZOS anti-aliased glyph alpha
    txt_pad_hi = 8 * ss
    txt_canvas_hi = Image.new("L", ((tw1 + 16) * ss, ph_hi), 0)
    ty_hi = (ph_hi - (tb1_hi[3] - tb1_hi[1])) // 2 - tb1_hi[1] - int(round(0.5 * ss))
    ImageDraw.Draw(txt_canvas_hi).text((txt_pad_hi - tb1_hi[0], ty_hi), main_txt, font=font_main_hi, fill=255)
    txt_mask_img = txt_canvas_hi.resize((tw1 + 16, pill_h), Image.Resampling.LANCZOS)
    txt_alpha_f = (np.asarray(txt_mask_img, dtype=np.float32) / 255.0) ** 0.85
    txt_mask_u8 = np.clip(np.round(txt_alpha_f * 255.0), 0.0, 255.0).astype(np.uint8)

    # 4x supersampled static pill base (pitch-black body + optional brand/step chip + divider + Esc keycap + 'A' mark)
    yy = np.linspace(0.0, 1.0, pill_h, dtype=np.float32)[:, None]
    body_arr = np.zeros((pill_h, pill_w, 4), dtype=np.uint8)
    body_arr[..., 0] = (8.0 * (1.0 - yy)).astype(np.uint8)
    body_arr[..., 1] = (9.0 * (1.0 - yy)).astype(np.uint8)
    body_arr[..., 2] = (13.0 * (1.0 - yy)).astype(np.uint8)
    body_arr[..., 3] = np.asarray(cap_mask, dtype=np.uint8)
    static_pill = Image.fromarray(body_arr, mode="RGBA")

    chrome_hi = Image.new("RGBA", (pw_hi, ph_hi), (0, 0, 0, 0))
    cd_hi = ImageDraw.Draw(chrome_hi)
    lead_x = pad_l + icon_sz + icon_gap
    if brand_lead and step_badge:
        ly_hi = (ph_hi - (tb_lead_hi[3] - tb_lead_hi[1])) // 2 - tb_lead_hi[1] - int(round(0.5 * ss))
        cd_hi.text((lead_x * ss - tb_lead_hi[0], ly_hi), brand_lead, font=font_main_hi, fill=(214, 224, 238, 240))
        bx0 = lead_x + lead_w + badge_gap
        by0 = (pill_h - key_h) // 2
        cd_hi.rounded_rectangle(
            (bx0 * ss, by0 * ss, (bx0 + badge_w) * ss - 1, (by0 + key_h) * ss - 1),
            radius=max(16, int(round(6 * s * ss))),
            fill=(116, 198, 255, 36),
            outline=(152, 218, 255, 92),
            width=max(3, ss),
        )
        btx_hi = (bx0 + int(round(6 * s))) * ss - tb_badge_hi[0]
        bty_hi = by0 * ss + (key_h * ss - (tb_badge_hi[3] - tb_badge_hi[1])) // 2 - tb_badge_hi[1] - int(round(0.4 * ss))
        cd_hi.text((btx_hi, bty_hi), step_badge, font=font_key_hi, fill=(196, 234, 255, 255))
    tx = lead_x + extra_lead
    dx = tx + tw1 + div_gap
    cd_hi.line(
        [(dx * ss, int(round(10 * s * ss))), (dx * ss, ph_hi - int(round(10 * s * ss)))],
        fill=(255, 255, 255, 38),
        width=max(3, ss),
    )
    kx0 = dx + 1 + div_gap
    ky0 = (pill_h - key_h) // 2
    cd_hi.rounded_rectangle(
        (kx0 * ss, ky0 * ss, (kx0 + key_w) * ss - 1, (ky0 + key_h) * ss - 1),
        radius=max(16, int(round(6 * s * ss))),
        fill=(22, 25, 32, 255),
        outline=(255, 255, 255, 48),
        width=max(3, ss),
    )
    ktx_hi = (kx0 + key_pad_x) * ss - tbk_hi[0]
    kty_hi = ky0 * ss + (key_h * ss - (tbk_hi[3] - tbk_hi[1])) // 2 - tbk_hi[1] - int(round(0.4 * ss))
    cd_hi.text((ktx_hi, kty_hi), key_txt, font=font_key_hi, fill=(192, 200, 214, 255))
    static_pill.alpha_composite(chrome_hi.resize((pill_w, pill_h), Image.Resampling.LANCZOS))

    a_ico = brand_sq.resize((icon_sz, icon_sz), Image.Resampling.LANCZOS)
    static_pill.alpha_composite(a_ico, dest=(pad_l, (pill_h - icon_sz) // 2))

    shadow_base = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    ImageDraw.Draw(shadow_base).rounded_rectangle(
        (margin - 1, margin + int(round(3 * s)), margin + pill_w + 1, margin + pill_h + int(round(7 * s))),
        radius=radius + 2,
        fill=(0, 0, 0, 175),
    )
    shadow_base = shadow_base.filter(ImageFilter.GaussianBlur(radius=7.5 * s))

    halo_template = Image.new("L", (cw, ch), 0)
    ImageDraw.Draw(halo_template).rounded_rectangle(
        (margin - int(round(3 * s)), margin - int(round(2 * s)), margin + pill_w + int(round(3 * s)), margin + pill_h + int(round(3 * s))),
        radius=radius + 4,
        fill=255,
    )
    halo_template = halo_template.filter(ImageFilter.GaussianBlur(radius=9.5 * s))
    halo_a_norm = np.asarray(halo_template, dtype=np.float32) / 255.0

    tw_box = tw1 + 16
    nx_txt = np.linspace(0.0, 1.0, tw_box, dtype=np.float32)[None, :]
    nx_pill = np.linspace(0.0, 1.0, pill_w, dtype=np.float32)[None, :]
    ny_pill = np.linspace(0.0, 1.0, pill_h, dtype=np.float32)[:, None]

    frames: list[Image.Image] = []
    n = max(1, int(num_frames))
    for i in range(n):
        phase = i / float(n)
        breath = 0.5 + 0.5 * math.sin(2.0 * math.pi * phase)

        canvas = shadow_base.copy()
        halo_rgba = np.zeros((ch, cw, 4), dtype=np.uint8)
        halo_rgba[..., 0] = 128
        halo_rgba[..., 1] = 204
        halo_rgba[..., 2] = 255
        halo_rgba[..., 3] = np.clip(halo_a_norm * (20.0 + 58.0 * breath), 0.0, 255.0).astype(np.uint8)
        canvas.alpha_composite(Image.fromarray(halo_rgba, mode="RGBA"))

        pill_frame = static_pill.copy()

        # Traveling metallic pearl/ice-blue glimmer wave across "Antigravity is using your computer"
        wave_center = -0.28 + 1.56 * phase
        dist = (nx_txt - wave_center) / 0.21
        glint = np.exp(-(dist * dist))
        lead_tint = np.exp(-(((nx_txt - (wave_center + 0.07)) / 0.14) ** 2))

        base_lum = 196.0 + 18.0 * breath
        r_ch = np.clip(base_lum + 59.0 * glint - 12.0 * lead_tint, 0.0, 255.0)
        g_ch = np.clip((base_lum + 7.0) + 52.0 * glint + 16.0 * lead_tint, 0.0, 255.0)
        b_ch = np.clip((base_lum + 20.0) + 39.0 * glint + 30.0 * lead_tint, 0.0, 255.0)

        txt_rgba = np.zeros((pill_h, tw_box, 4), dtype=np.uint8)
        txt_rgba[..., 0] = np.broadcast_to(r_ch.astype(np.uint8), (pill_h, tw_box))
        txt_rgba[..., 1] = np.broadcast_to(g_ch.astype(np.uint8), (pill_h, tw_box))
        txt_rgba[..., 2] = np.broadcast_to(b_ch.astype(np.uint8), (pill_h, tw_box))
        txt_rgba[..., 3] = txt_mask_u8
        pill_frame.alpha_composite(Image.fromarray(txt_rgba, mode="RGBA"), dest=(tx - 8, 0))

        # Synchronized rim shimmer + breathing hairline glow
        rim_glint = np.exp(-(((nx_pill - wave_center) / 0.26) ** 2)) * (1.0 - 0.55 * ny_pill)
        rim_alpha = np.clip(rim_outer_mask * (36.0 + 30.0 * breath + 95.0 * rim_glint), 0.0, 255.0).astype(np.uint8)
        rim_rgba = np.zeros((pill_h, pill_w, 4), dtype=np.uint8)
        rim_rgba[..., 0] = np.clip(210.0 + 45.0 * rim_glint, 0.0, 255.0).astype(np.uint8)
        rim_rgba[..., 1] = np.clip(232.0 + 23.0 * rim_glint, 0.0, 255.0).astype(np.uint8)
        rim_rgba[..., 2] = 255
        rim_rgba[..., 3] = rim_alpha
        pill_frame.alpha_composite(Image.fromarray(rim_rgba, mode="RGBA"))

        canvas.alpha_composite(pill_frame, dest=(margin, margin))
        frames.append(canvas)

    return frames, margin


def build_black_pill(dpi_scale: float = 1.0) -> tuple[Image.Image, int]:
    frames, margin = build_black_pill_frames(dpi_scale=dpi_scale, num_frames=1)
    return frames[0], margin


def compose_ripple_frame(age: float, side: int = 96) -> Image.Image:
    """Render 0..0.38s concentric click ripple rings ONLY (no cursor sprite)."""
    prog = max(0.0, min(1.0, age / 0.38))
    sc = side / 96.0
    cx = cy = side // 2
    frame = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    draw = ImageDraw.Draw(frame)

    rad1 = int(round((5 + 30 * (1.0 - (1.0 - prog) ** 2)) * sc))
    a1 = int(215 * (1.0 - prog))
    draw.ellipse(
        (cx - rad1, cy - rad1, cx + rad1, cy + rad1),
        outline=(236, 248, 255, a1),
        width=max(2, int(round(2 * sc))),
    )
    if prog >= 0.12:
        p2 = min(1.0, (prog - 0.12) / 0.75)
        rad2 = int(round((3 + 18 * (1.0 - (1.0 - p2) ** 2)) * sc))
        a2 = int(175 * (1.0 - p2))
        draw.ellipse(
            (cx - rad2, cy - rad2, cx + rad2, cy + rad2),
            outline=(135, 206, 255, a2),
            width=max(2, int(round(2 * sc))),
        )
    return frame


def rgba_to_premultiplied_bgra(img: Image.Image) -> bytes:
    arr = np.asarray(img.convert("RGBA"), dtype=np.uint16)
    a = arr[..., 3:4]
    bgra = np.empty_like(arr, dtype=np.uint8)
    bgra[..., 0] = ((arr[..., 2:3] * a + 127) // 255)[..., 0].astype(np.uint8)
    bgra[..., 1] = ((arr[..., 1:2] * a + 127) // 255)[..., 0].astype(np.uint8)
    bgra[..., 2] = ((arr[..., 0:1] * a + 127) // 255)[..., 0].astype(np.uint8)
    bgra[..., 3] = a[..., 0].astype(np.uint8)
    return bgra.tobytes()


class CachedDIBSurface:
    """Persistent GDI DIBSection so alpha updates take <0.1ms with zero reallocation."""

    def __init__(self, img: Image.Image) -> None:
        self.w, self.h = img.size
        raw_bgra = rgba_to_premultiplied_bgra(img)
        hdc_screen = user32.GetDC(None)
        self.hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = self.w
        bmi.bmiHeader.biHeight = -self.h
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        bits_ptr = ctypes.c_void_p()
        self.hbmp = gdi32.CreateDIBSection(self.hdc_mem, ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(bits_ptr), None, 0)
        self.old_obj = None
        if self.hbmp and bits_ptr.value:
            ctypes.memmove(bits_ptr.value, raw_bgra, len(raw_bgra))
            self.old_obj = gdi32.SelectObject(self.hdc_mem, self.hbmp)
        if hdc_screen:
            user32.ReleaseDC(None, hdc_screen)

    def blit(self, hwnd: int, source_alpha: int = 255, dst_xy: tuple[int, int] = (0, 0)) -> bool:
        if not self.hbmp or not self.hdc_mem:
            return False
        pt_dst = POINT(int(dst_xy[0]), int(dst_xy[1]))
        sz = SIZE(self.w, self.h)
        pt_src = POINT(0, 0)
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, max(0, min(255, int(source_alpha))), AC_SRC_ALPHA)
        return bool(
            user32.UpdateLayeredWindow(
                wintypes.HWND(hwnd),
                None,
                ctypes.byref(pt_dst),
                ctypes.byref(sz),
                self.hdc_mem,
                ctypes.byref(pt_src),
                0,
                ctypes.byref(blend),
                ULW_ALPHA,
            )
        )

    def close(self) -> None:
        if self.old_obj and self.hdc_mem:
            gdi32.SelectObject(self.hdc_mem, self.old_obj)
            self.old_obj = None
        if self.hbmp:
            gdi32.DeleteObject(self.hbmp)
            self.hbmp = None
        if self.hdc_mem:
            gdi32.DeleteDC(self.hdc_mem)
            self.hdc_mem = None


def blit_rgba_to_hwnd(hwnd: int, img: Image.Image, source_alpha: int = 255, dst_xy: tuple[int, int] = (0, 0)) -> bool:
    surf = CachedDIBSurface(img)
    try:
        return surf.blit(hwnd, source_alpha=source_alpha, dst_xy=dst_xy)
    finally:
        surf.close()


_LAST_STATE_KEY: tuple[int, int] = (-1, -1)
_CACHED_STATE: dict = {"active": True, "cursorStyle": "sleek_dart", "updatedAt": 0.0, "ttlSec": 300.0}

_LAST_SESSION_KEY: tuple[int, int] = (-1, -1)
_CACHED_HAS_GRANTS: bool = False


def _write_json_atomic(target: Path, payload: dict) -> None:
    tmp: Path | None = None
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(payload, ensure_ascii=True)
        tmp = RUNTIME_DIR / f".{target.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp"
        tmp.write_text(raw, encoding="utf-8")
        for _ in range(8):
            try:
                tmp.replace(target)
                return
            except PermissionError:
                time.sleep(0.006)
        target.write_text(raw, encoding="utf-8")
    except Exception:
        pass
    finally:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def read_overlay_state() -> dict:
    global _LAST_STATE_KEY, _CACHED_STATE
    for attempt in range(4):
        try:
            st = OVERLAY_STATE_FILE.stat()
            key = (st.st_mtime_ns, st.st_size)
            if key == _LAST_STATE_KEY:
                return _CACHED_STATE
            data = json.loads(OVERLAY_STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _LAST_STATE_KEY = key
                _CACHED_STATE = data
                return _CACHED_STATE
        except FileNotFoundError:
            _LAST_STATE_KEY = (-1, -1)
            _CACHED_STATE = {"active": False}
            return _CACHED_STATE
        except Exception:
            if attempt < 3:
                time.sleep(0.005)
    return _CACHED_STATE


def open_parent_sync_handle(pid: int) -> int:
    if pid <= 0:
        return 0
    return int(kernel32.OpenProcess(SYNCHRONIZE, False, int(pid)) or 0)


def is_handle_signaled(handle: int) -> bool:
    if not handle:
        return False
    return kernel32.WaitForSingleObject(wintypes.HANDLE(handle), 0) == 0


def is_pid_dead(pid: int) -> bool:
    if pid <= 0:
        return False
    ctypes.set_last_error(0)
    h = kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
    if not h:
        return ctypes.get_last_error() == ERROR_INVALID_PARAMETER
    try:
        return kernel32.WaitForSingleObject(h, 0) == 0
    finally:
        kernel32.CloseHandle(h)


def _normalize_cursor_style(raw_style: object) -> str:
    s = str(raw_style or "sleek_dart").strip()
    if not s or s == "default":
        return "sleek_dart"
    return s


def run_overlay_daemon(idle_timeout_sec: float = 600.0, self_test: bool = False) -> int:
    _register_exit_guards()
    mutex_handle = None
    h_parent = 0
    if not self_test:
        ctypes.set_last_error(0)
        mutex_handle = kernel32.CreateMutexW(None, False, "Local\\AntigravityCUAOverlayMutex")
        if mutex_handle and ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(mutex_handle)
            return 0

    st_init = read_overlay_state() if not self_test else {"active": True, "label": DEFAULT_PILL_CAPTION}
    if not self_test and not st_init.get("active", False):
        if mutex_handle:
            kernel32.CloseHandle(mutex_handle)
        return 0

    init_cur = st_init.get("cursor") if isinstance(st_init.get("cursor"), dict) else None
    if init_cur and "x" in init_cur and "y" in init_cur:
        mon_x, mon_y, w, h, dpi_scale = get_monitor_for_point(int(init_cur["x"]), int(init_cur["y"]))
    else:
        cx0, cy0 = get_cursor_pos()
        mon_x, mon_y, w, h, dpi_scale = get_monitor_for_point(cx0, cy0)

    ripple_side = max(96, int(round(96 * dpi_scale)))
    ripple_half = ripple_side // 2

    def wnd_proc(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        if msg in (WM_DESTROY, WM_QUERYENDSESSION, WM_ENDSESSION):
            try:
                restore_system_cursors()
            except Exception:
                pass
        return int(user32.DefWindowProcW(hwnd, msg, wparam, lparam))

    wnd_proc_cb = WNDPROC(wnd_proc)
    hinst = kernel32.GetModuleHandleW(None)
    cls_name = f"AntigravityCUAOverlay_{os.getpid()}_{int(time.time() * 1000)}"

    wc = WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
    wc.lpfnWndProc = wnd_proc_cb
    wc.hInstance = hinst
    wc.lpszClassName = cls_name
    user32.RegisterClassExW(ctypes.byref(wc))

    init_caption = _format_pill_caption(st_init.get("label"))
    pill_frames, pill_margin = build_black_pill_frames(
        dpi_scale=dpi_scale, num_frames=1 if self_test else 28, caption=init_caption
    )
    pill_img = pill_frames[0]
    pill_x = mon_x + (w - pill_img.width) // 2
    pill_y = mon_y + max(0, int(round(16 * dpi_scale)) - pill_margin)

    ex_style = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
    hwnd_border = user32.CreateWindowExW(ex_style, cls_name, "", WS_POPUP, mon_x, mon_y, w, h, None, None, hinst, None)
    hwnd_pill = user32.CreateWindowExW(
        ex_style, cls_name, "", WS_POPUP, pill_x, pill_y, pill_img.width, pill_img.height, None, None, hinst, None
    )
    hwnd_ripple = user32.CreateWindowExW(
        ex_style, cls_name, "", WS_POPUP, mon_x, mon_y, ripple_side, ripple_side, None, None, hinst, None
    )
    if not hwnd_border or not hwnd_pill or not hwnd_ripple:
        for hw in (hwnd_ripple, hwnd_pill, hwnd_border):
            if hw:
                user32.DestroyWindow(hw)
        if mutex_handle:
            kernel32.CloseHandle(mutex_handle)
        return 1

    border_dib = CachedDIBSurface(build_base_glow_frame(w, h))
    pill_dibs = [CachedDIBSurface(fr) for fr in pill_frames]
    pill_dib = pill_dibs[0]

    # Async caption/monitor frame builder with small LRU cache so Thinking... <-> stage swaps are instant
    pending_build_lock = threading.Lock()
    pending_pill_result: dict[str, object] = {}
    building_caption_key: tuple[str, float] | None = None
    pill_frame_cache: dict[tuple[str, float], tuple[list[Image.Image], int]] = {
        (init_caption, round(dpi_scale, 3)): (pill_frames, pill_margin)
    }

    def _request_async_pill(cap: str, scale: float) -> None:
        nonlocal building_caption_key
        key = (cap, round(scale, 3))
        with pending_build_lock:
            cached = pill_frame_cache.get(key)
            if cached is not None:
                pending_pill_result["key"] = key
                pending_pill_result["frames"] = cached[0]
                pending_pill_result["margin"] = cached[1]
                return
            if building_caption_key == key:
                return
            building_caption_key = key

        def _worker() -> None:
            try:
                fr, mg = build_black_pill_frames(dpi_scale=scale, num_frames=PILL_SHIMMER_FRAMES, caption=cap)
                with pending_build_lock:
                    if len(pill_frame_cache) >= 6:
                        # Keep default, thinking & waiting captions hot; evict oldest stage caption
                        for k_old in list(pill_frame_cache.keys()):
                            if k_old[0] not in (DEFAULT_PILL_CAPTION, THINKING_PILL_CAPTION, WAITING_PILL_CAPTION):
                                pill_frame_cache.pop(k_old, None)
                                break
                    pill_frame_cache[key] = (fr, mg)
                    pending_pill_result["key"] = key
                    pending_pill_result["frames"] = fr
                    pending_pill_result["margin"] = mg
            except Exception:
                pass
            finally:
                with pending_build_lock:
                    nonlocal building_caption_key
                    if building_caption_key == key:
                        building_caption_key = None

        threading.Thread(target=_worker, name="cua-pill-render", daemon=True).start()

    try:
        if self_test:
            ok_cur = engage_system_cursors("sleek_dart")
            ok_res = restore_system_cursors()
            ok_b = border_dib.blit(hwnd_border, source_alpha=242, dst_xy=(mon_x, mon_y))
            ok_p = pill_dib.blit(hwnd_pill, source_alpha=255, dst_xy=(pill_x, pill_y))
            ok_r = blit_rgba_to_hwnd(
                hwnd_ripple,
                compose_ripple_frame(0.14, side=ripple_side),
                source_alpha=255,
                dst_xy=(mon_x + w // 2 - ripple_half, mon_y + h // 2 - ripple_half),
            )
            print(json.dumps({
                "ok": bool(ok_b and ok_p and ok_r and ok_cur and ok_res),
                "hwnd": int(hwnd_border),
                "pillHwnd": int(hwnd_pill),
                "rippleHwnd": int(hwnd_ripple),
                "systemCursorTakeover": bool(ok_cur),
                "systemCursorRestored": bool(ok_res),
                "size": [w, h],
                "pillSize": [pill_img.width, pill_img.height],
                "shimmerCycleSec": PILL_SHIMMER_CYCLE_SEC,
            }))
            return 0 if (ok_b and ok_p and ok_r and ok_cur) else 1

        # Engage cursor FIRST (safely isolated) before showing windows so startup is atomic & flicker-free
        cur_style = _normalize_cursor_style(st_init.get("cursorStyle"))
        engage_system_cursors_safe(cur_style)

        border_dib.blit(hwnd_border, source_alpha=240, dst_xy=(mon_x, mon_y))
        pill_dib.blit(hwnd_pill, source_alpha=255, dst_xy=(pill_x, pill_y))
        cur_allow_capture = bool(st_init.get("allowCapture", False))
        if not cur_allow_capture:
            user32.SetWindowDisplayAffinity(hwnd_border, WDA_EXCLUDEFROMCAPTURE)
            user32.SetWindowDisplayAffinity(hwnd_pill, WDA_EXCLUDEFROMCAPTURE)
            user32.SetWindowDisplayAffinity(hwnd_ripple, WDA_EXCLUDEFROMCAPTURE)

        user32.ShowWindow(hwnd_border, SW_SHOWNOACTIVATE)
        user32.ShowWindow(hwnd_pill, SW_SHOWNOACTIVATE)
        user32.ShowWindow(hwnd_ripple, SW_HIDE)
        user32.SetWindowPos(hwnd_border, HWND_TOPMOST, mon_x, mon_y, w, h, SWP_NOACTIVATE)
        user32.SetWindowPos(
            hwnd_pill, HWND_TOPMOST, pill_x, pill_y, pill_img.width, pill_img.height, SWP_NOACTIVATE
        )

        parent_pid_bound = int(st_init.get("parentPid") or 0)
        if parent_pid_bound > 0:
            h_parent = open_parent_sync_handle(parent_pid_bound)

        started_at = time.time()
        _write_json_atomic(
            OVERLAY_PID_FILE,
            {
                "pid": os.getpid(),
                "parentPid": parent_pid_bound,
                "status": "running",
                "reason": None,
                "hwnd": int(hwnd_border),
                "pillHwnd": int(hwnd_pill),
                "rippleHwnd": int(hwnd_ripple),
                "cursorMode": "native_system_cursor",
                "cursorStyle": cur_style,
                "startedAt": started_at,
            },
        )

        msg = MSG()
        start_t = time.monotonic()
        ripple_visible = False
        frame_idx = 0
        cur_caption = init_caption
        exit_reason = "inactive"
        idle_baseline: tuple[int, int, int, float] | None = None  # (lii_dwTime, cx, cy, last_done_ts)
        _TAKEOVER_VKS = (0x01, 0x02, 0x04, 0x0D, 0x20, 0x09, 0x08)

        while True:
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                if msg.message == WM_QUIT:
                    return 0
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))

            if user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000:
                exit_reason = "esc"
                break

            st = read_overlay_state()
            if not st.get("active", False):
                exit_reason = str(st.get("reason") or "inactive")
                break

            req_ppid = int(st.get("parentPid") or 0)
            if req_ppid > 0 and req_ppid != parent_pid_bound:
                if h_parent:
                    kernel32.CloseHandle(wintypes.HANDLE(h_parent))
                parent_pid_bound = req_ppid
                h_parent = open_parent_sync_handle(parent_pid_bound)

            if frame_idx % 16 == 0 and parent_pid_bound > 0:
                if (h_parent and is_handle_signaled(h_parent)) or (not h_parent and is_pid_dead(parent_pid_bound)):
                    exit_reason = "parent_exit"
                    break

            now_wall = time.time()
            updated_at = _finite_float(st.get("updatedAt"), now_wall)
            executing = bool(st.get("executing", False))
            if executing:
                idle_baseline = None
                lease_exp = _finite_float(st.get("leaseExpiresAt"), updated_at + 60.0)
                if now_wall > lease_exp:
                    exit_reason = "lease_expired"
                    break
            else:
                last_done = _finite_float(st.get("lastToolDoneAt"), updated_at)
                raw_idle = st.get("idleTimeoutSec")
                if raw_idle is None:
                    raw_idle = st.get("ttlSec")
                eff_idle = _finite_float(raw_idle, idle_timeout_sec)
                eff_idle = max(2.0, min(eff_idle, 1800.0))
                if (now_wall - last_done) > eff_idle:
                    exit_reason = "idle_timeout"
                    break
                # Optional physical mouse takeover (opt-in via CUA_HUD_HUMAN_TAKEOVER=1; default off so checking chat doesn't kill Waiting for model)
                takeover_enabled = os.environ.get("CUA_HUD_HUMAN_TAKEOVER", "0").strip().lower() in ("1", "true", "yes")
                if (
                    takeover_enabled
                    and frame_idx % 3 == 0
                    and (now_wall - last_done) >= 0.65
                    and not bool(st.get("disableHumanTakeover", False))
                ):
                    cur_lii = get_last_input_tick()
                    cx_now, cy_now = get_cursor_pos()
                    if idle_baseline is None or abs(idle_baseline[3] - last_done) > 0.05:
                        idle_baseline = (cur_lii, cx_now, cy_now, last_done)
                    elif cur_lii != idle_baseline[0]:
                        moved_px = math.hypot(cx_now - idle_baseline[1], cy_now - idle_baseline[2])
                        key_or_btn = any(bool(user32.GetAsyncKeyState(vk) & 0x8000) for vk in _TAKEOVER_VKS)
                        if moved_px >= 36.0 or key_or_btn:
                            exit_reason = "human_takeover"
                            break

            # Swap in asynchronously rendered 28-frame pill if ready
            ready_frames = None
            ready_margin = pill_margin
            ready_cap = cur_caption
            with pending_build_lock:
                if pending_pill_result:
                    rk = pending_pill_result.get("key")
                    ready_frames = pending_pill_result.get("frames")
                    ready_margin = int(pending_pill_result.get("margin") or pill_margin)
                    if isinstance(rk, tuple):
                        ready_cap = str(rk[0])
                    pending_pill_result.clear()
            if isinstance(ready_frames, list) and ready_frames:
                new_dibs = [CachedDIBSurface(fr) for fr in ready_frames]
                old_dibs = pill_dibs
                pill_dibs = new_dibs
                elapsed_swap = time.monotonic() - start_t
                swap_idx = min(
                    len(pill_dibs) - 1,
                    int(((elapsed_swap / PILL_SHIMMER_CYCLE_SEC) % 1.0) * len(pill_dibs)),
                )
                pill_dib = pill_dibs[swap_idx]
                pill_img = ready_frames[0]
                pill_margin = ready_margin
                pill_x = mon_x + (w - pill_img.width) // 2
                pill_y = mon_y + max(0, int(round(16 * dpi_scale)) - pill_margin)
                user32.SetWindowPos(
                    hwnd_pill, HWND_TOPMOST, pill_x, pill_y, pill_img.width, pill_img.height, SWP_NOACTIVATE
                )
                pill_dib.blit(hwnd_pill, source_alpha=255, dst_xy=(pill_x, pill_y))
                cur_caption = ready_cap
                for od in old_dibs:
                    od.close()

            if frame_idx % 6 == 0:
                if executing:
                    req_caption = _format_pill_caption(st.get("label"))
                else:
                    last_done_cap = _finite_float(st.get("lastToolDoneAt"), updated_at)
                    idle_elapsed = now_wall - last_done_cap
                    wait_after = _finite_float(st.get("thinkingWaitSec"), WAITING_DELAY_SEC)
                    wait_after = max(5.0, min(wait_after, 300.0))
                    if idle_elapsed >= wait_after:
                        req_caption = WAITING_PILL_CAPTION
                    elif idle_elapsed >= THINKING_DELAY_SEC:
                        req_caption = THINKING_PILL_CAPTION
                    else:
                        req_caption = _format_pill_caption(st.get("label"))
                if req_caption != cur_caption:
                    _request_async_pill(req_caption, dpi_scale)

            if frame_idx % 16 == 0:
                req_style = _normalize_cursor_style(st.get("cursorStyle"))
                if req_style != cur_style:
                    cur_style = req_style
                    engage_system_cursors_safe(cur_style)
                req_cap = bool(st.get("allowCapture", False))
                if req_cap != cur_allow_capture:
                    cur_allow_capture = req_cap
                    aff = 0 if cur_allow_capture else WDA_EXCLUDEFROMCAPTURE
                    user32.SetWindowDisplayAffinity(hwnd_border, aff)
                    user32.SetWindowDisplayAffinity(hwnd_pill, aff)
                    user32.SetWindowDisplayAffinity(hwnd_ripple, aff)

                # Follow active monitor (supports multi-monitor negative X/Y and mixed DPI)
                c_info = st.get("cursor") if isinstance(st.get("cursor"), dict) else None
                if c_info and "x" in c_info and "y" in c_info:
                    tx_m, ty_m = int(c_info["x"]), int(c_info["y"])
                else:
                    tx_m, ty_m = get_cursor_pos()
                nx, ny, nw, nh, ndpi = get_monitor_for_point(tx_m, ty_m)
                if (nx, ny, nw, nh) != (mon_x, mon_y, w, h) or abs(ndpi - dpi_scale) > 0.05:
                    mon_x, mon_y, w, h, dpi_scale = nx, ny, nw, nh, ndpi
                    ripple_side = max(96, int(round(96 * dpi_scale)))
                    ripple_half = ripple_side // 2
                    old_b = border_dib
                    border_dib = CachedDIBSurface(build_base_glow_frame(w, h))
                    old_b.close()
                    user32.SetWindowPos(hwnd_border, HWND_TOPMOST, mon_x, mon_y, w, h, SWP_NOACTIVATE)
                    _request_async_pill(cur_caption, dpi_scale)

            # Click ripple ring (0.38s after a click; zero double cursor)
            click_info = st.get("click")
            click_age = (now_wall - _finite_float(click_info.get("ts"), 0.0)) if isinstance(click_info, dict) else 999.0
            if 0.0 <= click_age <= 0.38 and isinstance(click_info, dict):
                cx = int(click_info.get("x", mon_x + w // 2))
                cy = int(click_info.get("y", mon_y + h // 2))
                rframe = compose_ripple_frame(click_age, side=ripple_side)
                blit_rgba_to_hwnd(hwnd_ripple, rframe, source_alpha=255, dst_xy=(cx - ripple_half, cy - ripple_half))
                if not ripple_visible:
                    user32.ShowWindow(hwnd_ripple, SW_SHOWNOACTIVATE)
                    user32.SetWindowPos(
                        hwnd_ripple, HWND_TOPMOST, 0, 0, ripple_side, ripple_side, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
                    )
                    ripple_visible = True
            elif ripple_visible:
                user32.ShowWindow(hwnd_ripple, SW_HIDE)
                ripple_visible = False

            # Constant 2.15s wall-clock shimmer sweep (always 28 smooth frames) + calm breathing border glow
            elapsed = time.monotonic() - start_t
            if frame_idx % 2 == 0 and len(pill_dibs) > 0:
                phase = (elapsed / PILL_SHIMMER_CYCLE_SEC) % 1.0
                p_idx = min(len(pill_dibs) - 1, int(phase * len(pill_dibs)))
                pill_dib = pill_dibs[p_idx]
                pill_dib.blit(hwnd_pill, source_alpha=255, dst_xy=(pill_x, pill_y))
            if frame_idx % 3 == 0:
                breath_alpha = int(214 + 38 * (0.5 + 0.5 * math.sin(elapsed * 2.0)))
                border_dib.blit(hwnd_border, source_alpha=breath_alpha, dst_xy=(mon_x, mon_y))

            frame_idx += 1
            if frame_idx % 45 == 0:
                user32.SetWindowPos(hwnd_border, HWND_TOPMOST, mon_x, mon_y, w, h, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
                user32.SetWindowPos(
                    hwnd_pill, HWND_TOPMOST, pill_x, pill_y, pill_img.width, pill_img.height, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
                )

            time.sleep(0.016)

        # Immediately restore OS cursor, publish exit status, and release mutex BEFORE fade-out (eliminates respawn races)
        restore_system_cursors()
        _write_json_atomic(
            OVERLAY_PID_FILE,
            {
                "pid": os.getpid(),
                "parentPid": parent_pid_bound,
                "status": "exiting",
                "reason": exit_reason,
                "stoppedAt": time.time(),
                "startedAt": started_at,
            },
        )
        if mutex_handle:
            kernel32.CloseHandle(mutex_handle)
            mutex_handle = None

        if ripple_visible:
            user32.ShowWindow(hwnd_ripple, SW_HIDE)
        fade_steps = (140, 60, 0) if exit_reason in ("human_takeover", "esc") else (170, 95, 35, 0)
        fade_delay = 0.014 if exit_reason in ("human_takeover", "esc") else 0.020
        for a in fade_steps:
            border_dib.blit(hwnd_border, source_alpha=a, dst_xy=(mon_x, mon_y))
            pill_dib.blit(hwnd_pill, source_alpha=a, dst_xy=(pill_x, pill_y))
            time.sleep(fade_delay)
    finally:
        restore_system_cursors()
        for pd in pill_dibs:
            pd.close()
        border_dib.close()
        for hw in (hwnd_ripple, hwnd_pill, hwnd_border):
            try:
                user32.DestroyWindow(hw)
            except Exception:
                pass
        if h_parent:
            kernel32.CloseHandle(wintypes.HANDLE(h_parent))
        if mutex_handle:
            kernel32.CloseHandle(mutex_handle)
    return 0


def main() -> int:
    try:
        return run_overlay_daemon(self_test=("--self-test" in sys.argv))
    except Exception:
        restore_system_cursors()
        _append_error_log("main")
        try:
            print(traceback.format_exc(), file=sys.stderr, flush=True)
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
