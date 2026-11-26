"""Native Windows 10/11 (win32) platform backend for Antigravity Computer Use v3.2.0.

Preserves the exact v3.1.0 Win32 ctypes / GDI PrintWindow / Per-Monitor DPI v2 /
120Hz SendInput / UI Automation (C# + PowerShell fallback) / CF_UNICODETEXT
clipboard / Job Object implementation with zero regressions.
"""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
import ctypes
from ctypes import wintypes
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import winreg
from typing import Any, Callable

# 1. Enable Per-Monitor DPI Awareness v2 BEFORE importing GUI/screen libraries.
try:
    _early_user32 = ctypes.windll.user32
    _early_user32.SetProcessDpiAwarenessContext.argtypes = [wintypes.HANDLE]
    _early_user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
    _early_user32.SetProcessDpiAwarenessContext(wintypes.HANDLE(-4))
except Exception:
    try:
        _early_shcore = ctypes.windll.shcore
        _early_shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
        _early_shcore.SetProcessDpiAwareness.restype = ctypes.c_long
        _early_shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

from PIL import Image, ImageFont, ImageGrab

SCRIPT_DIR = Path(__file__).resolve().parent
OVERLAY_SCRIPT = SCRIPT_DIR / "overlay_hud.py"
UIA_CS_FILE = SCRIPT_DIR / "uia_fast_bridge.cs"

DPI_MODE = "PerMonitorV2"
SELECT_ALL_MODIFIER = "ctrl"

_RUNTIME_DIR: Path = SCRIPT_DIR.parent / ".runtime"


def configure(runtime_dir: Path, _overlay_pid_file: Path) -> None:
    global _RUNTIME_DIR
    _RUNTIME_DIR = runtime_dir


user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
dwmapi = ctypes.windll.dwmapi
kernel32 = ctypes.windll.kernel32
advapi32 = ctypes.windll.advapi32
winmm = ctypes.windll.winmm
winmm.timeBeginPeriod.argtypes = [wintypes.UINT]
winmm.timeBeginPeriod.restype = wintypes.UINT
winmm.timeEndPeriod.argtypes = [wintypes.UINT]
winmm.timeEndPeriod.restype = wintypes.UINT
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SetCursorPos.restype = wintypes.BOOL
user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT]
user32.SystemParametersInfoW.restype = wintypes.BOOL

SW_RESTORE = 9
GA_ROOT = 2
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
PW_RENDERFULLCONTENT = 0x00000002
DIB_RGB_COLORS = 0
BI_RGB = 0
VK_LBUTTON = 0x01
VK_BACK = 0x08
VK_ESCAPE = 0x1B
VK_END = 0x23
VK_HOME = 0x24
VK_DELETE = 0x2E
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
GW_ENABLEDPOPUP = 6
SYNCHRONIZE = 0x00100000
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x01000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000
_CUA_INPUT_TAG = 0x41475943
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JobObjectExtendedLimitInformation = 9
TH32CS_SNAPPROCESS = 0x00000002
WHEEL_DELTA = 120
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
DESKTOP_SWITCHDESKTOP = 0x0100
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TokenElevation = 20

WM_NULL = 0x0000
WM_ACTIVATE = 0x0006
WM_SETTEXT = 0x000C
WM_GETTEXT = 0x000D
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_IME_CHAR = 0x0286
BM_CLICK = 0x00F5
WA_ACTIVE = 1
MK_LBUTTON = 0x0001
SMTO_ABORTIFHUNG = 0x0002
GWL_STYLE = -16
ES_PASSWORD = 0x0020
EM_GETPASSWORDCHAR = 0x00D2

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

ULONG_PTR = ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_uint32
LONG_PTR = ctypes.c_int64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_int32
LRESULT = LONG_PTR
SIZE_T = ULONG_PTR
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


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
    _fields_ = [
        ("bmiHeader", BITMAPINFOHEADER),
        ("bmiColors", wintypes.DWORD * 3),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", RECT),
    ]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", wintypes.DWORD),
    ]


MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(RECT), wintypes.LPARAM
)


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", SIZE_T),
        ("MaximumWorkingSetSize", SIZE_T),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ULONG_PTR),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", SIZE_T),
        ("JobMemoryLimit", SIZE_T),
        ("PeakProcessMemoryUsed", SIZE_T),
        ("PeakJobMemoryUsed", SIZE_T),
    ]


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.DWORD),
        ("union", _INPUT_UNION),
    ]


user32.WindowFromPoint.argtypes = [POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.ReleaseDC.restype = ctypes.c_int
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
user32.PrintWindow.restype = wintypes.BOOL
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.GetThreadDesktop.argtypes = [wintypes.DWORD]
user32.GetThreadDesktop.restype = wintypes.HDESK
user32.GetUserObjectInformationW.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)
]
user32.GetUserObjectInformationW.restype = wintypes.BOOL
user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
user32.OpenInputDesktop.restype = wintypes.HDESK
user32.CloseDesktop.argtypes = [wintypes.HDESK]
user32.CloseDesktop.restype = wintypes.BOOL
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = wintypes.SHORT
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsWindowEnabled.argtypes = [wintypes.HWND]
user32.IsWindowEnabled.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND
user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUITHREADINFO)]
user32.GetGUIThreadInfo.restype = wintypes.BOOL
user32.VkKeyScanW.argtypes = [wintypes.WCHAR]
user32.VkKeyScanW.restype = ctypes.c_short
user32.VkKeyScanExW.argtypes = [wintypes.WCHAR, wintypes.HKL]
user32.VkKeyScanExW.restype = ctypes.c_short
user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
user32.MapVirtualKeyW.restype = wintypes.UINT
user32.MapVirtualKeyExW.argtypes = [wintypes.UINT, wintypes.UINT, wintypes.HKL]
user32.MapVirtualKeyExW.restype = wintypes.UINT
user32.GetKeyboardLayout.argtypes = [wintypes.DWORD]
user32.GetKeyboardLayout.restype = wintypes.HKL
user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.POINTER(RECT), MONITORENUMPROC, wintypes.LPARAM]
user32.EnumDisplayMonitors.restype = wintypes.BOOL
user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]
user32.GetMonitorInfoW.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
user32.GetClientRect.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wintypes.HWND
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.AttachThreadInput.restype = wintypes.BOOL
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.BringWindowToTop.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.GetKeyboardState.argtypes = [ctypes.POINTER(ctypes.c_ubyte)]
user32.GetKeyboardState.restype = wintypes.BOOL
user32.SetKeyboardState.argtypes = [ctypes.POINTER(ctypes.c_ubyte)]
user32.SetKeyboardState.restype = wintypes.BOOL
user32.SendInput.argtypes = [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.EnumChildWindows.argtypes = [wintypes.HWND, WNDENUMPROC, wintypes.LPARAM]
user32.EnumChildWindows.restype = wintypes.BOOL
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = wintypes.LONG
user32.SendMessageTimeoutW.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, ctypes.c_void_p,
    wintypes.UINT, wintypes.UINT, ctypes.POINTER(ULONG_PTR),
]
user32.SendMessageTimeoutW.restype = LRESULT
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
user32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(POINT)]
user32.ScreenToClient.restype = wintypes.BOOL
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.argtypes = []
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.argtypes = []
user32.EmptyClipboard.restype = wintypes.BOOL
user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE

gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD,
]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.restype = wintypes.BOOL
gdi32.BitBlt.argtypes = [
    wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD,
]
gdi32.BitBlt.restype = wintypes.BOOL
SRCCOPY = 0x00CC0020

dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long

kernel32.GetCurrentThreadId.argtypes = []
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.GetProcessTimes.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
    ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
]
kernel32.GetProcessTimes.restype = wintypes.BOOL
kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
kernel32.CreateJobObjectW.restype = wintypes.HANDLE
kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
kernel32.SetInformationJobObject.restype = wintypes.BOOL
kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
kernel32.Process32FirstW.restype = wintypes.BOOL
kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
kernel32.Process32NextW.restype = wintypes.BOOL
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, SIZE_T]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalFree.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.restype = wintypes.BOOL
kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalSize.restype = SIZE_T

advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
advapi32.OpenProcessToken.restype = wintypes.BOOL
advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
advapi32.GetTokenInformation.restype = wintypes.BOOL


def _is_sandboxed_low_integrity() -> bool:
    htok = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(wintypes.HANDLE(-1), TOKEN_QUERY, ctypes.byref(htok)):
        return False
    try:
        is_appc = wintypes.DWORD(0)
        ret_len = wintypes.DWORD(0)
        if advapi32.GetTokenInformation(htok, 29, ctypes.byref(is_appc), ctypes.sizeof(is_appc), ctypes.byref(ret_len)):
            if is_appc.value != 0:
                return True
        buf = (ctypes.c_ubyte * 128)()
        if advapi32.GetTokenInformation(htok, 25, ctypes.byref(buf), ctypes.sizeof(buf), ctypes.byref(ret_len)):
            sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p)).contents.value
            if sid_ptr:
                sub_count = ctypes.c_ubyte.from_address(sid_ptr + 1).value
                if sub_count >= 1:
                    rid = ctypes.c_uint32.from_address(sid_ptr + 8 + (sub_count - 1) * 4).value
                    if rid < 0x2000:
                        return True
    except Exception:
        pass
    finally:
        kernel32.CloseHandle(htok)
    return False


IS_LOW_INTEGRITY = _is_sandboxed_low_integrity()


def platform_status_extra() -> dict[str, Any]:
    return {}


_HELPER_JOB: int = 0
_TIMER_PERIOD_DEPTH: int = 0


def _ensure_helper_job() -> int:
    global _HELPER_JOB
    if _HELPER_JOB:
        return _HELPER_JOB
    try:
        hjob = int(kernel32.CreateJobObjectW(None, None) or 0)
        if hjob:
            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if kernel32.SetInformationJobObject(
                wintypes.HANDLE(hjob),
                JobObjectExtendedLimitInformation,
                ctypes.byref(info),
                ctypes.sizeof(info),
            ):
                _HELPER_JOB = hjob
            else:
                kernel32.CloseHandle(wintypes.HANDLE(hjob))
    except Exception:
        pass
    return _HELPER_JOB


def _assign_to_helper_job(proc: subprocess.Popen[Any]) -> None:
    hjob = _ensure_helper_job()
    if not hjob:
        return
    try:
        hproc = int(getattr(proc, "_handle", 0) or 0)
        if hproc:
            kernel32.AssignProcessToJobObject(wintypes.HANDLE(hjob), wintypes.HANDLE(hproc))
    except Exception:
        pass


def close_helpers() -> None:
    global _HELPER_JOB, _PRINTWINDOW_POOL
    kill_ui_bridge()
    with _PRINTWINDOW_POOL_LOCK:
        if _PRINTWINDOW_POOL is not None:
            _PRINTWINDOW_POOL.shutdown(wait=False, cancel_futures=True)
            _PRINTWINDOW_POOL = None
    if _HELPER_JOB:
        try:
            kernel32.CloseHandle(wintypes.HANDLE(_HELPER_JOB))
        except Exception:
            pass
        _HELPER_JOB = 0


def restore_os_cursors_safe() -> None:
    try:
        user32.SystemParametersInfoW(0x0057, 0, None, 0)
    except Exception:
        pass


def enter_1ms_timer() -> None:
    global _TIMER_PERIOD_DEPTH
    if _TIMER_PERIOD_DEPTH == 0:
        winmm.timeBeginPeriod(1)
    _TIMER_PERIOD_DEPTH += 1


def exit_1ms_timer() -> None:
    global _TIMER_PERIOD_DEPTH
    if _TIMER_PERIOD_DEPTH > 0:
        _TIMER_PERIOD_DEPTH -= 1
        if _TIMER_PERIOD_DEPTH == 0:
            winmm.timeEndPeriod(1)


def get_cursor_pos() -> tuple[int, int]:
    pt = POINT(0, 0)
    user32.GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)


def pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    h = kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
    if not h:
        return False
    try:
        return kernel32.WaitForSingleObject(h, 0) != 0
    finally:
        kernel32.CloseHandle(h)


def query_pid_exe(pid: int) -> str:
    if pid <= 0:
        return ""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return ""
    try:
        sz = wintypes.DWORD(512)
        buf = ctypes.create_unicode_buffer(512)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(sz)):
            return (buf.value or "").strip()
        return ""
    finally:
        kernel32.CloseHandle(h)


def is_process_elevated(pid: int) -> bool:
    if pid <= 0:
        return False
    is_self = pid == os.getpid()
    hproc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not hproc:
        return False if is_self else True
    try:
        htok = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(wintypes.HANDLE(hproc), TOKEN_QUERY, ctypes.byref(htok)):
            return False if is_self else True
        try:
            elev = wintypes.DWORD(0)
            ret_len = wintypes.DWORD(0)
            ok = advapi32.GetTokenInformation(
                htok, TokenElevation, ctypes.byref(elev), ctypes.sizeof(elev), ctypes.byref(ret_len)
            )
            if not ok:
                return False if is_self else True
            return elev.value != 0
        finally:
            kernel32.CloseHandle(htok)
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(hproc))


def get_proc_create_time(pid: int) -> int | None:
    if pid <= 0:
        return None
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return None
    try:
        ct = wintypes.FILETIME()
        et = wintypes.FILETIME()
        kt = wintypes.FILETIME()
        ut = wintypes.FILETIME()
        if kernel32.GetProcessTimes(wintypes.HANDLE(h), ctypes.byref(ct), ctypes.byref(et), ctypes.byref(kt), ctypes.byref(ut)):
            return (int(ct.dwHighDateTime) << 32) | int(ct.dwLowDateTime)
        return None
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(h))


def virtual_bounds() -> dict[str, int]:
    return {
        "left": int(user32.GetSystemMetrics(76)),
        "top": int(user32.GetSystemMetrics(77)),
        "width": int(user32.GetSystemMetrics(78)),
        "height": int(user32.GetSystemMetrics(79)),
    }


def get_monitor_rects() -> list[tuple[int, int, int, int]]:
    rects: list[tuple[int, int, int, int]] = []

    def _cb(hmon: int, _hdc: int, _r: object, _d: int) -> bool:
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(wintypes.HMONITOR(hmon), ctypes.byref(mi)):
            r = mi.rcMonitor
            rects.append((int(r.left), int(r.top), int(r.right), int(r.bottom)))
        return True

    cb_ref = MONITORENUMPROC(_cb)
    try:
        user32.EnumDisplayMonitors(None, None, cb_ref, 0)
    except Exception:
        pass
    if not rects:
        vb = virtual_bounds()
        rects.append((vb["left"], vb["top"], vb["left"] + vb["width"], vb["top"] + vb["height"]))
    return rects


def get_foreground_hwnd() -> int:
    return int(user32.GetForegroundWindow() or 0)


def current_desktop_name() -> str:
    try:
        hdesk = user32.GetThreadDesktop(kernel32.GetCurrentThreadId())
        if not hdesk:
            return "unknown"
        buf = ctypes.create_unicode_buffer(256)
        needed = wintypes.DWORD(0)
        if user32.GetUserObjectInformationW(wintypes.HANDLE(hdesk), 2, buf, ctypes.sizeof(buf), ctypes.byref(needed)):
            return buf.value
    except Exception:
        pass
    return "unknown"


def is_desktop_locked() -> bool:
    if IS_LOW_INTEGRITY or current_desktop_name().lower() != "default":
        return False
    hdesk = user32.OpenInputDesktop(0, False, DESKTOP_SWITCHDESKTOP)
    if not hdesk:
        return True
    user32.CloseDesktop(wintypes.HDESK(hdesk))
    return False


def is_escape_pressed() -> bool:
    return bool(user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000)


def is_window(hwnd: int) -> bool:
    return bool(user32.IsWindow(wintypes.HWND(hwnd)))


def is_window_visible(hwnd: int) -> bool:
    return bool(user32.IsWindowVisible(wintypes.HWND(hwnd))) and not is_window_cloaked(hwnd)


def is_window_minimized(hwnd: int) -> bool:
    return bool(user32.IsIconic(wintypes.HWND(hwnd)))


def is_window_cloaked(hwnd: int) -> bool:
    cloaked = wintypes.DWORD(0)
    hr = dwmapi.DwmGetWindowAttribute(
        wintypes.HWND(hwnd),
        wintypes.DWORD(DWMWA_CLOAKED),
        ctypes.byref(cloaked),
        ctypes.sizeof(cloaked),
    )
    return hr == 0 and cloaked.value != 0


def get_window_title_safe(hwnd: int, timeout_ms: int = 120) -> str:
    buf = ctypes.create_unicode_buffer(512)
    res_len = ULONG_PTR(0)
    ok = user32.SendMessageTimeoutW(
        wintypes.HWND(hwnd), WM_GETTEXT, 512, buf, SMTO_ABORTIFHUNG, timeout_ms, ctypes.byref(res_len)
    )
    if not ok:
        return ""
    return (buf.value or "").strip()


def enum_visible_windows(*, require_title: bool = True, timeout_ms: int = 120) -> list[tuple[int, str]]:
    results: list[tuple[int, str]] = []

    def _cb(hwnd_ptr: int | None, _: int) -> bool:
        if not hwnd_ptr:
            return True
        h = int(hwnd_ptr)
        if not user32.IsWindowVisible(wintypes.HWND(h)):
            return True
        if is_window_cloaked(h):
            return True
        cls_buf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(wintypes.HWND(h), cls_buf, 64)
        if (cls_buf.value or "").startswith("AntigravityCUAOverlay_"):
            return True
        title = get_window_title_safe(h, timeout_ms=timeout_ms)
        if require_title and not title:
            return True
        results.append((h, title))
        return True

    cb_ref = WNDENUMPROC(_cb)
    user32.EnumWindows(cb_ref, 0)
    return results


def get_window_bounds(hwnd: int) -> dict[str, int]:
    rect = RECT()
    hr = dwmapi.DwmGetWindowAttribute(
        wintypes.HWND(hwnd),
        wintypes.DWORD(DWMWA_EXTENDED_FRAME_BOUNDS),
        ctypes.byref(rect),
        ctypes.sizeof(rect),
    )
    if hr != 0:
        user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect))
    return {
        "left": int(rect.left),
        "top": int(rect.top),
        "right": int(rect.right),
        "bottom": int(rect.bottom),
        "width": max(0, int(rect.right - rect.left)),
        "height": max(0, int(rect.bottom - rect.top)),
    }


def get_window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    return int(pid.value)


def get_process_info(hwnd: int) -> tuple[int, str, str]:
    pid_val = get_window_pid(hwnd)
    if pid_val <= 0:
        return 0, "unknown", ""
    exe = query_pid_exe(pid_val)
    if exe:
        return pid_val, Path(exe).name, exe
    return pid_val, "unknown", ""


def get_window_bundle_id(hwnd: int) -> str:
    return ""


def detect_active_popup(owner_hwnd: int) -> int:
    try:
        ep = int(user32.GetWindow(wintypes.HWND(owner_hwnd), GW_ENABLEDPOPUP) or 0)
        if ep and ep != int(owner_hwnd) and user32.IsWindowVisible(wintypes.HWND(ep)):
            return ep
        tid = user32.GetWindowThreadProcessId(wintypes.HWND(owner_hwnd), None)
        if tid:
            gti = GUITHREADINFO()
            gti.cbSize = ctypes.sizeof(GUITHREADINFO)
            if user32.GetGUIThreadInfo(tid, ctypes.byref(gti)):
                mo = int(gti.hwndMenuOwner or 0)
                if mo and mo != int(owner_hwnd) and user32.IsWindowVisible(wintypes.HWND(mo)):
                    return mo
        owner_pid, _, _ = get_process_info(owner_hwnd)
        if owner_pid > 0:
            fg = get_foreground_hwnd()
            if fg and fg != int(owner_hwnd) and user32.IsWindowVisible(wintypes.HWND(fg)):
                fg_pid, _, _ = get_process_info(fg)
                if fg_pid == owner_pid:
                    return fg
    except Exception:
        pass
    return 0


def activate_hwnd(hwnd: int) -> bool:
    if user32.IsIconic(wintypes.HWND(hwnd)):
        user32.ShowWindow(wintypes.HWND(hwnd), SW_RESTORE)
        time.sleep(0.15)
    fg = get_foreground_hwnd()
    if fg == int(hwnd):
        return True
    fg_tid = user32.GetWindowThreadProcessId(wintypes.HWND(fg), None) if fg else 0
    cur_tid = kernel32.GetCurrentThreadId()
    attached = False
    try:
        if fg_tid and fg_tid != cur_tid:
            attached = bool(user32.AttachThreadInput(cur_tid, fg_tid, True))
        user32.BringWindowToTop(wintypes.HWND(hwnd))
        user32.SetForegroundWindow(wintypes.HWND(hwnd))
    finally:
        if attached:
            user32.AttachThreadInput(cur_tid, fg_tid, False)
    for _ in range(8):
        time.sleep(0.04)
        if get_foreground_hwnd() == int(hwnd):
            return True
    if get_foreground_hwnd() == 0 and current_desktop_name().lower() != "default":
        return bool(user32.IsWindow(wintypes.HWND(hwnd)))
    return get_foreground_hwnd() == int(hwnd)


def restore_window(hwnd: int) -> None:
    user32.ShowWindow(wintypes.HWND(hwnd), SW_RESTORE)


def is_foreground_for_window(hwnd: int, fg_hwnd: int) -> bool:
    return int(fg_hwnd) == int(hwnd)


def verify_point_belongs_to_hwnd(sx: int, sy: int, target_hwnd: int) -> tuple[bool, int]:
    pt = POINT(sx, sy)
    hit = int(user32.WindowFromPoint(pt) or 0)
    if not hit:
        return False, 0
    root = int(user32.GetAncestor(wintypes.HWND(hit), GA_ROOT) or hit)
    if root == int(target_hwnd):
        return True, root
    t_pid, _, _ = get_process_info(target_hwnd)
    h_pid, _, _ = get_process_info(root)
    return (t_pid > 0 and t_pid == h_pid), root


def _has_child_windows(hwnd: int) -> bool:
    if hwnd <= 0:
        return False
    found = [False]

    def _cb(_child_ptr: int | None, _: int) -> bool:
        found[0] = True
        return False

    cb_ref = WNDENUMPROC(_cb)
    user32.EnumChildWindows(wintypes.HWND(hwnd), cb_ref, 0)
    return found[0]


def ensure_child_canvas_focus(sx: int, sy: int, root_hwnd: int, *, hitting_owned_popup: bool = False) -> None:
    if hitting_owned_popup or IS_LOW_INTEGRITY or current_desktop_name().lower() != "default":
        return
    try:
        hit = int(user32.WindowFromPoint(POINT(sx, sy)) or 0)
        if not hit or hit == int(root_hwnd) or not user32.IsWindowEnabled(wintypes.HWND(hit)):
            return
        if _has_child_windows(hit):
            return
        c_buf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(wintypes.HWND(hit), c_buf, 64)
        if (c_buf.value or "").lower() in ("button", "static", "#32768", "combolbox"):
            return
        cur_tid = kernel32.GetCurrentThreadId()
        target_tid = user32.GetWindowThreadProcessId(wintypes.HWND(hit), None)
        if not target_tid:
            return
        gti = GUITHREADINFO()
        gti.cbSize = ctypes.sizeof(GUITHREADINFO)
        if user32.GetGUIThreadInfo(target_tid, ctypes.byref(gti)) and int(gti.hwndFocus or 0) == hit:
            return
        attached = False
        if target_tid != cur_tid:
            attached = bool(user32.AttachThreadInput(cur_tid, target_tid, True))
        try:
            user32.SetFocus(wintypes.HWND(hit))
        finally:
            if attached:
                user32.AttachThreadInput(cur_tid, target_tid, False)
    except Exception:
        pass


# Capture (PrintWindow + GDI BitBlt)
def _image_has_signal(image: Image.Image) -> bool:
    thumb = image.resize((64, 64), Image.Resampling.NEAREST)
    ext = thumb.getextrema()
    if isinstance(ext[0], tuple):
        return any(hi > 0 for _, hi in ext)
    return ext[1] > 0


_PRINTWINDOW_POOL_LOCK = threading.Lock()
_PRINTWINDOW_POOL: ThreadPoolExecutor | None = ThreadPoolExecutor(max_workers=2, thread_name_prefix="cua-pw")


def _get_printwindow_pool() -> ThreadPoolExecutor:
    global _PRINTWINDOW_POOL
    with _PRINTWINDOW_POOL_LOCK:
        if _PRINTWINDOW_POOL is None:
            _PRINTWINDOW_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="cua-pw")
        return _PRINTWINDOW_POOL


def _printwindow_worker(hwnd: int, w: int, h: int, pw_flags: int) -> bytes | None:
    hdc_screen = None
    hdc_mem = None
    hbmp = None
    old_obj = None
    try:
        hdc_screen = user32.GetDC(None)
        if not hdc_screen:
            return None
        hdc_mem = gdi32.CreateCompatibleDC(wintypes.HDC(hdc_screen))
        if not hdc_mem:
            return None
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        bits_ptr = ctypes.c_void_p()
        hbmp = gdi32.CreateDIBSection(
            wintypes.HDC(hdc_mem), ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(bits_ptr), None, 0
        )
        if not hbmp or not bits_ptr.value:
            return None
        old_obj = gdi32.SelectObject(wintypes.HDC(hdc_mem), wintypes.HGDIOBJ(hbmp))
        ok = user32.PrintWindow(wintypes.HWND(hwnd), wintypes.HDC(hdc_mem), pw_flags)
        if not ok:
            return None
        return ctypes.string_at(bits_ptr.value, w * h * 4)
    finally:
        if old_obj and hdc_mem:
            gdi32.SelectObject(wintypes.HDC(hdc_mem), wintypes.HGDIOBJ(old_obj))
        if hbmp:
            gdi32.DeleteObject(wintypes.HGDIOBJ(hbmp))
        if hdc_mem:
            gdi32.DeleteDC(wintypes.HDC(hdc_mem))
        if hdc_screen:
            user32.ReleaseDC(None, wintypes.HDC(hdc_screen))


def capture_window(hwnd: int) -> tuple[Image.Image | None, dict[str, int], str]:
    global _PRINTWINDOW_POOL
    rect = RECT()
    user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect))
    w = int(rect.right - rect.left)
    h = int(rect.bottom - rect.top)
    fallback_origin = {"left": int(rect.left), "top": int(rect.top), "width": w, "height": h}
    if w <= 4 or h <= 4 or user32.IsIconic(wintypes.HWND(hwnd)):
        return None, fallback_origin, ""

    is_modal = (not user32.IsWindowEnabled(wintypes.HWND(hwnd))) or (
        int(user32.GetWindow(wintypes.HWND(hwnd), GW_ENABLEDPOPUP) or 0) != 0
    )
    pw_timeout = 0.45 if is_modal else 1.2

    img: Image.Image | None = None
    for pw_flags in (PW_RENDERFULLCONTENT, 0):
        pool = _get_printwindow_pool()
        fut = pool.submit(_printwindow_worker, int(hwnd), w, h, pw_flags)
        try:
            raw = fut.result(timeout=pw_timeout)
        except FuturesTimeoutError:
            with _PRINTWINDOW_POOL_LOCK:
                if _PRINTWINDOW_POOL is pool:
                    _PRINTWINDOW_POOL.shutdown(wait=False, cancel_futures=True)
                    _PRINTWINDOW_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="cua-pw")
            return None, fallback_origin, ""
        if not raw:
            continue
        candidate = Image.frombuffer("RGBA", (w, h), raw, "raw", "BGRA", 0, 1).convert("RGB")
        if _image_has_signal(candidate):
            img = candidate
            break

    if img is None:
        return None, fallback_origin, ""

    ext = get_window_bounds(hwnd)
    dx1 = max(0, ext["left"] - int(rect.left))
    dy1 = max(0, ext["top"] - int(rect.top))
    dx2 = min(w, dx1 + ext["width"])
    dy2 = min(h, dy1 + ext["height"])
    if dx2 > dx1 + 4 and dy2 > dy1 + 4:
        img = img.crop((dx1, dy1, dx2, dy2))
        origin = {"left": ext["left"], "top": ext["top"], "width": img.width, "height": img.height}
    else:
        origin = fallback_origin

    if not _image_has_signal(img):
        return None, origin, ""
    return img, origin, "print_window_background"


def capture_desktop() -> tuple[Image.Image, dict[str, int], str]:
    vb = virtual_bounds()
    w, h = max(1, vb["width"]), max(1, vb["height"])
    hdc_screen = None
    hdc_mem = None
    hbmp = None
    old_obj = None
    try:
        hdc_screen = user32.GetDC(None)
        if not hdc_screen:
            return ImageGrab.grab(all_screens=True), vb, "desktop_gdi_bitblt"
        hdc_mem = gdi32.CreateCompatibleDC(wintypes.HDC(hdc_screen))
        if not hdc_mem:
            return ImageGrab.grab(all_screens=True), vb, "desktop_gdi_bitblt"
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        bits_ptr = ctypes.c_void_p()
        hbmp = gdi32.CreateDIBSection(
            wintypes.HDC(hdc_mem), ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(bits_ptr), None, 0
        )
        if not hbmp or not bits_ptr.value:
            return ImageGrab.grab(all_screens=True), vb, "desktop_gdi_bitblt"
        old_obj = gdi32.SelectObject(wintypes.HDC(hdc_mem), wintypes.HGDIOBJ(hbmp))
        ok = gdi32.BitBlt(
            wintypes.HDC(hdc_mem), 0, 0, w, h,
            wintypes.HDC(hdc_screen), vb["left"], vb["top"], SRCCOPY,
        )
        if not ok:
            return ImageGrab.grab(all_screens=True), vb, "desktop_gdi_bitblt"
        raw = ctypes.string_at(bits_ptr.value, w * h * 4)
        img = Image.frombuffer("RGBA", (w, h), raw, "raw", "BGRA", 0, 1).convert("RGB")
        return img, vb, "desktop_gdi_bitblt"
    finally:
        if old_obj and hdc_mem:
            gdi32.SelectObject(wintypes.HDC(hdc_mem), wintypes.HGDIOBJ(old_obj))
        if hbmp:
            gdi32.DeleteObject(wintypes.HGDIOBJ(hbmp))
        if hdc_mem:
            gdi32.DeleteDC(wintypes.HDC(hdc_mem))
        if hdc_screen:
            user32.ReleaseDC(None, wintypes.HDC(hdc_screen))


_SOM_FONT_CACHE: ImageFont.FreeTypeFont | ImageFont.ImageFont | None = None


def get_som_font() -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    global _SOM_FONT_CACHE
    if _SOM_FONT_CACHE is None:
        try:
            fonts_dir = Path(os.environ.get("WINDIR") or r"C:\Windows") / "Fonts"
            font_p = fonts_dir / "segoeuib.ttf"
            if not font_p.is_file():
                font_p = fonts_dir / "arial.ttf"
            _SOM_FONT_CACHE = ImageFont.truetype(str(font_p), 11) if font_p.is_file() else ImageFont.load_default()
        except Exception:
            _SOM_FONT_CACHE = ImageFont.load_default()
    return _SOM_FONT_CACHE


# Hardware SendInput Pointer & Keyboard
def send_mouse_move_abs(tx: int, ty: int) -> None:
    x, y = int(tx), int(ty)
    if IS_LOW_INTEGRITY or current_desktop_name().lower() != "default":
        user32.SetCursorPos(x, y)
        return
    vb = virtual_bounds()
    vw = max(1, vb["width"])
    vh = max(1, vb["height"])
    nx = max(0, min(65535, int(((x - vb["left"]) + 0.5) * 65536.0 / vw)))
    ny = max(0, min(65535, int(((y - vb["top"]) + 0.5) * 65536.0 / vh)))
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.union.mi = MOUSEINPUT(
        nx, ny, 0,
        wintypes.DWORD(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK),
        0, _CUA_INPUT_TAG,
    )
    if user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)) != 1:
        user32.SetCursorPos(x, y)


def smooth_move_to(
    tx: int,
    ty: int,
    duration: float | None = None,
    *,
    in_batch: bool = False,
    check_abort: bool = True,
    on_esc_abort: Callable[[], None] | None = None,
) -> None:
    if IS_LOW_INTEGRITY or current_desktop_name().lower() != "default":
        user32.SetCursorPos(int(tx), int(ty))
        return
    sx, sy = get_cursor_pos()
    dx, dy = float(tx - sx), float(ty - sy)
    dist = math.hypot(dx, dy)
    if dist < 2.0 or (duration is not None and duration <= 0.004):
        send_mouse_move_abs(int(tx), int(ty))
        return
    if duration is None:
        if in_batch:
            dur = max(0.012, min(0.026, 0.012 + (min(dist, 1400.0) / 1400.0) * 0.014))
        else:
            dur = max(0.020, min(0.046, 0.020 + (min(dist, 1400.0) / 1400.0) * 0.026))
    else:
        dur = max(0.008, min(float(duration), 2.0))

    steps = max(3, int(round(dur * 120.0)))
    dt = dur / steps
    enter_1ms_timer()
    t0 = time.perf_counter()
    try:
        for i in range(1, steps + 1):
            if check_abort and is_escape_pressed():
                if on_esc_abort is not None:
                    on_esc_abort()
                raise RuntimeError("emergency_stop: Physical Escape key detected during cursor glide.")
            t = i / steps
            u = t ** 0.82
            s = u * u * u * (10.0 - 15.0 * u + 6.0 * u * u)
            send_mouse_move_abs(int(round(sx + dx * s)), int(round(sy + dy * s)))
            target_t = t0 + i * dt
            rem = target_t - time.perf_counter()
            if rem > 0.0018:
                time.sleep(rem - 0.0012)
            while time.perf_counter() < target_t:
                pass
        send_mouse_move_abs(int(tx), int(ty))
    finally:
        exit_1ms_timer()


_MOUSE_BUTTON_FLAGS: dict[str, tuple[int, int]] = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


def send_mouse_button(button: str = "left", *, down: bool) -> None:
    pair = _MOUSE_BUTTON_FLAGS.get(button.lower())
    if pair is None:
        raise ValueError(f"Unsupported mouse button: {button!r}")
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.union.mi = MOUSEINPUT(0, 0, 0, wintypes.DWORD(pair[0] if down else pair[1]), 0, _CUA_INPUT_TAG)
    sent = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    if sent != 1 and not IS_LOW_INTEGRITY:
        raise RuntimeError("SendInput mouse button event failed (blocked by UIPI or inactive desktop).")


def send_mouse_click(button: str = "left", clicks: int = 1, interval: float = 0.0, *, x: int | None = None, y: int | None = None) -> None:
    pair = _MOUSE_BUTTON_FLAGS.get(button.lower())
    if pair is None:
        raise ValueError(f"Unsupported mouse button: {button!r}")
    down_flag, up_flag = pair
    count = max(1, int(clicks))
    for i in range(count):
        inputs = (INPUT * 2)()
        inputs[0].type = INPUT_MOUSE
        inputs[0].union.mi = MOUSEINPUT(0, 0, 0, down_flag, 0, _CUA_INPUT_TAG)
        inputs[1].type = INPUT_MOUSE
        inputs[1].union.mi = MOUSEINPUT(0, 0, 0, up_flag, 0, _CUA_INPUT_TAG)
        sent = user32.SendInput(2, ctypes.byref(inputs), ctypes.sizeof(INPUT))
        if sent != 2 and not IS_LOW_INTEGRITY:
            raise RuntimeError(f"SendInput mouse click failed (sent {sent}/2 events).")
        if i + 1 < count and interval > 0:
            time.sleep(interval)


def send_mouse_drag(
    samples: list[tuple[int, int]],
    *,
    button: str = "left",
    duration: float = 0.1,
    hold_ms: int = 0,
    on_esc_abort: Callable[[], None] | None = None,
) -> None:
    if not samples:
        return
    sx0, sy0 = samples[0]
    sx_end, sy_end = samples[-1]
    if IS_LOW_INTEGRITY or current_desktop_name().lower() != "default":
        user32.SetCursorPos(sx_end, sy_end)
        return
    enter_1ms_timer()
    try:
        send_mouse_move_abs(sx0, sy0)
        send_mouse_button(button, down=True)
        try:
            time.sleep(0.004)
            n_samp = max(1, len(samples))
            dt = duration / n_samp
            t0 = time.perf_counter()
            for idx, (ix, iy) in enumerate(samples, start=1):
                if is_escape_pressed():
                    if on_esc_abort is not None:
                        on_esc_abort()
                    raise RuntimeError("emergency_stop: Physical Escape key detected during drag path; grants revoked.")
                send_mouse_move_abs(ix, iy)
                target_t = t0 + idx * dt
                rem = target_t - time.perf_counter()
                if rem > 0.0018:
                    time.sleep(rem - 0.0012)
                while time.perf_counter() < target_t:
                    pass
            send_mouse_move_abs(sx_end, sy_end)
            time.sleep(max(0.004, hold_ms / 1000.0))
        finally:
            send_mouse_button(button, down=False)
    finally:
        exit_1ms_timer()


def send_mouse_wheel(clicks: int, *, horizontal: bool = False, x: int | None = None, y: int | None = None) -> None:
    steps = max(-50, min(int(clicks), 50))
    if steps == 0:
        return
    inp = INPUT()
    inp.type = INPUT_MOUSE
    flag = MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL
    inp.union.mi = MOUSEINPUT(0, 0, wintypes.DWORD((steps * WHEEL_DELTA) & 0xFFFFFFFF), flag, 0, _CUA_INPUT_TAG)
    sent = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    if sent != 1 and not IS_LOW_INTEGRITY and current_desktop_name().lower() == "default":
        raise RuntimeError("SendInput mouse wheel event failed.")


_VK_NAME_MAP: dict[str, int] = {
    "backspace": 0x08, "back": 0x08,
    "tab": 0x09,
    "enter": 0x0D, "return": 0x0D,
    "shift": 0x10, "shiftleft": 0xA0, "shiftright": 0xA1,
    "ctrl": 0x11, "control": 0x11, "ctrlleft": 0xA2, "ctrlright": 0xA3,
    "alt": 0x12, "menu": 0x12, "altleft": 0xA4, "altright": 0xA5,
    "pause": 0x13, "capslock": 0x14,
    "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "spacebar": 0x20,
    "pageup": 0x21, "pgup": 0x21,
    "pagedown": 0x22, "pgdn": 0x22,
    "end": 0x23, "home": 0x24,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "printscreen": 0x2C, "prtsc": 0x2C,
    "insert": 0x2D, "ins": 0x2D,
    "delete": 0x2E, "del": 0x2E,
    "multiply": 0x6A, "add": 0x6B, "plus": 0xBB, "+": 0xBB,
    "subtract": 0x6D, "minus": 0xBD, "-": 0xBD,
    "decimal": 0x6E, "divide": 0x6F,
    "numlock": 0x90, "scrolllock": 0x91,
    **{f"f{n}": 0x6F + n for n in range(1, 25)},
}
_EXTENDED_VKS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x6F, 0xA3, 0xA5}


def _resolve_vk_token(tok: str, *, hkl: int = 0) -> tuple[int, bool, int]:
    low = (tok or "").strip().lower()
    if low == "+" and hkl:
        vk_scan = int(user32.VkKeyScanExW("+", wintypes.HKL(hkl)))
        if vk_scan != -1:
            vk = vk_scan & 0xFF
            return vk, vk in _EXTENDED_VKS, (vk_scan >> 8) & 0x07
    if low in _VK_NAME_MAP:
        vk = _VK_NAME_MAP[low]
        req_shift = 1 if low == "+" else 0
        return vk, vk in _EXTENDED_VKS, req_shift
    if len(tok) == 1:
        if "a" <= low <= "z":
            return ord(low.upper()), False, 0
        if "0" <= low <= "9":
            return ord(low), False, 0
        vk_scan = int(user32.VkKeyScanExW(tok, wintypes.HKL(hkl))) if hkl else int(user32.VkKeyScanW(tok))
        if vk_scan != -1:
            vk = vk_scan & 0xFF
            return vk, vk in _EXTENDED_VKS, (vk_scan >> 8) & 0x07
    raise ValueError(f"Unsupported key token: {tok!r}")


def send_key_chord(*keys: str, target_hwnd: int = 0) -> None:
    hkl = 0
    if target_hwnd > 0:
        tid = user32.GetWindowThreadProcessId(wintypes.HWND(target_hwnd), None)
        if tid:
            hkl = int(user32.GetKeyboardLayout(tid) or 0)

    resolved = [_resolve_vk_token(k, hkl=hkl) for k in keys]
    present_vks = {vk for vk, _, _ in resolved}
    seq: list[tuple[int, bool]] = []
    if len(resolved) == 1:
        vk0, ext0, smask = resolved[0]
        if (smask & 1) and 0x10 not in present_vks:
            seq.append((0x10, False))
        if (smask & 2) and 0x11 not in present_vks:
            seq.append((0x11, False))
        if (smask & 4) and 0x12 not in present_vks:
            seq.append((0x12, False))
        seq.append((vk0, ext0))
    else:
        for vk, ext, _ in resolved:
            seq.append((vk, ext))

    n = len(seq)
    inputs = (INPUT * (n * 2))()
    idx = 0
    for vk, ext in seq:
        scan = int((user32.MapVirtualKeyExW(vk, 0, wintypes.HKL(hkl)) if hkl else user32.MapVirtualKeyW(vk, 0)) or 0)
        flags = KEYEVENTF_EXTENDEDKEY if ext else 0
        inputs[idx].type = INPUT_KEYBOARD
        inputs[idx].union.ki = KEYBDINPUT(vk, scan, flags, 0, _CUA_INPUT_TAG)
        idx += 1
    for vk, ext in reversed(seq):
        scan = int((user32.MapVirtualKeyExW(vk, 0, wintypes.HKL(hkl)) if hkl else user32.MapVirtualKeyW(vk, 0)) or 0)
        flags = KEYEVENTF_KEYUP | (KEYEVENTF_EXTENDEDKEY if ext else 0)
        inputs[idx].type = INPUT_KEYBOARD
        inputs[idx].union.ki = KEYBDINPUT(vk, scan, flags, 0, _CUA_INPUT_TAG)
        idx += 1
    sent = user32.SendInput(len(inputs), ctypes.byref(inputs), ctypes.sizeof(INPUT))
    if sent != len(inputs) and not IS_LOW_INTEGRITY and current_desktop_name().lower() == "default":
        raise RuntimeError(f"SendInput key chord failed (sent {sent}/{len(inputs)} events)")


def send_unicode_units(units: list[int]) -> None:
    if not units:
        return
    inputs = (INPUT * (len(units) * 2))()
    idx = 0
    for code in units:
        inputs[idx].type = INPUT_KEYBOARD
        inputs[idx].union.ki = KEYBDINPUT(0, code, KEYEVENTF_UNICODE, 0, _CUA_INPUT_TAG)
        idx += 1
        inputs[idx].type = INPUT_KEYBOARD
        inputs[idx].union.ki = KEYBDINPUT(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, _CUA_INPUT_TAG)
        idx += 1
    sent = user32.SendInput(len(inputs), ctypes.byref(inputs), ctypes.sizeof(INPUT))
    if sent != len(inputs) and not IS_LOW_INTEGRITY and current_desktop_name().lower() == "default":
        raise RuntimeError(f"SendInput failed (sent {sent}/{len(inputs)} events)")


def send_unicode_char(ch: str) -> None:
    if ch == "\r":
        return
    if ch == "\n":
        send_key_chord("enter")
        return
    if ch == "\t":
        send_key_chord("tab")
        return
    encoded = ch.encode("utf-16-le")
    units = [int.from_bytes(encoded[i : i + 2], "little") for i in range(0, len(encoded), 2)]
    send_unicode_units(units)


def send_unicode_chunk(chunk: str) -> None:
    buf: list[int] = []
    for ch in chunk:
        if ch == "\r":
            continue
        if ch in ("\n", "\t"):
            if buf:
                send_unicode_units(buf)
                buf.clear()
            send_key_chord("enter" if ch == "\n" else "tab")
            continue
        enc = ch.encode("utf-16-le")
        for i in range(0, len(enc), 2):
            buf.append(int.from_bytes(enc[i : i + 2], "little"))
    if buf:
        send_unicode_units(buf)


# App Discovery (Registry App Paths / Uninstall / WindowsApps / StartApps / PATH + PE subsystem)
_APP_INDEX_CACHE: tuple[float, dict[str, list[str]]] = (0.0, {})
_START_APPS_CACHE: tuple[float, list[tuple[str, str]]] = (0.0, [])


def _norm_app_key(s: str) -> str:
    return re.sub(r"[\W_]+", "", (s or "").lower().removesuffix(".exe"), flags=re.UNICODE)


def _is_unc_or_device_path(s: str) -> bool:
    norm = (s or "").strip(" \"'").replace("/", "\\")
    return norm.startswith("\\\\")


def _canonical_proc_or_name(raw: str) -> str:
    cleaned = (raw or "").strip(" \"'")
    if cleaned and not _is_unc_or_device_path(cleaned) and ("\\" in cleaned or "/" in cleaned or ":" in cleaned):
        try:
            p = Path(os.path.expandvars(cleaned))
            if p.exists():
                return p.resolve().name
        except OSError:
            pass
    return cleaned


def _get_pe_subsystem(exe_path: Path) -> int:
    try:
        with exe_path.open("rb") as fh:
            if fh.read(2) != b"MZ":
                return 0
            fh.seek(0x3C)
            pe_off = struct.unpack("<I", fh.read(4))[0]
            fh.seek(pe_off)
            if fh.read(4) != b"PE\x00\x00":
                return 0
            fh.seek(pe_off + 24)
            magic = struct.unpack("<H", fh.read(2))[0]
            if magic not in (0x10B, 0x20B):
                return 0
            fh.seek(pe_off + 24 + 68)
            return int(struct.unpack("<H", fh.read(2))[0])
    except Exception:
        return 0


def _clean_reg_exe(raw: str) -> Path | None:
    s = (raw or "").strip()
    if not s:
        return None
    if s.startswith('"'):
        end = s.find('"', 1)
        if end > 1:
            s = s[1:end]
    else:
        idx = s.lower().find(".exe")
        if idx >= 0:
            s = s[: idx + 4]
    expanded = os.path.expandvars(s.strip(" \"'"))
    if not expanded or _is_unc_or_device_path(expanded):
        return None
    p = Path(expanded)
    if p.is_file() and p.suffix.lower() == ".exe":
        try:
            return p.resolve()
        except OSError:
            return p
    return None


def _build_registry_and_fs_app_index() -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}

    def _add(label: str, cmd: list[str]) -> None:
        key = _norm_app_key(label)
        if key and key not in index:
            index[key] = cmd

    app_paths_sub = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, app_paths_sub) as root:
                n_sub, _, _ = winreg.QueryInfoKey(root)
                for i in range(n_sub):
                    try:
                        sub_name = winreg.EnumKey(root, i)
                        with winreg.OpenKey(root, sub_name) as sk:
                            val, _ = winreg.QueryValueEx(sk, "")
                        exe_p = _clean_reg_exe(str(val))
                        if exe_p is not None:
                            _add(sub_name, [str(exe_p)])
                            _add(exe_p.stem, [str(exe_p)])
                    except OSError:
                        continue
        except OSError:
            continue

    local_app_str = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_str:
        local_app = Path(local_app_str)
        win_apps = local_app / "Microsoft" / "WindowsApps"
        if win_apps.is_dir():
            try:
                for item in win_apps.iterdir():
                    if item.suffix.lower() == ".exe":
                        _add(item.stem, [str(item)])
            except OSError:
                pass
        if local_app.is_dir():
            try:
                for folder in local_app.iterdir():
                    if not folder.is_dir():
                        continue
                    app_dirs = sorted(folder.glob("app-*"), reverse=True)
                    for ad in app_dirs[:1]:
                        exes = [
                            c for c in ad.glob("*.exe")
                            if c.name.lower() not in (
                                "update.exe", "squirrel.exe", "unins000.exe",
                                "crashpad_handler.exe", "chrome_pwa_launcher.exe", "notification_helper.exe",
                            )
                        ]
                        if not exes:
                            continue
                        fk = _norm_app_key(folder.name)
                        exes.sort(key=lambda c: (0 if _norm_app_key(c.stem) == fk else 1, 0 if _get_pe_subsystem(c) == 2 else 1, len(c.name)))
                        best_sq = exes[0]
                        _add(folder.name, [str(best_sq)])
                        _add(best_sq.stem, [str(best_sq)])
            except OSError:
                pass

    uninst_paths = (
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    )
    for hive, sub in uninst_paths:
        try:
            with winreg.OpenKey(hive, sub) as root:
                n_sub, _, _ = winreg.QueryInfoKey(root)
                for i in range(n_sub):
                    try:
                        sk_name = winreg.EnumKey(root, i)
                        with winreg.OpenKey(root, sk_name) as sk:
                            try:
                                disp, _ = winreg.QueryValueEx(sk, "DisplayName")
                            except OSError:
                                continue
                            exe_p: Path | None = None
                            try:
                                icon, _ = winreg.QueryValueEx(sk, "DisplayIcon")
                                exe_p = _clean_reg_exe(str(icon))
                            except OSError:
                                pass
                            if exe_p is None:
                                try:
                                    iloc, _ = winreg.QueryValueEx(sk, "InstallLocation")
                                    iloc_exp = os.path.expandvars(str(iloc).strip(" \"'"))
                                    if iloc_exp and not _is_unc_or_device_path(iloc_exp):
                                        ip = Path(iloc_exp)
                                        if ip.is_dir():
                                            d_key = _norm_app_key(str(disp))
                                            for c in ip.glob("*.exe"):
                                                if "unins" in c.name.lower() or "setup" in c.name.lower():
                                                    continue
                                                if _norm_app_key(c.stem) in d_key or d_key in _norm_app_key(c.stem):
                                                    exe_p = c
                                                    break
                                except OSError:
                                    pass
                            if exe_p is not None:
                                _add(str(disp), [str(exe_p)])
                                _add(exe_p.stem, [str(exe_p)])
                    except OSError:
                        continue
        except OSError:
            continue

    return index


def _get_start_apps_index() -> list[tuple[str, str]]:
    global _START_APPS_CACHE
    now = time.monotonic()
    if _START_APPS_CACHE[1] and (now - _START_APPS_CACHE[0]) < 120.0:
        return _START_APPS_CACHE[1]
    flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    entries: list[tuple[str, str]] = []
    try:
        ps_cmd = "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); Get-StartApps | ConvertTo-Json -Compress"
        res = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True, text=True, encoding="utf-8", timeout=4.0, creationflags=flags,
        )
        if res.returncode == 0 and res.stdout.strip():
            data = json.loads(res.stdout)
            if isinstance(data, dict):
                data = [data]
            for item in data or []:
                nm = str(item.get("Name") or "").strip()
                aid = str(item.get("AppID") or "").strip()
                if nm and aid:
                    entries.append((nm, aid))
    except Exception:
        pass
    _START_APPS_CACHE = (now, entries)
    return entries


_VERSIONED_RUNTIME_RE = re.compile(
    r"^(?:pythonw?|py|node|deno|bun|perl|ruby|php|lua|bash|zsh|sh|fish|pwsh|ssh|scp|sftp)[\d._-]*(\.exe)?$",
    re.I,
)


def _is_url_arg(s: str) -> bool:
    low = (s or "").strip(" \"'").lower()
    return low.startswith("http://") or low.startswith("https://") or low.startswith("mailto:")


def _is_blocked_runtime_token(
    raw_tok: str,
    *,
    classify_policy_fn: Callable[[str], str],
    norm_proc_fn: Callable[[str], str],
    blocked_runtimes: set[str],
) -> bool:
    canon = _canonical_proc_or_name(raw_tok)
    cleaned = canon.strip(" \"'")
    leaf = cleaned.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    for cand in (cleaned, leaf):
        if not cand:
            continue
        if classify_policy_fn(cand) == "blocked":
            return True
        if norm_proc_fn(cand) in blocked_runtimes:
            return True
        if _VERSIONED_RUNTIME_RE.match(cand):
            return True
    return False


def is_blocked_launch_target(
    raw_app: str,
    cmd: list[str],
    extra_args: list[str] | None,
    *,
    classify_policy_fn: Callable[[str], str],
    norm_proc_fn: Callable[[str], str],
    blocked_runtimes: set[str],
    blocked_aumid_fragments: tuple[str, ...],
) -> str | None:
    if _is_blocked_runtime_token(
        raw_app,
        classify_policy_fn=classify_policy_fn,
        norm_proc_fn=norm_proc_fn,
        blocked_runtimes=blocked_runtimes,
    ):
        return raw_app

    for arg in (extra_args or []):
        s_arg = str(arg or "").strip(" \"'")
        if not s_arg or _is_url_arg(s_arg):
            continue
        parts = list(s_arg.split("=", 1)) if "=" in s_arg else [s_arg]
        for part in parts:
            p_str = part.strip(" \"'")
            if not p_str or _is_url_arg(p_str):
                continue
            if _is_blocked_runtime_token(
                p_str,
                classify_policy_fn=classify_policy_fn,
                norm_proc_fn=norm_proc_fn,
                blocked_runtimes=blocked_runtimes,
            ):
                return arg

    is_uwp_appsfolder = (
        len(cmd) == 2
        and Path(cmd[0]).name.lower() == "explorer.exe"
        and str(cmd[1]).lower().startswith("shell:appsfolder\\")
    )
    if is_uwp_appsfolder:
        aumid_raw = str(cmd[1]).split("\\", 1)[1]
        aumid = aumid_raw.lower()
        if not aumid:
            return cmd[1]
        for frag in blocked_aumid_fragments:
            if frag in aumid:
                return aumid
        for seg in re.split(r"[\\/!]+", aumid_raw):
            if seg and _is_blocked_runtime_token(
                seg,
                classify_policy_fn=classify_policy_fn,
                norm_proc_fn=norm_proc_fn,
                blocked_runtimes=blocked_runtimes,
            ):
                return seg
        if extra_args:
            return str(extra_args[0])
        return None

    cmd0_canon = _canonical_proc_or_name(cmd[0])
    if _is_blocked_runtime_token(
        cmd[0],
        classify_policy_fn=classify_policy_fn,
        norm_proc_fn=norm_proc_fn,
        blocked_runtimes=blocked_runtimes,
    ):
        return Path(cmd0_canon).name
    return None


def resolve_installed_app(
    app: str,
    *,
    classify_policy_fn: Callable[[str], str],
) -> tuple[list[str] | None, tuple[str, ...]]:
    global _APP_INDEX_CACHE
    raw = app.strip()
    if not raw or _is_unc_or_device_path(raw):
        return None, ()

    exp_raw = os.path.expandvars(raw)
    if _is_unc_or_device_path(exp_raw):
        return None, ()
    exe = Path(exp_raw)
    if exe.is_file() and exe.suffix.lower() == ".exe":
        try:
            exe = exe.resolve()
        except OSError:
            pass
        return [str(exe)], (exe.name.lower(),)

    key = _norm_app_key(raw)
    if not key:
        return None, ()

    now = time.monotonic()
    if not _APP_INDEX_CACHE[1] or (now - _APP_INDEX_CACHE[0]) >= 60.0:
        _APP_INDEX_CACHE = (now, _build_registry_and_fs_app_index())
    idx = _APP_INDEX_CACHE[1]

    if key in idx:
        cmd = idx[key]
        leaf = Path(cmd[0]).name.lower()
        return cmd, (leaf,) if leaf.endswith(".exe") else ()

    raw_words = [w for w in re.findall(r"[^\W_]+", raw.lower(), flags=re.UNICODE) if w]
    best_cmd: list[str] | None = None
    best_score = 0
    for k, cmd in idx.items():
        if len(k) < 3:
            continue
        p_c = Path(cmd[0])
        gui_bonus = 25 if _get_pe_subsystem(p_c) == 2 else 0
        if k.startswith(key) or key.startswith(k):
            sc = 100 - abs(len(k) - len(key)) + gui_bonus
        elif key in k:
            sc = 60 - abs(len(k) - len(key)) + gui_bonus
        elif len(raw_words) >= 2 and all(w in k for w in raw_words):
            sc = 75 - abs(len(k) - len(key)) + gui_bonus
        else:
            continue
        if sc > best_score:
            best_score = sc
            best_cmd = cmd
    if best_cmd is not None:
        leaf = Path(best_cmd[0]).name.lower()
        return best_cmd, (leaf,) if leaf.endswith(".exe") else ()

    for nm, aid in _get_start_apps_index():
        if classify_policy_fn(nm) == "blocked" or _VERSIONED_RUNTIME_RE.match(nm.strip()):
            continue
        if any(
            classify_policy_fn(seg) == "blocked" or _VERSIONED_RUNTIME_RE.match(seg.strip())
            for seg in re.split(r"[\\/!]+", aid)
            if seg.strip()
        ):
            continue
        nk = _norm_app_key(nm)
        ak = _norm_app_key(aid)
        words_hit = len(raw_words) >= 2 and all(w in nk for w in raw_words)
        if key == nk or key == ak or words_hit or (len(key) >= 3 and (nk.startswith(key) or key in nk)):
            if aid.lower().endswith(".exe") and not _is_unc_or_device_path(aid) and Path(aid).is_file():
                resolved_aid = Path(aid)
                try:
                    resolved_aid = resolved_aid.resolve()
                except OSError:
                    pass
                if classify_policy_fn(resolved_aid.name) == "blocked" or _VERSIONED_RUNTIME_RE.match(resolved_aid.name):
                    continue
                if _get_pe_subsystem(resolved_aid) != 3:
                    return [str(resolved_aid)], (resolved_aid.name.lower(),)
            return ["explorer.exe", f"shell:AppsFolder\\{aid}"], (f"{key}.exe", key)

    low = raw.lower()
    stem = low[:-4] if (low.endswith(".exe") and "\\" not in low and "/" not in low) else low
    cui_fallback: tuple[list[str], tuple[str, ...]] | None = None
    for probe_name in (f"{stem}.exe", raw):
        which_hit = shutil.which(probe_name)
        if which_hit and not _is_unc_or_device_path(which_hit) and Path(which_hit).is_file():
            p = Path(which_hit)
            try:
                p = p.resolve()
            except OSError:
                pass
            if p.suffix.lower() != ".exe" or classify_policy_fn(p.name) == "blocked" or _VERSIONED_RUNTIME_RE.match(p.name):
                continue
            subsys = _get_pe_subsystem(p)
            if subsys != 3:
                return [str(p)], (p.name.lower(),)
            if cui_fallback is None:
                cui_fallback = ([str(p)], (p.name.lower(),))
    if cui_fallback is not None:
        return cui_fallback
    return None, ()


def spawn_detached_app(cmd: list[str], extra_args: list[str]) -> subprocess.Popen[Any]:
    spawn_flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB
    try:
        return subprocess.Popen(
            [*cmd, *extra_args],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, creationflags=spawn_flags,
        )
    except OSError:
        return subprocess.Popen(
            [*cmd, *extra_args],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        )


# UI Automation Bridge (C# fast bridge + PowerShell fallback + Win32 child controls)
UIA_DAEMON_PS = r"""
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
try {
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class CuaInlineDaemonDpi {
    [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr value);
    [DllImport("shcore.dll")] public static extern int SetProcessDpiAwareness(int value);
    public static void Init() {
        try { if (SetProcessDpiAwarenessContext(new IntPtr(-4))) return; } catch {}
        try { SetProcessDpiAwareness(2); } catch {}
    }
}
"@
    [CuaInlineDaemonDpi]::Init()
} catch {}
Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes,WindowsBase,System.Web.Extensions
$json = New-Object System.Web.Script.Serialization.JavaScriptSerializer
$json.MaxJsonLength = 16777216
$keepSet = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
foreach ($t in @('Button','Edit','Document','ComboBox','CheckBox','RadioButton','MenuItem','TabItem','ListItem','Hyperlink','TreeItem','Slider','SplitButton','DataItem')) { [void]$keepSet.Add($t) }
$cacheReq = New-Object System.Windows.Automation.CacheRequest
$cacheReq.AutomationElementMode = [System.Windows.Automation.AutomationElementMode]::Full
$cacheReq.TreeFilter = [System.Windows.Automation.Automation]::ControlViewCondition
$cacheReq.TreeScope = ([System.Windows.Automation.TreeScope]::Element -bor [System.Windows.Automation.TreeScope]::Children)
foreach ($prop in @(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    [System.Windows.Automation.AutomationElement]::NameProperty,
    [System.Windows.Automation.AutomationElement]::AutomationIdProperty,
    [System.Windows.Automation.AutomationElement]::BoundingRectangleProperty,
    [System.Windows.Automation.AutomationElement]::NativeWindowHandleProperty,
    [System.Windows.Automation.AutomationElement]::IsEnabledProperty,
    [System.Windows.Automation.AutomationElement]::IsOffscreenProperty,
    [System.Windows.Automation.AutomationElement]::HasKeyboardFocusProperty,
    [System.Windows.Automation.AutomationElement]::IsPasswordProperty
)) { $cacheReq.Add($prop) }
[Console]::Out.WriteLine('{"pong":true}')
[Console]::Out.Flush()
while ($null -ne ($line = [Console]::In.ReadLine())) {
    $line = $line.Trim()
    if ($line.Length -eq 0) { continue }
    try {
        $req = $json.DeserializeObject($line)
        if ($req.ContainsKey('ping')) { [Console]::Out.WriteLine('{"pong":true}'); [Console]::Out.Flush(); continue }
        $hwnd = [int64]$req['hwnd']
        $maxElements = if ($req.ContainsKey('maxElements')) { [int]$req['maxElements'] } else { 80 }
        $setValueIndex = if ($req.ContainsKey('setValueIndex')) { [int]$req['setValueIndex'] } else { -1 }
        $valueText = if ($req.ContainsKey('valueText') -and $null -ne $req['valueText']) { [string]$req['valueText'] } else { '' }
        $invokeIndex = if ($req.ContainsKey('invokeIndex')) { [int]$req['invokeIndex'] } else { -1 }
        $cacheReq.Push()
        try { $root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$hwnd) } finally { $cacheReq.Pop() }
        if ($null -eq $root) { throw "Cannot bind AutomationElement for HWND $hwnd" }
        $focusedStr = $null
        $queue = New-Object System.Collections.Generic.Queue[object]
        $queue.Enqueue(@{ el = $root; depth = 0 })
        $items = New-Object System.Collections.Generic.List[object]
        $earlyResp = $null
        while ($queue.Count -gt 0 -and $items.Count -lt $maxElements -and $null -eq $earlyResp) {
            $cur = $queue.Dequeue(); $el = $cur.el; $depth = [int]$cur.depth
            if ($depth -gt 0) {
                try {
                    $c = $el.Cached
                    if ($null -ne $c.ControlType) {
                        $ct = $c.ControlType.ProgrammaticName.Replace('ControlType.','')
                        $name = [string]$c.Name
                        if ($null -eq $focusedStr -and $c.HasKeyboardFocus) { $focusedStr = "$ct '$name'" }
                        $r = $c.BoundingRectangle
                        $rectOk = (-not $r.IsEmpty) -and (-not [double]::IsNaN($r.Left)) -and (-not [double]::IsInfinity($r.Left)) -and (-not [double]::IsNaN($r.Top)) -and (-not [double]::IsInfinity($r.Top)) -and (-not [double]::IsNaN($r.Width)) -and (-not [double]::IsInfinity($r.Width)) -and (-not [double]::IsNaN($r.Height)) -and (-not [double]::IsInfinity($r.Height))
                        if (-not $c.IsOffscreen -and $rectOk -and $r.Width -gt 2 -and $r.Height -gt 2) {
                            $aid = [string]$c.AutomationId
                            if ($keepSet.Contains($ct) -or ($name.Length -gt 0 -and $depth -le 4)) {
                                $isPassword = $false
                                try { $isPassword = [bool]$c.IsPassword } catch { $isPassword = $true }
                                $val = $null; $vpObj = $null
                                $checkVal = ($setValueIndex -eq $items.Count) -or ($ct -in @('Edit','ComboBox','Document','Slider'))
                                if ($checkVal -and $el.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$vpObj)) {
                                    if (-not $isPassword) {
                                        try { $val = ([System.Windows.Automation.ValuePattern]$vpObj).Current.Value } catch {}
                                        if ($null -ne $val -and $val.Length -gt 120) { $val = $val.Substring(0, 120) + '...' }
                                    }
                                }
                                $idx = $items.Count; $cx = [int]($r.Left + $r.Width / 2); $cy = [int]($r.Top + $r.Height / 2)
                                if ($setValueIndex -eq $idx) {
                                    if ($null -ne $vpObj) { ([System.Windows.Automation.ValuePattern]$vpObj).SetValue($valueText); $earlyResp = @{ action = 'set_value'; method = 'ValuePattern'; index = $idx; name = $name } }
                                    else { $earlyResp = @{ action = 'set_value'; method = 'fallback_needed'; index = $idx; cx = $cx; cy = $cy } }
                                    break
                                }
                                if ($invokeIndex -eq $idx) {
                                    $ipObj = $null
                                    if ($el.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$ipObj)) { ([System.Windows.Automation.InvokePattern]$ipObj).Invoke(); $earlyResp = @{ action = 'invoke'; method = 'InvokePattern'; index = $idx; name = $name } }
                                    else { $earlyResp = @{ action = 'invoke'; method = 'fallback_click'; index = $idx; cx = $cx; cy = $cy } }
                                    break
                                }
                                $items.Add(@{ index = $idx; depth = $depth; controlType = $ct; name = $name; automationId = $aid; nativeHwnd = [int64]$c.NativeWindowHandle; enabled = [bool]$c.IsEnabled; value = $val; bounds = @{ left = [int]$r.Left; top = [int]$r.Top; width = [int]$r.Width; height = [int]$r.Height; cx = $cx; cy = $cy } })
                            }
                        }
                    }
                } catch {}
            }
            if ($depth -lt 8 -and $null -eq $earlyResp) {
                try {
                    $children = $el.CachedChildren
                    if ($null -ne $children) { foreach ($ch in $children) { try { $uch = $ch.GetUpdatedCache($cacheReq); $queue.Enqueue(@{ el = $uch; depth = ($depth + 1) }) } catch {} } }
                } catch {}
            }
        }
        $outObj = if ($null -ne $earlyResp) { $earlyResp } else { @{ focusedElement = $focusedStr; count = $items.Count; elements = $items } }
        [Console]::Out.WriteLine($json.Serialize($outObj)); [Console]::Out.Flush()
    } catch { [Console]::Out.WriteLine($json.Serialize(@{ error = $_.Exception.Message })); [Console]::Out.Flush() }
}
"""

UIA_PS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
try {
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class CuaColdDpi {
    [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr value);
    [DllImport("shcore.dll")] public static extern int SetProcessDpiAwareness(int value);
    public static void Init() {
        try { if (SetProcessDpiAwarenessContext(new IntPtr(-4))) return; } catch {}
        try { SetProcessDpiAwareness(2); } catch {}
    }
}
"@
    [CuaColdDpi]::Init()
} catch {}
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]([int64]$Hwnd))
if ($null -eq $root) { throw "Cannot bind UIAutomationElement for HWND $Hwnd" }
$focusedStr = $null
$keepTypes = @('Button','Edit','Document','ComboBox','CheckBox','RadioButton','MenuItem','TabItem','ListItem','Hyperlink','TreeItem','Slider','SplitButton','DataItem')
$walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
$queue = New-Object System.Collections.Generic.Queue[object]
$queue.Enqueue(@{ el = $root; depth = 0 })
$items = New-Object System.Collections.Generic.List[object]
while ($queue.Count -gt 0 -and $items.Count -lt $MaxElements) {
    $cur = $queue.Dequeue()
    $el = $cur.el
    $depth = [int]$cur.depth
    if ($depth -gt 0) {
        try {
            $c = $el.Current
            if ($null -ne $c.ControlType) {
                $ct = $c.ControlType.ProgrammaticName.Replace('ControlType.','')
                $name = [string]$c.Name
                if ($null -eq $focusedStr -and $c.HasKeyboardFocus) { $focusedStr = "$ct '$name'" }
                $r = $c.BoundingRectangle
                $rectOk = (-not $r.IsEmpty) -and (-not [double]::IsNaN($r.Left)) -and (-not [double]::IsInfinity($r.Left)) -and (-not [double]::IsNaN($r.Top)) -and (-not [double]::IsInfinity($r.Top)) -and (-not [double]::IsNaN($r.Width)) -and (-not [double]::IsInfinity($r.Width)) -and (-not [double]::IsNaN($r.Height)) -and (-not [double]::IsInfinity($r.Height))
                if (-not $c.IsOffscreen -and $rectOk -and $r.Width -gt 2 -and $r.Height -gt 2) {
                    $aid = [string]$c.AutomationId
                    if (($ct -in $keepTypes) -or ($name.Length -gt 0 -and $depth -le 4)) {
                        $isPassword = $false
                        try { $isPassword = [bool]$c.IsPassword } catch { $isPassword = $true }
                        $val = $null
                        $vpObj = $null
                        if ($el.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$vpObj)) {
                            if (-not $isPassword) {
                                try { $val = ([System.Windows.Automation.ValuePattern]$vpObj).Current.Value } catch {}
                                if ($null -ne $val -and $val.Length -gt 120) { $val = $val.Substring(0, 120) + "..." }
                            }
                        }
                        $idx = $items.Count
                        if ($SetValueIndex -eq $idx) {
                            if ($null -ne $vpObj) {
                                ([System.Windows.Automation.ValuePattern]$vpObj).SetValue($ValueText)
                                @{ action = "set_value"; method = "ValuePattern"; index = $idx; name = $name } | ConvertTo-Json -Compress
                                exit 0
                            } else {
                                @{ action = "set_value"; method = "fallback_needed"; index = $idx; cx = [int]($r.Left + $r.Width/2); cy = [int]($r.Top + $r.Height/2) } | ConvertTo-Json -Compress
                                exit 0
                            }
                        }
                        if ($InvokeIndex -eq $idx) {
                            $ipObj = $null
                            if ($el.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$ipObj)) {
                                ([System.Windows.Automation.InvokePattern]$ipObj).Invoke()
                                @{ action = "invoke"; method = "InvokePattern"; index = $idx; name = $name } | ConvertTo-Json -Compress
                                exit 0
                            } else {
                                @{ action = "invoke"; method = "fallback_click"; index = $idx; cx = [int]($r.Left + $r.Width/2); cy = [int]($r.Top + $r.Height/2) } | ConvertTo-Json -Compress
                                exit 0
                            }
                        }
                        $items.Add(@{
                            index = $idx; depth = $depth; controlType = $ct; name = $name; automationId = $aid;
                            nativeHwnd = [int64]$c.NativeWindowHandle; enabled = [bool]$c.IsEnabled; value = $val;
                            bounds = @{ left = [int]$r.Left; top = [int]$r.Top; width = [int]$r.Width; height = [int]$r.Height; cx = [int]($r.Left + $r.Width / 2); cy = [int]($r.Top + $r.Height / 2) }
                        })
                    }
                }
            }
        } catch {}
    }
    if ($depth -lt 8) {
        try {
            $child = $walker.GetFirstChild($el)
            while ($null -ne $child) {
                $queue.Enqueue(@{ el = $child; depth = ($depth + 1) })
                $child = $walker.GetNextSibling($child)
            }
        } catch {}
    }
}
@{ focusedElement = $focusedStr; count = $items.Count; elements = $items } | ConvertTo-Json -Depth 5 -Compress
"""


def enum_win32_child_controls(hwnd: int, start_index: int = 0, max_items: int = 80) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def cb(child_hwnd_ptr: int | None, _: int) -> bool:
        if not child_hwnd_ptr:
            return True
        if len(found) >= max_items:
            return False
        child_hwnd = int(child_hwnd_ptr)
        if not user32.IsWindowVisible(wintypes.HWND(child_hwnd)):
            return True
        rect = RECT()
        user32.GetWindowRect(wintypes.HWND(child_hwnd), ctypes.byref(rect))
        w = int(rect.right - rect.left)
        h = int(rect.bottom - rect.top)
        if w <= 4 or h <= 4:
            return True
        cls_buf = ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(wintypes.HWND(child_hwnd), cls_buf, 128)
        cls_name = cls_buf.value or "Control"
        cls_low = cls_name.lower()
        is_pwd = (int(user32.GetWindowLongW(wintypes.HWND(child_hwnd), GWL_STYLE)) & ES_PASSWORD) != 0
        if not is_pwd and "edit" in cls_low:
            pw_ch = ULONG_PTR(0)
            if (
                user32.SendMessageTimeoutW(
                    wintypes.HWND(child_hwnd),
                    EM_GETPASSWORDCHAR,
                    0,
                    None,
                    SMTO_ABORTIFHUNG,
                    50,
                    ctypes.byref(pw_ch),
                )
                != 0
                and pw_ch.value != 0
            ):
                is_pwd = True
        text_val = "" if is_pwd else get_window_title_safe(child_hwnd, timeout_ms=80)
        found.append({
            "index": start_index + len(found),
            "depth": 1,
            "controlType": f"Win32:{cls_name}",
            "name": text_val,
            "automationId": f"hwnd:{child_hwnd}",
            "nativeHwnd": child_hwnd,
            "enabled": bool(user32.IsWindowEnabled(wintypes.HWND(child_hwnd))),
            "value": text_val if text_val else None,
            "bounds": {
                "left": int(rect.left), "top": int(rect.top), "width": w, "height": h,
                "cx": int(rect.left + w // 2), "cy": int(rect.top + h // 2),
            },
        })
        return True

    cb_ref = WNDENUMPROC(cb)
    user32.EnumChildWindows(wintypes.HWND(hwnd), cb_ref, 0)
    return found


_UIA_LOCK = threading.Lock()
_UIA_PROC: subprocess.Popen[str] | None = None
_UIA_OUT_Q: queue.Queue[str] | None = None
_CSC_ATTEMPTED = False
_CSC_EXE_CACHE: Path | None = None


def kill_ui_bridge() -> None:
    global _UIA_PROC, _UIA_OUT_Q
    if _UIA_PROC is not None:
        try:
            _UIA_PROC.kill()
        except Exception:
            pass
    _UIA_PROC = None
    _UIA_OUT_Q = None


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _ensure_compiled_uia_bridge() -> Path | None:
    global _CSC_ATTEMPTED, _CSC_EXE_CACHE
    if _CSC_ATTEMPTED:
        return _CSC_EXE_CACHE
    _CSC_ATTEMPTED = True
    _RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    exe_path = _RUNTIME_DIR / "uia_fast_bridge.exe"
    sha_path = _RUNTIME_DIR / "uia_fast_bridge.sha256"
    cs_sha = _file_sha256(UIA_CS_FILE) if UIA_CS_FILE.is_file() else ""
    if exe_path.is_file() and UIA_CS_FILE.is_file() and sha_path.is_file():
        try:
            recorded = sha_path.read_text(encoding="utf-8").strip()
            exe_sha = _file_sha256(exe_path)
            if recorded == f"{cs_sha}:{exe_sha}":
                _CSC_EXE_CACHE = exe_path
                return exe_path
        except OSError:
            pass
    if IS_LOW_INTEGRITY and "CUA_RUNTIME_DIR" not in os.environ:
        return None
    windir = Path(os.environ.get("WINDIR") or r"C:\Windows")
    fx_dir = windir / "Microsoft.NET" / "Framework64" / "v4.0.30319"
    if not (fx_dir / "csc.exe").is_file():
        fx_dir = windir / "Microsoft.NET" / "Framework" / "v4.0.30319"
    csc = fx_dir / "csc.exe"
    if not csc.is_file() or not UIA_CS_FILE.is_file():
        return None
    tmp_exe = _RUNTIME_DIR / f"uia_fast_bridge.{os.getpid()}.tmp.exe"
    wpf_dir = str(fx_dir / "WPF")
    cmd = [
        str(csc), "/nologo", "/optimize+", "/target:exe", f"/out:{tmp_exe}",
        f"/lib:{wpf_dir}", "/r:UIAutomationClient.dll", "/r:UIAutomationTypes.dll",
        "/r:WindowsBase.dll", "/r:System.Web.Extensions.dll", str(UIA_CS_FILE),
    ]
    flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    env = os.environ.copy()
    env["TMP"] = str(_RUNTIME_DIR)
    env["TEMP"] = str(_RUNTIME_DIR)
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10, creationflags=flags, env=env)
        if res.returncode == 0 and tmp_exe.is_file():
            os.replace(tmp_exe, exe_path)
            try:
                sha_path.write_text(f"{cs_sha}:{_file_sha256(exe_path)}\n", encoding="utf-8")
            except OSError:
                pass
            _CSC_EXE_CACHE = exe_path
            return exe_path
    except Exception:
        pass
    finally:
        try:
            tmp_exe.unlink(missing_ok=True)
        except OSError:
            pass
    return None


def _start_uia_reader(proc: subprocess.Popen[str]) -> queue.Queue[str]:
    q: queue.Queue[str] = queue.Queue()

    def _pump() -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                q.put(line)
        except Exception:
            pass
        finally:
            q.put("")

    threading.Thread(target=_pump, name="cua-uia-reader", daemon=True).start()
    return q


def _drain_uia_line(q: queue.Queue[str], timeout_sec: float) -> str:
    deadline = time.monotonic() + timeout_sec
    while True:
        rem = deadline - time.monotonic()
        if rem <= 0:
            raise TimeoutError("UIA bridge read timed out")
        line = q.get(timeout=rem)
        if not line:
            return ""
        s = line.strip()
        if s.startswith("{"):
            return s


def _get_warm_uia_bridge() -> subprocess.Popen[str] | None:
    global _UIA_PROC, _UIA_OUT_Q
    if _UIA_PROC is not None and _UIA_PROC.poll() is None and _UIA_OUT_Q is not None:
        return _UIA_PROC
    flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))

    compiled_exe = _ensure_compiled_uia_bridge()
    if compiled_exe is not None:
        proc: subprocess.Popen[str] | None = None
        try:
            proc = subprocess.Popen(
                [str(compiled_exe), "--parent-pid", str(os.getpid())],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", creationflags=flags,
            )
            _assign_to_helper_job(proc)
            assert proc.stdin is not None and proc.stdout is not None
            q = _start_uia_reader(proc)
            proc.stdin.write('{"ping":true}\n')
            proc.stdin.flush()
            pong = _drain_uia_line(q, 1.5)
            if '"pong":true' in pong:
                _UIA_PROC = proc
                _UIA_OUT_Q = q
                return _UIA_PROC
            proc.kill()
        except Exception:
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass

    daemon_ps1 = SCRIPT_DIR / "uia_daemon.ps1"
    ps_candidates: list[list[str]] = []
    if daemon_ps1.is_file():
        ps_candidates.append(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(daemon_ps1)]
        )
    ps_candidates.append(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", UIA_DAEMON_PS])

    for ps_cmd in ps_candidates:
        proc = None
        try:
            proc = subprocess.Popen(
                ps_cmd,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", creationflags=flags,
            )
            _assign_to_helper_job(proc)
            assert proc.stdin is not None and proc.stdout is not None
            q = _start_uia_reader(proc)
            pong = _drain_uia_line(q, 4.0)
            if '"pong":true' in pong:
                _UIA_PROC = proc
                _UIA_OUT_Q = q
                return _UIA_PROC
            proc.kill()
        except Exception:
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass
    _UIA_PROC = None
    _UIA_OUT_Q = None
    return None


def _query_uia_raw(hwnd: int, *, max_elements: int, set_value_index: int, value_text: str, invoke_index: int) -> dict[str, Any]:
    global _UIA_PROC, _UIA_OUT_Q
    with _UIA_LOCK:
        modal_blocked = (not user32.IsWindowEnabled(wintypes.HWND(hwnd))) or (
            int(user32.GetWindow(wintypes.HWND(hwnd), GW_ENABLEDPOPUP) or 0) != 0
        )
        if modal_blocked and set_value_index < 0 and invoke_index < 0:
            return {"focusedElement": None, "count": 0, "modalBlocked": True, "elements": []}

        proc = _get_warm_uia_bridge()
        if proc is not None and proc.stdin is not None and _UIA_OUT_Q is not None:
            try:
                while not _UIA_OUT_Q.empty():
                    _UIA_OUT_Q.get_nowait()
                req = {
                    "hwnd": int(hwnd),
                    "maxElements": int(max_elements),
                    "setValueIndex": int(set_value_index),
                    "valueText": value_text,
                    "invokeIndex": int(invoke_index),
                }
                proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
                proc.stdin.flush()
                raw_line = _drain_uia_line(_UIA_OUT_Q, 1.8)
                if raw_line:
                    resp = json.loads(raw_line)
                    if "error" in resp:
                        raise RuntimeError(str(resp["error"]))
                    return resp
            except Exception:
                kill_ui_bridge()

    if modal_blocked:
        return {"focusedElement": None, "count": 0, "modalBlocked": True, "elements": []}

    val_b64 = base64.b64encode(value_text.encode("utf-8")).decode("ascii")
    invocation = (
        f"$Hwnd = [int64]{int(hwnd)}; $MaxElements = {int(max_elements)}; "
        f"$SetValueIndex = {int(set_value_index)}; "
        f"$ValueText = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{val_b64}')); "
        f"$InvokeIndex = {int(invoke_index)};\n"
    )
    full_ps = invocation + UIA_PS_SCRIPT
    encoded = base64.b64encode(full_ps.encode("utf-16-le")).decode("ascii")
    flags_ps = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    cproc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True, text=True, encoding="utf-8", timeout=12, creationflags=flags_ps,
    )
    if cproc.returncode != 0:
        raise RuntimeError(f"UIA helper failed: {cproc.stderr.strip() or cproc.stdout.strip()}")
    raw_out = cproc.stdout.strip()
    return json.loads(raw_out) if raw_out else {"focusedElement": None, "count": 0, "elements": []}


_WOKE_CHROMIUM_HWNDS: set[int] = set()


def _wake_chromium_accessibility(hwnd: int) -> None:
    if hwnd in _WOKE_CHROMIUM_HWNDS:
        return
    cls_buf = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(wintypes.HWND(hwnd), cls_buf, 64)
    if "chrome_widgetwin" not in (cls_buf.value or "").lower():
        return
    _WOKE_CHROMIUM_HWNDS.add(hwnd)
    try:
        oleacc = ctypes.windll.oleacc
        iid_iaccessible = (ctypes.c_byte * 16)(
            0x61, 0x87, 0x36, 0x61, 0x3D, 0x3C, 0xCF, 0x11, 0x81, 0x0C, 0x00, 0xAA, 0x00, 0x38, 0x9B, 0x71
        )

        def _cb(child_ptr: int | None, _: int) -> bool:
            if not child_ptr:
                return True
            ch = int(child_ptr)
            cbuf = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(wintypes.HWND(ch), cbuf, 64)
            if "renderwidgethosthwnd" in (cbuf.value or "").lower():
                res = ULONG_PTR(0)
                user32.SendMessageTimeoutW(wintypes.HWND(ch), 0x003D, 0, ctypes.c_void_p(-4), SMTO_ABORTIFHUNG, 80, ctypes.byref(res))
                pacc = ctypes.c_void_p()
                hr = oleacc.AccessibleObjectFromWindow(wintypes.HWND(ch), ctypes.c_uint32(0xFFFFFFFC), ctypes.byref(iid_iaccessible), ctypes.byref(pacc))
                if hr == 0 and pacc.value:
                    try:
                        vtbl = ctypes.cast(pacc.value, ctypes.POINTER(ctypes.c_void_p))[0]
                        rel_fn = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(ctypes.cast(vtbl, ctypes.POINTER(ctypes.c_void_p))[2])
                        rel_fn(pacc.value)
                    except Exception:
                        pass
            return True

        cb_ref = WNDENUMPROC(_cb)
        user32.EnumChildWindows(wintypes.HWND(hwnd), cb_ref, 0)
        time.sleep(0.035)
    except Exception:
        pass


def run_ui_tree_helper(
    hwnd: int,
    *,
    max_elements: int = 100,
    set_value_index: int = -1,
    value_text: str = "",
    invoke_index: int = -1,
) -> dict[str, Any]:
    _wake_chromium_accessibility(hwnd)
    parsed = _query_uia_raw(
        hwnd, max_elements=max_elements, set_value_index=set_value_index, value_text=value_text, invoke_index=invoke_index
    )
    if set_value_index < 0 and invoke_index < 0:
        elems = parsed.get("elements") or []
        if isinstance(elems, dict):
            elems = [elems]
        win_b = get_window_bounds(hwnd)
        client_uia_count = sum(
            1 for e in elems if "bounds" in e and int(e["bounds"].get("cy", 0)) > win_b["top"] + 36
        )
        if len(elems) < max_elements:
            existing_centers = [(int(e["bounds"]["cx"]), int(e["bounds"]["cy"])) for e in elems if "bounds" in e]
            for child in enum_win32_child_controls(hwnd, start_index=len(elems), max_items=max_elements - len(elems)):
                cb = child["bounds"]
                cx, cy = int(cb["cx"]), int(cb["cy"])
                ctype_low = str(child.get("controlType") or "").lower()
                is_canvas_view = (
                    int(cb.get("width", 0)) >= 200
                    and int(cb.get("height", 0)) >= 200
                    and (
                        not _has_child_windows(int(child.get("nativeHwnd") or 0))
                        or any(k in ctype_low for k in ("view", "canvas", "render", "surface", "opengl", "d3d"))
                    )
                )
                if client_uia_count < 5 or is_canvas_view:
                    tol = 20 if is_canvas_view else 6
                    if not any(abs(cx - ecx) <= tol and abs(cy - ecy) <= tol for ecx, ecy in existing_centers):
                        child["index"] = len(elems)
                        elems.append(child)
                        existing_centers.append((cx, cy))
                        if len(elems) >= max_elements:
                            break
        parsed["elements"] = elems
        parsed["count"] = len(elems)
    return parsed


def _is_tk_toplevel(hwnd: int) -> bool:
    cls_buf = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(wintypes.HWND(hwnd), cls_buf, 64)
    return (cls_buf.value or "").lower().startswith("tk")


def _resolve_element_child_hwnd(match: dict[str, Any], cx: int, cy: int) -> int:
    aid = str(match.get("automationId") or "")
    if aid.startswith("hwnd:"):
        try:
            return int(aid.split(":", 1)[1])
        except ValueError:
            pass
    nh = int(match.get("nativeHwnd") or 0)
    if nh > 0:
        return nh
    return int(user32.WindowFromPoint(POINT(cx, cy)) or 0)


def send_child_hwnd_click(child_hwnd: int, cx_screen: int, cy_screen: int) -> None:
    root_hwnd = int(user32.GetAncestor(wintypes.HWND(child_hwnd), GA_ROOT) or child_hwnd)
    cls_buf = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(wintypes.HWND(child_hwnd), cls_buf, 64)
    if (cls_buf.value or "").lower() == "button" and not _is_tk_toplevel(root_hwnd):
        user32.PostMessageW(wintypes.HWND(child_hwnd), BM_CLICK, 0, 0)
        return

    crect = RECT()
    user32.GetClientRect(wintypes.HWND(child_hwnd), ctypes.byref(crect))
    cw = max(4, int(crect.right - crect.left))
    ch = max(4, int(crect.bottom - crect.top))
    pt = POINT(cx_screen, cy_screen)
    user32.ScreenToClient(wintypes.HWND(child_hwnd), ctypes.byref(pt))
    px = max(2, min(int(pt.x), max(2, cw - 3)))
    py = max(2, min(int(pt.y), max(2, ch - 3)))
    lparam = ((py & 0xFFFF) << 16) | (px & 0xFFFF)

    cur_tid = kernel32.GetCurrentThreadId()
    target_tid = user32.GetWindowThreadProcessId(wintypes.HWND(child_hwnd), None)
    res_len = ULONG_PTR(0)
    ks = (ctypes.c_ubyte * 256)()
    attached = False
    if target_tid and target_tid != cur_tid:
        attached = bool(user32.AttachThreadInput(cur_tid, target_tid, True))
    try:
        user32.SendMessageTimeoutW(wintypes.HWND(root_hwnd), WM_ACTIVATE, WA_ACTIVE, None, SMTO_ABORTIFHUNG, 120, ctypes.byref(res_len))
        user32.GetKeyboardState(ks)
        orig_lbtn = ks[VK_LBUTTON]
        try:
            ks[VK_LBUTTON] = 0x00
            user32.SetKeyboardState(ks)
            user32.SendMessageTimeoutW(wintypes.HWND(child_hwnd), WM_MOUSEMOVE, 0, ctypes.c_void_p(lparam), SMTO_ABORTIFHUNG, 120, ctypes.byref(res_len))
            ks[VK_LBUTTON] = 0x80
            user32.SetKeyboardState(ks)
            user32.SendMessageTimeoutW(wintypes.HWND(child_hwnd), WM_LBUTTONDOWN, MK_LBUTTON, ctypes.c_void_p(lparam), SMTO_ABORTIFHUNG, 120, ctypes.byref(res_len))
            ks[VK_LBUTTON] = 0x00
            user32.SetKeyboardState(ks)
            user32.SendMessageTimeoutW(wintypes.HWND(child_hwnd), WM_LBUTTONUP, 0, ctypes.c_void_p(lparam), SMTO_ABORTIFHUNG, 120, ctypes.byref(res_len))
        finally:
            ks[VK_LBUTTON] = orig_lbtn
            user32.SetKeyboardState(ks)
    finally:
        if attached:
            user32.AttachThreadInput(cur_tid, target_tid, False)


def send_child_hwnd_text(child_hwnd: int, text: str, cx_screen: int | None = None, cy_screen: int | None = None) -> None:
    res_len = ULONG_PTR(0)
    cls_buf = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(wintypes.HWND(child_hwnd), cls_buf, 64)
    cls_low = (cls_buf.value or "").lower()

    val_buf = ctypes.create_unicode_buffer(text)
    user32.SendMessageTimeoutW(wintypes.HWND(child_hwnd), WM_SETTEXT, 0, val_buf, SMTO_ABORTIFHUNG, 150, ctypes.byref(res_len))
    if cls_low in ("edit", "richedit20w", "richedit50w"):
        return

    if cx_screen is None or cy_screen is None:
        r = RECT()
        user32.GetWindowRect(wintypes.HWND(child_hwnd), ctypes.byref(r))
        cx_screen = int((r.left + r.right) // 2)
        cy_screen = int((r.top + r.bottom) // 2)
    send_child_hwnd_click(child_hwnd, cx_screen, cy_screen)

    user32.PostMessageW(wintypes.HWND(child_hwnd), WM_KEYDOWN, VK_HOME, 0)
    user32.PostMessageW(wintypes.HWND(child_hwnd), WM_KEYUP, VK_HOME, 0)
    for _ in range(96):
        user32.PostMessageW(wintypes.HWND(child_hwnd), WM_KEYDOWN, VK_DELETE, 0)
        user32.PostMessageW(wintypes.HWND(child_hwnd), WM_KEYUP, VK_DELETE, 0)
    user32.SendMessageTimeoutW(wintypes.HWND(child_hwnd), WM_NULL, 0, None, SMTO_ABORTIFHUNG, 120, ctypes.byref(res_len))

    encoded = text.encode("utf-16-le")
    for i in range(0, len(encoded), 2):
        code_unit = int.from_bytes(encoded[i : i + 2], "little")
        user32.PostMessageW(wintypes.HWND(child_hwnd), WM_IME_CHAR, code_unit, 0)
    user32.SendMessageTimeoutW(wintypes.HWND(child_hwnd), WM_NULL, 0, None, SMTO_ABORTIFHUNG, 150, ctypes.byref(res_len))


def _child_belongs_to_target(child_hwnd: int, target_hwnd: int) -> bool:
    if child_hwnd <= 0 or target_hwnd <= 0:
        return False
    if not user32.IsWindow(wintypes.HWND(child_hwnd)):
        return False
    root = int(user32.GetAncestor(wintypes.HWND(child_hwnd), GA_ROOT) or 0)
    if root == int(target_hwnd):
        return True
    t_pid = get_window_pid(target_hwnd)
    c_pid = get_window_pid(child_hwnd)
    return t_pid > 0 and c_pid == t_pid


def click_element_direct(
    hwnd: int,
    obs: dict[str, Any],
    match: dict[str, Any],
    elem_idx: int,
    *,
    prefer_invoke: bool = False,
) -> dict[str, Any] | None:
    cx = int(match["bounds"]["cx"])
    cy = int(match["bounds"]["cy"])
    aid = str(match.get("automationId") or "")
    is_tk = _is_tk_toplevel(hwnd)
    obs_max_el = max(int(obs.get("maxElements") or 100), elem_idx + 10)
    uia_target_hwnd = int(obs.get("activePopupHwnd") or hwnd)
    if prefer_invoke or get_foreground_hwnd() == 0 or aid.startswith("hwnd:") or is_tk:
        if not aid.startswith("hwnd:") and not is_tk:
            res = run_ui_tree_helper(uia_target_hwnd, max_elements=obs_max_el, invoke_index=elem_idx)
            if res.get("method") in ("InvokePattern", "SelectionItemPattern", "TogglePattern"):
                return {
                    "summary": f"Invoked [{elem_idx}] {match['controlType']} {match.get('name')!r} via UIA {res.get('method')}.",
                    "structured": res,
                }
        child_hwnd = _resolve_element_child_hwnd(match, cx, cy)
        if child_hwnd > 0 and (_child_belongs_to_target(child_hwnd, hwnd) or _child_belongs_to_target(child_hwnd, uia_target_hwnd)):
            send_child_hwnd_click(child_hwnd, cx, cy)
            return {
                "summary": f"Dispatched background window-message click to [{elem_idx}] {match['controlType']} (hwnd:{child_hwnd}).",
                "structured": {"action": "background_msg_click", "index": elem_idx, "childHwnd": child_hwnd},
            }
    return None


def set_value_direct(
    hwnd: int,
    obs: dict[str, Any],
    match: dict[str, Any],
    elem_idx: int,
    value: str,
    *,
    prefer_background_msg: bool | None = None,
) -> dict[str, Any] | None:
    cx, cy = int(match["bounds"]["cx"]), int(match["bounds"]["cy"])
    aid = str(match.get("automationId") or "")
    is_tk = _is_tk_toplevel(hwnd)
    obs_max_el = max(int(obs.get("maxElements") or 100), elem_idx + 10)
    uia_target_hwnd = int(obs.get("activePopupHwnd") or hwnd)
    if not aid.startswith("hwnd:") and not is_tk:
        res = run_ui_tree_helper(uia_target_hwnd, max_elements=obs_max_el, set_value_index=elem_idx, value_text=value)
        if res.get("method") == "ValuePattern":
            return {
                "summary": f"Set value on element [{elem_idx}] via UIA ValuePattern ({len(value)} chars).",
                "structured": res,
            }
    child_hwnd = _resolve_element_child_hwnd(match, cx, cy)
    if child_hwnd > 0 and not (_child_belongs_to_target(child_hwnd, hwnd) or _child_belongs_to_target(child_hwnd, uia_target_hwnd)):
        child_hwnd = 0
    off_default = current_desktop_name().lower() != "default" or get_foreground_hwnd() == 0
    cls_low = ""
    if child_hwnd > 0:
        cbuf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(wintypes.HWND(child_hwnd), cbuf, 64)
        cls_low = (cbuf.value or "").lower()
    allow_bg_msg = (
        child_hwnd > 0
        and (
            off_default
            or is_tk
            or (bool(prefer_background_msg) if prefer_background_msg is not None else cls_low in ("edit", "richedit20w", "richedit50w"))
        )
    )
    if allow_bg_msg:
        send_child_hwnd_text(child_hwnd, value, cx, cy)
        return {
            "summary": f"Dispatched background text ({len(value)} chars) to [{elem_idx}] (hwnd:{child_hwnd}).",
            "structured": {"action": "background_msg_text", "index": elem_idx, "childHwnd": child_hwnd},
        }
    return None


# Clipboard (Win32 CF_UNICODETEXT)
def _open_clipboard_with_retry(retries: int = 8, delay_sec: float = 0.025) -> bool:
    for _ in range(retries):
        if user32.OpenClipboard(None):
            return True
        time.sleep(delay_sec)
    return False


def clipboard_read(max_chars: int) -> tuple[dict[str, Any] | None, str | None]:
    if not _open_clipboard_with_retry():
        return None, "clipboard_busy: Could not open Windows clipboard."
    try:
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return {"hasText": False, "text": "", "length": 0, "truncated": False}, None
        h_data = user32.GetClipboardData(CF_UNICODETEXT)
        if not h_data:
            return {"hasText": False, "text": "", "length": 0, "truncated": False}, None
        ptr = kernel32.GlobalLock(wintypes.HGLOBAL(h_data))
        if not ptr:
            return None, "clipboard_lock_failed: GlobalLock returned NULL."
        try:
            byte_size = int(kernel32.GlobalSize(wintypes.HGLOBAL(h_data)) or 0)
            if byte_size >= 2:
                raw_wstr = ctypes.wstring_at(ptr, byte_size // 2)
                text = raw_wstr.split("\x00", 1)[0]
            else:
                text = ""
        finally:
            kernel32.GlobalUnlock(wintypes.HGLOBAL(h_data))
    finally:
        user32.CloseClipboard()
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]
    return {"hasText": True, "text": text, "length": len(text), "truncated": truncated}, None


def clipboard_write(text: str) -> tuple[dict[str, Any] | None, str | None]:
    encoded = text.encode("utf-16-le") + b"\x00\x00"
    if not _open_clipboard_with_retry():
        return None, "clipboard_busy: Could not open Windows clipboard."
    try:
        if not user32.EmptyClipboard():
            return None, "clipboard_empty_failed: EmptyClipboard failed."
        h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
        if not h_mem:
            return None, "clipboard_alloc_failed: GlobalAlloc returned NULL."
        ptr = kernel32.GlobalLock(wintypes.HGLOBAL(h_mem))
        if not ptr:
            kernel32.GlobalFree(wintypes.HGLOBAL(h_mem))
            return None, "clipboard_lock_failed: GlobalLock returned NULL."
        try:
            ctypes.memmove(ptr, encoded, len(encoded))
        finally:
            kernel32.GlobalUnlock(wintypes.HGLOBAL(h_mem))
        placed = user32.SetClipboardData(CF_UNICODETEXT, wintypes.HANDLE(h_mem))
        if not placed:
            kernel32.GlobalFree(wintypes.HGLOBAL(h_mem))
            return None, "clipboard_set_failed: SetClipboardData failed."
    finally:
        user32.CloseClipboard()
    return {"characters": len(text), "bytes": len(encoded)}, None


# Overlay HUD & Ancestor Watchdog
def spawn_overlay_hud(runtime_dir: Path) -> subprocess.Popen[Any] | None:
    if not OVERLAY_SCRIPT.is_file():
        return None
    py_dir = Path(sys.executable).resolve().parent
    pyw = py_dir / "pythonw.exe"
    exe = str(pyw if pyw.is_file() else Path(sys.executable))
    flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    try:
        return subprocess.Popen(
            [exe, str(OVERLAY_SCRIPT)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, creationflags=flags,
        )
    except Exception:
        return None


def is_overlay_exe_match(exe_low: str) -> bool:
    return (not exe_low) or ("python" in exe_low) or ("overlay_hud" in exe_low)


def _get_ancestor_pids() -> list[int]:
    my_pid = os.getpid()
    ppid_map: dict[int, int] = {}
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == wintypes.HANDLE(-1).value:
        return []
    try:
        pe = _PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        if kernel32.Process32FirstW(snap, ctypes.byref(pe)):
            while True:
                ppid_map[int(pe.th32ProcessID)] = int(pe.th32ParentProcessID)
                if not kernel32.Process32NextW(snap, ctypes.byref(pe)):
                    break
    finally:
        kernel32.CloseHandle(snap)
    ancestors: list[int] = []
    cur = ppid_map.get(my_pid, 0)
    if cur > 4:
        ancestors.append(cur)
    return ancestors[:1]


def make_parent_exit_checker() -> tuple[Callable[[], bool], Callable[[], None]]:
    handles: list[int] = []
    for apid in _get_ancestor_pids():
        h = int(kernel32.OpenProcess(SYNCHRONIZE, False, int(apid)) or 0)
        if h:
            handles.append(h)

    def _parent_exited() -> bool:
        for h in handles:
            if kernel32.WaitForSingleObject(wintypes.HANDLE(h), 0) == 0:
                return True
        return False

    def _close() -> None:
        for h in handles:
            try:
                kernel32.CloseHandle(wintypes.HANDLE(h))
            except Exception:
                pass
        handles.clear()

    return _parent_exited, _close
