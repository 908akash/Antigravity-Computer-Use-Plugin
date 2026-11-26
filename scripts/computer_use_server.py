"""Hardened Windows 10/11 Computer Use MCP Server & CLI.

Zero-dependency pure Win32 ctypes automation engine providing:
  - Per-Monitor DPI v2 awareness across mixed-DPI and multi-monitor displays
  - Modal-immune background capture via Win32 PrintWindow + DWM bounds + GDI fallback
  - High-resolution region zoom (cu_zoom) and numbered UI element overlays
  - Indexed Windows UI Automation tree with ValuePattern/InvokePattern actions
  - Hardware 120Hz SendInput mouse paths, layout-aware hotkeys, and Unicode typing
  - Fast local UI element matcher (Unicode/color/symbol + optional local LLM fallback)
  - Multi-layer safety: expiring per-app session grants, terminal/password/UAC/agent
    deny-list, browser look-only tier, sensitive-file editor guard, UIPI elevation
    detection, occlusion/focus checks, human takeover auto-dismiss, and Esc abort.
"""

from __future__ import annotations

import atexit
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
import ctypes
from ctypes import wintypes
import hashlib
import io
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
import tempfile
import threading
import time
import traceback
import uuid
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

_pylibs_env = os.environ.get("CUA_PYLIBS_DIR", "").strip()
if _pylibs_env and _pylibs_env not in sys.path:
    sys.path.insert(0, _pylibs_env)

from PIL import Image, ImageDraw, ImageFont, ImageGrab

SERVER_NAME = "computer-use-win32"
SERVER_VERSION = "3.1.0"
DEFAULT_PROTOCOL = "2024-11-05"

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import importlib
import local_matcher as _local_matcher_mod  # noqa: E402

_MATCHER_MTIME_NS: int = -1


def _get_local_matcher():
    global _MATCHER_MTIME_NS
    try:
        st = Path(_local_matcher_mod.__file__).stat()
        if _MATCHER_MTIME_NS != st.st_mtime_ns:
            if _MATCHER_MTIME_NS != -1:
                importlib.reload(_local_matcher_mod)
            _MATCHER_MTIME_NS = st.st_mtime_ns
    except Exception:
        pass
    return _local_matcher_mod


def match_ui_element(goal: str, elements: list[dict[str, Any]], *, confidence_floor: float = 0.80) -> dict[str, Any]:
    return _get_local_matcher().match_ui_element(goal, elements, confidence_floor=confidence_floor)


PLUGIN_DIR = SCRIPT_DIR.parent


def _dir_is_writable(d: Path) -> bool:
    probe: Path | None = None
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / f".write_probe_{os.getpid()}_{uuid.uuid4().hex[:6]}"
        probe.write_text("", encoding="utf-8")
        return True
    except Exception:
        return False
    finally:
        if probe is not None:
            try:
                probe.unlink(missing_ok=True)
            except Exception:
                pass


def _resolve_runtime_dir(default_dir: Path) -> Path:
    candidates: list[Path] = []
    for var in ("CUA_RUNTIME_DIR", "PLUGIN_DATA"):
        val = os.environ.get(var, "").strip()
        if val and not val.startswith("${"):
            candidates.append(Path(val))
    candidates.append(default_dir)
    local_app = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app:
        candidates.append(Path(local_app) / "computer-use-win32")
    temp_dir = os.environ.get("TEMP", "").strip() or os.environ.get("TMP", "").strip() or tempfile.gettempdir()
    if temp_dir:
        candidates.append(Path(temp_dir) / "computer-use-win32")
    for cand in candidates:
        if _dir_is_writable(cand):
            return cand
    return candidates[0]


RUNTIME_DIR = _resolve_runtime_dir(PLUGIN_DIR / ".runtime")
SHOTS_DIR = RUNTIME_DIR / "screenshots"
STATE_FILE = RUNTIME_DIR / "session_state.json"
OVERLAY_SCRIPT = Path(__file__).resolve().parent / "overlay_hud.py"
UIA_CS_FILE = Path(__file__).resolve().parent / "uia_fast_bridge.cs"
OVERLAY_STATE_FILE = RUNTIME_DIR / "overlay_state.json"
OVERLAY_PID_FILE = RUNTIME_DIR / "overlay_pid.json"

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

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

# Security Policy Tables
BLOCKED_PROCESSES = {
    # Shells, terminals & script/LOLBin launchers
    "cmd.exe",
    "powershell.exe",
    "pwsh.exe",
    "windowsterminal.exe",
    "wt.exe",
    "conhost.exe",
    "openconsole.exe",
    "mintty.exe",
    "alacritty.exe",
    "wezterm-gui.exe",
    "powershell_ise.exe",
    "wsl.exe",
    "git-bash.exe",
    "putty.exe",
    "tabby.exe",
    "hyper.exe",
    "conemu.exe",
    "conemu64.exe",
    "bash.exe",
    "sh.exe",
    "zsh.exe",
    "ubuntu.exe",
    "debian.exe",
    "kali.exe",
    "mshta.exe",
    "cscript.exe",
    "wscript.exe",
    "rundll32.exe",
    "regsvr32.exe",
    "reg.exe",
    "regedt32.exe",
    "runas.exe",
    "schtasks.exe",
    "sc.exe",
    "net.exe",
    "net1.exe",
    "certutil.exe",
    "bitsadmin.exe",
    "msiexec.exe",
    # Password managers & auth brokers
    "1password.exe",
    "bitwarden.exe",
    "keepass.exe",
    "keepassxc.exe",
    "dashlane.exe",
    "credentialuibroker.exe",
    # Security, UAC, lock screen, system admin & debuggers
    "explorer.exe",
    "consent.exe",
    "lockapp.exe",
    "logonui.exe",
    "sechealthui.exe",
    "regedit.exe",
    "taskmgr.exe",
    "mmc.exe",
    "procexp.exe",
    "procexp64.exe",
    "procmon.exe",
    "procmon64.exe",
    "windbg.exe",
    "x64dbg.exe",
    # Agent hosts (prevent self-approval / prompt-injection self-hijack)
    "antigravity.exe",
    "codex.exe",
    "claude.exe",
}

_BLOCKED_LAUNCH_RUNTIMES = {
    "python.exe",
    "pythonw.exe",
    "py.exe",
    "node.exe",
    "deno.exe",
    "bun.exe",
}

_BLOCKED_AUMID_FRAGMENTS = (
    "windowsterminal",
    "terminal",
    "powershell",
    "pwsh",
    "commandprompt",
    "cmd.exe",
    "wsl",
    "ubuntu",
    "debian",
    "kali",
    "opensuse",
    "regedit",
    "taskmgr",
    "1password",
    "bitwarden",
    "keepass",
    "dashlane",
    "antigravity",
    "claude",
    "codex",
)

BROWSER_PROCESSES = {
    "chrome.exe",
    "msedge.exe",
    "firefox.exe",
    "brave.exe",
    "opera.exe",
    "vivaldi.exe",
    "arc.exe",
}

EDITOR_PROCESSES = {
    "notepad.exe",
    "notepad++.exe",
    "wordpad.exe",
    "code.exe",
    "code - insiders.exe",
    "cursor.exe",
    "windsurf.exe",
    "vscodium.exe",
    "zed.exe",
    "sublime_text.exe",
    "devenv.exe",
    "idea64.exe",
    "pycharm64.exe",
    "webstorm64.exe",
}

SENSITIVE_TITLE_PATTERNS = [
    re.compile(r"(?:^|[\s\\/\[\(])(\.bashrc|\.bash_profile|\.zshrc|\.profile|profile\.ps1|microsoft\.powershell_profile\.ps1)(?:$|[\s\*—\-–\]\)])", re.I),
    re.compile(r"(?:^|[\s\\/\[\(])(authorized_keys|known_hosts|id_rsa|id_ed25519|id_ecdsa|\.ssh)(?:$|[\s\\/\*—\-–\]\)])", re.I),
    re.compile(r"(?:^|[\s\\/\[\(])(\.gitconfig|mcp_config\.json|claude_desktop_config\.json|\.claude\.json|settings\.json|config\.toml)(?:$|[\s\*—\-–\]\)])", re.I),
    re.compile(r"(?:^|[\s\\/\[\(●])(\.env(?:\.[a-z0-9_.-]+)?|\.npmrc|\.pypirc|\.netrc)(?:$|[\s\*—\-–\]\):,])", re.I),
    re.compile(r"(?:^|[\s\\/\[\(●])credentials(?:\.[a-z0-9_.-]+)?(?:$|[\s\\/\*—\-–\]\):,])", re.I),
    re.compile(r"\.git[\\/]hooks", re.I),
    re.compile(r"\.(bat|cmd|reg|vbs|vbe|jse|wsf|wsh|hta|ps1|pem|key|pfx|p12)(?:$|[\s\*—\-–\]\):,])", re.I),
]

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
    """Return PE OptionalHeader.Subsystem (2=Windows GUI, 3=Windows CUI console, 0=unknown)."""
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
        return None  # Skip UNC/device paths that can stall or access remote/kernel namespaces
    p = Path(expanded)
    if p.is_file() and p.suffix.lower() == ".exe":
        try:
            return p.resolve()
        except OSError:
            return p
    return None


def _build_registry_and_fs_app_index() -> dict[str, list[str]]:
    """Dynamically index installed Windows apps via App Paths, Uninstall, WindowsApps, Squirrel & ProgramFiles."""
    index: dict[str, list[str]] = {}

    def _add(label: str, cmd: list[str]) -> None:
        key = _norm_app_key(label)
        if key and key not in index:
            index[key] = cmd

    # 1. Registry App Paths
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

    # 2. WindowsApps & generic Squirrel/Electron in %LOCALAPPDATA%
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

    # 3. Registry Uninstall DisplayName -> DisplayIcon / InstallLocation
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
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=4.0,
            creationflags=flags,
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


def _is_blocked_launch_target(raw_app: str, cmd: list[str], extra_args: list[str] | None = None) -> str | None:
    """Verify neither the executable nor a UWP StartApps AUMID bypasses BLOCKED_PROCESSES."""
    raw_canon = _canonical_proc_or_name(raw_app)
    if classify_policy(raw_canon) == "blocked":
        return raw_app
    if _norm_proc_name(raw_canon) in _BLOCKED_LAUNCH_RUNTIMES:
        return raw_app
    is_uwp_appsfolder = (
        len(cmd) == 2
        and cmd[0].lower() == "explorer.exe"
        and str(cmd[1]).lower().startswith("shell:appsfolder\\")
        and not (extra_args or [])
    )
    if is_uwp_appsfolder:
        aumid = str(cmd[1]).split("\\", 1)[1].lower()
        if not aumid:
            return cmd[1]
        for frag in _BLOCKED_AUMID_FRAGMENTS:
            if frag in aumid:
                return aumid
        return None
    cmd0_canon = _canonical_proc_or_name(cmd[0])
    if classify_policy(cmd0_canon) == "blocked":
        return Path(cmd0_canon).name
    if _norm_proc_name(cmd0_canon) in _BLOCKED_LAUNCH_RUNTIMES:
        return Path(cmd0_canon).name
    return None


def _resolve_known_or_installed_app(app: str) -> tuple[list[str] | None, tuple[str, ...]]:
    """Universally resolve GUI desktop apps via path/Registry/WindowsApps/StartApps before PATH fallback."""
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

    # 1. Exact GUI match in dynamic Registry / WindowsApps / Squirrel index
    if key in idx:
        cmd = idx[key]
        leaf = Path(cmd[0]).name.lower()
        return cmd, (leaf,) if leaf.endswith(".exe") else ()

    # 2. Prefix / acronym / word match in dynamic Registry / filesystem index (prefers GUI subsystem)
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

    # 3. Start Menu & Store/UWP AppsFolder lookup (finds desktop GUI shortcuts before CLI binaries on PATH)
    for nm, aid in _get_start_apps_index():
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
                if _get_pe_subsystem(resolved_aid) != 3:
                    return [str(resolved_aid)], (resolved_aid.name.lower(),)
            return ["explorer.exe", f"shell:AppsFolder\\{aid}"], (f"{key}.exe", key)

    # 4. PATH fallback (.exe ONLY — never .cmd/.bat shims, preferring GUI subsystem over CLI console tools)
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
            if p.suffix.lower() != ".exe" or classify_policy(p.name) == "blocked":
                continue
            subsys = _get_pe_subsystem(p)
            if subsys != 3:
                return [str(p)], (p.name.lower(),)
            if cui_fallback is None:
                cui_fallback = ([str(p)], (p.name.lower(),))
    if cui_fallback is not None:
        return cui_fallback

    return None, ()

BLOCKED_KEYS = {
    "win", "winleft", "winright", "windows", "super", "meta", "cmd", "command", "os",
}


# Win32 ctypes structures & 64-bit pointer-sized integer aliases
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


# Explicit 64-bit-safe signatures for every Win32 / GDI / DWM / Kernel32 / Advapi32 call
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
    wintypes.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
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
user32.SendMessageTimeoutW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    ctypes.c_void_p,
    wintypes.UINT,
    wintypes.UINT,
    ctypes.POINTER(ULONG_PTR),
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
    wintypes.HDC,
    ctypes.POINTER(BITMAPINFO),
    wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p),
    wintypes.HANDLE,
    wintypes.DWORD,
]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.restype = wintypes.BOOL
gdi32.BitBlt.argtypes = [
    wintypes.HDC,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HDC,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.DWORD,
]
gdi32.BitBlt.restype = wintypes.BOOL
SRCCOPY = 0x00CC0020

dwmapi.DwmGetWindowAttribute.argtypes = [
    wintypes.HWND,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
]
dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long

kernel32.GetCurrentThreadId.argtypes = []
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.GetProcessTimes.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(wintypes.FILETIME),
    ctypes.POINTER(wintypes.FILETIME),
    ctypes.POINTER(wintypes.FILETIME),
    ctypes.POINTER(wintypes.FILETIME),
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

advapi32.OpenProcessToken.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.HANDLE),
]
advapi32.OpenProcessToken.restype = wintypes.BOOL
advapi32.GetTokenInformation.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]
advapi32.GetTokenInformation.restype = wintypes.BOOL


def ensure_runtime_dirs() -> None:
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)


def load_state() -> dict[str, Any]:
    ensure_runtime_dirs()
    if STATE_FILE.is_file():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("grants", {})
                data.setdefault("observations", {})
                return data
        except Exception:
            pass
    return {"grants": {}, "observations": {}}


def _atomic_write_json(target: Path, data: dict[str, Any], *, indent: int | None = None) -> None:
    ensure_runtime_dirs()
    tmp = target.parent / f".{target.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp"
    raw = json.dumps(data, ensure_ascii=True, indent=indent)
    tmp.write_text(raw, encoding="utf-8")
    last_err: Exception | None = None
    for _ in range(8):
        try:
            tmp.replace(target)
            return
        except PermissionError as exc:
            last_err = exc
            time.sleep(0.008)
    try:
        target.write_text(raw, encoding="utf-8")
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
    if last_err is not None and not target.is_file():
        raise last_err


_STATE_LOCK = threading.RLock()


def save_state(state: dict[str, Any]) -> None:
    ensure_runtime_dirs()
    with _STATE_LOCK:
        # Keep last 25 observations to bound state file size
        obs = state.get("observations", {})
        if len(obs) > 25:
            keys = sorted(obs.keys(), key=lambda k: obs[k].get("timestamp", 0))[-25:]
            obs = {k: obs[k] for k in keys}
            state["observations"] = obs
        to_write = {
            **state,
            "grants": dict(state.get("grants") or {}),
            "observations": {} if _STDIO_MODE else dict(obs),
        }
        _atomic_write_json(STATE_FILE, to_write, indent=2)


def _get_observation(obs_id: str | None) -> dict[str, Any] | None:
    if not obs_id:
        return None
    with _STATE_LOCK:
        obs = STATE.get("observations", {}).get(str(obs_id))
        return dict(obs) if isinstance(obs, dict) else None


def _store_observation(obs_id: str, obs: dict[str, Any]) -> None:
    with _STATE_LOCK:
        obs_map = STATE.setdefault("observations", {})
        obs_map[obs_id] = obs
        if len(obs_map) > 30:
            keys = sorted(obs_map.keys(), key=lambda k: obs_map[k].get("timestamp", 0))[-30:]
            STATE["observations"] = {k: obs_map[k] for k in keys}


def _purge_session_artifacts() -> None:
    with _STATE_LOCK:
        STATE["observations"] = {}
    try:
        if SHOTS_DIR.is_dir():
            for p in SHOTS_DIR.iterdir():
                if p.is_file() and p.name.startswith("obs_"):
                    try:
                        p.unlink(missing_ok=True)
                    except OSError:
                        pass
    except Exception:
        pass


_ABORT_EVENT = threading.Event()
_SERVER_BOOT_ID = uuid.uuid4().hex[:10]
_IN_FLIGHT_TOOLS: int = 0
_LAST_TOOL_ACTIVITY: float = time.time()
_LAST_TOOL_DONE_AT: float = time.time()
_THINKING_WAIT_SEC = max(10.0, min(float(os.environ.get("CUA_THINKING_WAIT_SEC", "90")), 300.0))
_HUD_IDLE_TIMEOUT_SEC = max(30.0, min(float(os.environ.get("CUA_HUD_IDLE_TIMEOUT_SEC", "600")), 1800.0))
_GRANT_IDLE_TTL_SEC = max(30.0, min(float(os.environ.get("CUA_GRANT_TTL_SEC", "600")), 1800.0))
_HELPER_JOB: int = 0

STATE = load_state()
_STDIO_MODE = False
_OVERLAY_ALIVE_CACHE: tuple[float, bool] = (0.0, False)
_OVERLAY_PROC: subprocess.Popen[bytes] | None = None


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


def _close_helper_job() -> None:
    global _HELPER_JOB
    if _HELPER_JOB:
        try:
            kernel32.CloseHandle(wintypes.HANDLE(_HELPER_JOB))
        except Exception:
            pass
        _HELPER_JOB = 0


def _norm_proc_name(name: str) -> str:
    s = (name or "").strip(" \"'").replace("/", "\\").rsplit("\\", 1)[-1].rstrip(".").lower()
    if s and "." not in s:
        s = f"{s}.exe"
    return s


def prune_expired_grants(*, save_if_changed: bool = True) -> bool:
    """Expire idle grants past TTL and drop stale grants owned by dead previous MCP server PIDs."""
    changed = False
    now = time.time()
    with _STATE_LOCK:
        grants = STATE.get("grants")
        if not isinstance(grants, dict) or not grants:
            return False
        for key in list(grants.keys()):
            g = grants.get(key)
            if not isinstance(g, dict):
                grants.pop(key, None)
                changed = True
                continue
            ttl = float(g.get("ttlSec") or _GRANT_IDLE_TTL_SEC)
            last_used = float(g.get("lastUsedEpoch") or g.get("grantedEpoch") or 0.0)
            if last_used > 0.0 and (now - last_used) > ttl:
                grants.pop(key, None)
                changed = True
                continue
            if _STDIO_MODE:
                old_pid = int(g.get("serverPid") or 0)
                old_boot = str(g.get("serverBootId") or "")
                if old_boot and old_boot != _SERVER_BOOT_ID and old_pid > 0 and not _pid_is_alive(old_pid):
                    grants.pop(key, None)
                    changed = True
        if changed and save_if_changed:
            save_state(STATE)
            if not STATE["grants"]:
                update_overlay_hud(False, "Idle", force_flush=True)
    return changed


def sync_state(*, force_disk: bool = False) -> None:
    global STATE
    with _STATE_LOCK:
        if not _STDIO_MODE or force_disk:
            STATE = load_state()
        prune_expired_grants(save_if_changed=True)


def _restore_os_cursors_safe() -> None:
    try:
        user32.SystemParametersInfoW(0x0057, 0, None, 0)
    except Exception:
        pass


_TIMER_PERIOD_DEPTH: int = 0
_IN_BATCH_DEPTH: int = 0


def _enter_1ms_timer() -> None:
    global _TIMER_PERIOD_DEPTH
    if _TIMER_PERIOD_DEPTH == 0:
        winmm.timeBeginPeriod(1)
    _TIMER_PERIOD_DEPTH += 1


def _exit_1ms_timer() -> None:
    global _TIMER_PERIOD_DEPTH
    if _TIMER_PERIOD_DEPTH > 0:
        _TIMER_PERIOD_DEPTH -= 1
        if _TIMER_PERIOD_DEPTH == 0:
            winmm.timeEndPeriod(1)


def get_cursor_pos() -> tuple[int, int]:
    pt = POINT(0, 0)
    user32.GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    h = kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
    if not h:
        return False
    try:
        return kernel32.WaitForSingleObject(h, 0) != 0
    finally:
        kernel32.CloseHandle(h)


def _query_pid_exe(pid: int) -> str:
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


def _get_monitor_rects() -> list[tuple[int, int, int, int]]:
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


def send_mouse_move_abs(tx: int, ty: int) -> None:
    """Dispatch real hardware absolute pointer motion across virtual desktop using pixel-center SendInput math."""
    x, y = int(tx), int(ty)
    if _IS_LOW_INTEGRITY or current_desktop_name().lower() != "default":
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
        nx,
        ny,
        0,
        wintypes.DWORD(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK),
        0,
        _CUA_INPUT_TAG,
    )
    if user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)) != 1:
        user32.SetCursorPos(x, y)


def smooth_move_to(tx: int, ty: int, duration: float | None = None, *, check_abort: bool = True) -> None:
    """120Hz minimum-jerk hardware cursor glide — dispatches real SendInput pointer packets."""
    if _IS_LOW_INTEGRITY or current_desktop_name().lower() != "default":
        user32.SetCursorPos(int(tx), int(ty))
        return
    sx, sy = get_cursor_pos()
    dx, dy = float(tx - sx), float(ty - sy)
    dist = math.hypot(dx, dy)
    if dist < 2.0 or (duration is not None and duration <= 0.004):
        send_mouse_move_abs(int(tx), int(ty))
        return
    if duration is None:
        if _IN_BATCH_DEPTH > 0:
            dur = max(0.012, min(0.026, 0.012 + (min(dist, 1400.0) / 1400.0) * 0.014))
        else:
            dur = max(0.020, min(0.046, 0.020 + (min(dist, 1400.0) / 1400.0) * 0.026))
    else:
        dur = max(0.008, min(float(duration), 2.0))

    steps = max(3, int(round(dur * 120.0)))
    dt = dur / steps
    _enter_1ms_timer()
    t0 = time.perf_counter()
    try:
        for i in range(1, steps + 1):
            if check_abort and is_escape_pressed():
                update_overlay_hud(False, "Stopped (Esc)", force_flush=True)
                raise RuntimeError("emergency_stop: Physical Escape key detected during cursor glide.")
            t = i / steps
            u = t ** 0.82
            s = u * u * u * (10.0 - 15.0 * u + 6.0 * u * u)
            ix = int(round(sx + dx * s))
            iy = int(round(sy + dy * s))
            send_mouse_move_abs(ix, iy)
            target_t = t0 + i * dt
            rem = target_t - time.perf_counter()
            if rem > 0.0018:
                time.sleep(rem - 0.0012)
            while time.perf_counter() < target_t:
                pass
        send_mouse_move_abs(int(tx), int(ty))
    finally:
        _exit_1ms_timer()


def get_foreground_hwnd() -> int:
    return int(user32.GetForegroundWindow() or 0)


def virtual_bounds() -> dict[str, int]:
    return {
        "left": int(user32.GetSystemMetrics(76)),
        "top": int(user32.GetSystemMetrics(77)),
        "width": int(user32.GetSystemMetrics(78)),
        "height": int(user32.GetSystemMetrics(79)),
    }


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


def _is_sandboxed_low_integrity() -> bool:
    """Detect if this server process itself is running inside a Low/Untrusted/AppContainer test sandbox."""
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
                    if rid < 0x2000:  # Below SECURITY_MANDATORY_MEDIUM_RID
                        return True
    except Exception:
        pass
    finally:
        kernel32.CloseHandle(htok)
    return False


_IS_LOW_INTEGRITY = _is_sandboxed_low_integrity()


def is_desktop_locked() -> bool:
    if _IS_LOW_INTEGRITY or current_desktop_name().lower() != "default":
        return False
    hdesk = user32.OpenInputDesktop(0, False, DESKTOP_SWITCHDESKTOP)
    if not hdesk:
        return True
    user32.CloseDesktop(wintypes.HDESK(hdesk))
    return False


_CURSOR_ENGAGED = False


def is_escape_pressed() -> bool:
    return bool(user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000)


_OVERLAY_READ_KEY: tuple[int, int] = (-1, -1)
_OVERLAY_READ_CACHE: dict[str, Any] = {"active": False}
_OVERLAY_LAST_STAT_MONO: float = 0.0
_OVERLAY_LAST_WRITE_MONO: float = 0.0
_OVERLAY_DIRTY_PAYLOAD: dict[str, Any] | None = None
_OVERLAY_MIN_WRITE_INTERVAL_SEC: float = 0.120


def read_overlay_state(*, throttle_stat_sec: float = 0.0) -> dict[str, Any]:
    global _OVERLAY_READ_KEY, _OVERLAY_READ_CACHE, _OVERLAY_LAST_STAT_MONO
    now_m = time.monotonic()
    if throttle_stat_sec > 0.0 and (now_m - _OVERLAY_LAST_STAT_MONO) < throttle_stat_sec:
        return _OVERLAY_READ_CACHE
    _OVERLAY_LAST_STAT_MONO = now_m
    try:
        st = OVERLAY_STATE_FILE.stat()
        key = (st.st_mtime_ns, st.st_size)
        if key == _OVERLAY_READ_KEY:
            return _OVERLAY_READ_CACHE
        data = json.loads(OVERLAY_STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            _OVERLAY_READ_KEY = key
            _OVERLAY_READ_CACHE = data
            return _OVERLAY_READ_CACHE
    except FileNotFoundError:
        _OVERLAY_READ_KEY = (-1, -1)
        _OVERLAY_READ_CACHE = {"active": False}
    except Exception:
        pass
    return _OVERLAY_READ_CACHE


def _write_overlay_state(payload: dict[str, Any]) -> None:
    global _OVERLAY_READ_KEY, _OVERLAY_READ_CACHE, _OVERLAY_LAST_WRITE_MONO, _OVERLAY_DIRTY_PAYLOAD
    try:
        _atomic_write_json(OVERLAY_STATE_FILE, payload)
        _OVERLAY_READ_CACHE = dict(payload)
        try:
            st = OVERLAY_STATE_FILE.stat()
            _OVERLAY_READ_KEY = (st.st_mtime_ns, st.st_size)
        except OSError:
            pass
        _OVERLAY_LAST_WRITE_MONO = time.monotonic()
        _OVERLAY_DIRTY_PAYLOAD = None
    except Exception:
        pass


def flush_overlay_hud() -> None:
    if _OVERLAY_DIRTY_PAYLOAD is not None:
        _write_overlay_state(_OVERLAY_DIRTY_PAYLOAD)


def is_overlay_daemon_alive(*, force: bool = False) -> bool:
    global _OVERLAY_ALIVE_CACHE, _OVERLAY_PROC
    now = time.monotonic()
    # Always inspect overlay_pid.json on force=True so an 'exiting' fade-out is never treated as alive
    pid_info = _read_overlay_pid_info() if (force or _OVERLAY_PROC is None or (now - _OVERLAY_ALIVE_CACHE[0]) >= 0.35) else None
    if pid_info is not None:
        st_hud = str(pid_info.get("status") or "").lower()
        if st_hud == "exiting":
            _OVERLAY_PROC = None
            _OVERLAY_ALIVE_CACHE = (now, False)
            return False
    if _OVERLAY_PROC is not None:
        if _OVERLAY_PROC.poll() is None:
            return True
        _OVERLAY_PROC = None
        _OVERLAY_ALIVE_CACHE = (0.0, False)
    if not force and (now - _OVERLAY_ALIVE_CACHE[0]) < 1.0:
        return _OVERLAY_ALIVE_CACHE[1]
    alive = False
    try:
        info = pid_info if pid_info is not None else _read_overlay_pid_info()
        st_hud = str(info.get("status") or "running").lower()
        pid = int(info.get("pid") or 0)
        if st_hud == "running" and pid > 0 and _pid_is_alive(pid):
            exe_low = _query_pid_exe(pid).lower()
            if not exe_low or "python" in exe_low:
                alive = True
    except Exception:
        pass
    _OVERLAY_ALIVE_CACHE = (now, alive)
    return alive


_BATCH_STAGE_LABEL: str | None = None


def update_overlay_hud(
    active: bool,
    label: str = "Active",
    *,
    click_xy: tuple[int, int] | None = None,
    cursor_xy: tuple[int, int] | None = None,
    cursor_style: str | None = None,
    ttl_sec: float = 600.0,
    allow_capture: bool = False,
    force_spawn: bool = False,
    force_flush: bool = False,
) -> dict[str, Any]:
    global _OVERLAY_ALIVE_CACHE, _OVERLAY_PROC, _OVERLAY_DIRTY_PAYLOAD
    prev = _OVERLAY_DIRTY_PAYLOAD if _OVERLAY_DIRTY_PAYLOAD is not None else read_overlay_state(throttle_stat_sec=0.04)
    now = time.time()
    now_m = time.monotonic()
    style = cursor_style or str(prev.get("cursorStyle") or "sleek_dart")
    eff_label = (_BATCH_STAGE_LABEL if (active and _BATCH_STAGE_LABEL) else label) or "Active"
    is_exec = _IN_FLIGHT_TOOLS > 0
    eff_idle = max(2.0, min(float(ttl_sec if ttl_sec <= 1800.0 else _HUD_IDLE_TIMEOUT_SEC), 1800.0))
    payload: dict[str, Any] = {
        "active": bool(active),
        "label": str(eff_label)[:72],
        "cursorStyle": style,
        "updatedAt": now,
        "executing": is_exec,
        "leaseExpiresAt": now + 60.0,
        "lastToolDoneAt": now if is_exec else _LAST_TOOL_DONE_AT,
        "thinkingWaitSec": _THINKING_WAIT_SEC,
        "idleTimeoutSec": eff_idle,
        "ttlSec": eff_idle,
        "allowCapture": bool(allow_capture),
    }
    if _STDIO_MODE and active:
        payload["parentPid"] = os.getpid()
    eff_cursor = cursor_xy if cursor_xy is not None else click_xy
    if eff_cursor is not None:
        payload["cursor"] = {"x": int(eff_cursor[0]), "y": int(eff_cursor[1]), "ts": now}
    elif active and isinstance(prev.get("cursor"), dict):
        payload["cursor"] = prev["cursor"]

    if click_xy is not None:
        payload["click"] = {"x": int(click_xy[0]), "y": int(click_xy[1]), "ts": now}
    elif active and isinstance(prev.get("click"), dict) and (now - float(prev["click"].get("ts", 0)) <= 0.40):
        payload["click"] = prev["click"]

    if not active:
        _write_overlay_state(payload)
        _OVERLAY_ALIVE_CACHE = (now_m, False)
        _OVERLAY_PROC = None
        _restore_os_cursors_safe()
        return payload | {"daemonRunning": False, "spawnedPid": None}

    alive = is_overlay_daemon_alive(force=force_flush)
    must_write = (
        force_flush
        or not bool(prev.get("active", False))
        or not alive
        or cursor_style is not None
        or (now_m - _OVERLAY_LAST_WRITE_MONO) >= _OVERLAY_MIN_WRITE_INTERVAL_SEC
    )
    if must_write:
        _write_overlay_state(payload)
    else:
        _OVERLAY_DIRTY_PAYLOAD = payload

    spawned_pid: int | None = None
    desk = current_desktop_name().lower()
    if OVERLAY_SCRIPT.is_file() and (desk == "default" or force_spawn) and not alive:
        if _OVERLAY_DIRTY_PAYLOAD is not None:
            _write_overlay_state(payload)
        py_dir = Path(sys.executable).resolve().parent
        pyw = py_dir / "pythonw.exe"
        exe = str(pyw if pyw.is_file() else Path(sys.executable))
        flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
        try:
            proc = subprocess.Popen(
                [exe, str(OVERLAY_SCRIPT)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                creationflags=flags,
            )
            _OVERLAY_PROC = proc
            spawned_pid = int(proc.pid)
            _OVERLAY_ALIVE_CACHE = (time.monotonic(), True)
            alive = True
            _atomic_write_json(OVERLAY_PID_FILE, {"pid": spawned_pid, "status": "running", "startedAt": now})
        except Exception:
            pass

    return payload | {"daemonRunning": alive, "spawnedPid": spawned_pid}


_LAST_HANDLED_ESC_TS: float = 0.0
_LAST_REVOKED_ESC_TS: float = 0.0
_PENDING_USER_CANCEL_TS: float = 0.0


def _read_overlay_pid_info() -> dict[str, Any]:
    try:
        if OVERLAY_PID_FILE.is_file():
            data = json.loads(OVERLAY_PID_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _revoke_all_on_emergency(reason_label: str) -> None:
    with _STATE_LOCK:
        if STATE.get("grants"):
            STATE["grants"] = {}
            save_state(STATE)
    update_overlay_hud(False, reason_label, force_flush=True)


def check_emergency_stop(*, check_corner: bool = True) -> None:
    global _LAST_HANDLED_ESC_TS, _PENDING_USER_CANCEL_TS
    if _ABORT_EVENT.is_set():
        raise RuntimeError("mcp_deadline_exceeded: Tool call aborted by watchdog timeout.")
    if is_escape_pressed():
        now_esc = time.time()
        _LAST_HANDLED_ESC_TS = now_esc
        _PENDING_USER_CANCEL_TS = 0.0
        _revoke_all_on_emergency("Idle")
        raise RuntimeError(
            "user_closed_computer_use: Physical Escape key pressed by the user. "
            "Computer Use was closed and all active app grants were revoked. "
            "Stop and check with the user, or call cu_launch_app / cu_grant_app to restart Computer Use when authorized."
        )
    pid_info = _read_overlay_pid_info()
    esc_ts = float(pid_info.get("stoppedAt") or 0.0)
    pending_ts = _PENDING_USER_CANCEL_TS
    if pid_info.get("reason") == "esc" and esc_ts > _LAST_HANDLED_ESC_TS and (time.time() - esc_ts <= _GRANT_IDLE_TTL_SEC):
        pending_ts = max(pending_ts, esc_ts)
    if pending_ts > _LAST_HANDLED_ESC_TS and (time.time() - pending_ts <= _GRANT_IDLE_TTL_SEC):
        ago = max(0, int(round(time.time() - pending_ts)))
        _LAST_HANDLED_ESC_TS = pending_ts
        _PENDING_USER_CANCEL_TS = 0.0
        _revoke_all_on_emergency("Idle")
        raise RuntimeError(
            f"user_closed_computer_use: Computer Use was closed by the user via Escape ({ago}s ago). "
            f"All active app grants were revoked and the overlay was dismissed. "
            f"Stop and check with the user, or call cu_launch_app / cu_grant_app to restart Computer Use."
        )
    if check_corner and _CURSOR_ENGAGED and not _IS_LOW_INTEGRITY and current_desktop_name().lower() == "default":
        px, py = get_cursor_pos()
        for ml, mt, _, _ in _get_monitor_rects():
            if ml <= px <= ml + 1 and mt <= py <= mt + 1:
                _revoke_all_on_emergency("Idle")
                raise RuntimeError("emergency_stop: Pointer moved to monitor upper-left corner failsafe; Computer Use halted.")


def is_process_elevated(pid: int) -> bool:
    if pid <= 0:
        return False
    is_self = pid == os.getpid()
    hproc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not hproc:
        return False if is_self else True  # Fail closed for external/protected/admin processes
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
        wintypes.HWND(hwnd),
        WM_GETTEXT,
        512,
        buf,
        SMTO_ABORTIFHUNG,
        timeout_ms,
        ctypes.byref(res_len),
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


def get_process_info(hwnd: int) -> tuple[int, str, str]:
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    pid_val = int(pid.value)
    if pid_val <= 0:
        return 0, "unknown", ""
    exe = _query_pid_exe(pid_val)
    if exe:
        return pid_val, Path(exe).name, exe
    return pid_val, "unknown", ""


def classify_policy(process_name: str) -> str:
    low = _norm_proc_name(_canonical_proc_or_name(process_name))
    if low in BLOCKED_PROCESSES:
        return "blocked"
    if low in BROWSER_PROCESSES:
        return "look_only"
    return "interactive"


def check_sensitive_editor_title(process_name: str, title: str) -> str | None:
    low_proc = process_name.lower()
    low_title = (title or "").lower()
    is_editor = (
        low_proc in EDITOR_PROCESSES
        or " - notepad" in low_title
        or "visual studio code" in low_title
    )
    if not is_editor:
        return None
    for pat in SENSITIVE_TITLE_PATTERNS:
        m = pat.search(title or "")
        if m:
            return (
                f"sensitive_file_guard: Refusing input to {process_name} because window title "
                f"'{title}' matches protected config/script pattern ({m.group(0).strip()})."
            )
    return None


_PROC_META_CACHE: dict[tuple[int, int], tuple[str, str, bool]] = {}
_NON_EDITOR_TITLE_CACHE: dict[tuple[int, int], tuple[float, str]] = {}


def _get_proc_create_time(pid: int) -> int | None:
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


def _get_cached_proc_meta(hwnd: int) -> tuple[int, str, str, bool]:
    pid_dw = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid_dw))
    pid_val = int(pid_dw.value)
    if pid_val <= 0:
        return 0, "unknown", "", False
    ctime = _get_proc_create_time(pid_val)
    cache_key = (pid_val, ctime) if ctime is not None else None
    if cache_key is not None:
        cached = _PROC_META_CACHE.get(cache_key)
        if cached is not None:
            return pid_val, cached[0], cached[1], cached[2]
    _, proc_name, exe_path = get_process_info(hwnd)
    elev = is_process_elevated(pid_val)
    if cache_key is not None and proc_name != "unknown":
        while len(_PROC_META_CACHE) >= 256:
            _PROC_META_CACHE.pop(next(iter(_PROC_META_CACHE)))
        _PROC_META_CACHE[cache_key] = (proc_name, exe_path, elev)
    return pid_val, proc_name, exe_path, elev


def inspect_hwnd(hwnd: int) -> dict[str, Any] | None:
    if not user32.IsWindow(wintypes.HWND(hwnd)):
        return None
    pid, proc_name, exe_path, is_elev = _get_cached_proc_meta(hwnd)
    low_proc = proc_name.lower()
    if _IN_BATCH_DEPTH > 0 and low_proc not in EDITOR_PROCESSES:
        now_m = time.monotonic()
        t_key = (int(hwnd), pid)
        cached_t = _NON_EDITOR_TITLE_CACHE.get(t_key)
        if cached_t is not None and (now_m - cached_t[0]) < 0.25:
            title = cached_t[1]
        else:
            title = get_window_title_safe(hwnd, timeout_ms=120)
            _NON_EDITOR_TITLE_CACHE[t_key] = (now_m, title)
    else:
        title = get_window_title_safe(hwnd, timeout_ms=120)
    bounds = get_window_bounds(hwnd)
    fg = get_foreground_hwnd()
    tier = classify_policy(exe_path or proc_name)
    with _STATE_LOCK:
        raw_grant = STATE.get("grants", {}).get(low_proc)
        grant = dict(raw_grant) if isinstance(raw_grant, dict) else None
    return {
        "hwnd": int(hwnd),
        "pid": pid,
        "processName": proc_name,
        "exePath": exe_path,
        "title": title,
        "bounds": bounds,
        "isForeground": int(hwnd) == fg,
        "isMinimized": bool(user32.IsIconic(wintypes.HWND(hwnd))),
        "isVisible": bool(user32.IsWindowVisible(wintypes.HWND(hwnd))) and not is_window_cloaked(hwnd),
        "isElevated": is_elev,
        "policyTier": tier,
        "granted": grant is not None,
        "grantMode": grant.get("mode") if grant is not None else None,
    }


def require_window_permission(hwnd: int, *, need_input: bool) -> tuple[dict[str, Any], str | None]:
    sync_state()
    if is_desktop_locked():
        return {}, "desktop_locked: Windows desktop is locked. Ask the user to unlock before using Computer Use."
    info = inspect_hwnd(hwnd)
    if info is None or not info["isVisible"]:
        return {}, f"window_unavailable: HWND {hwnd} is no longer open or visible."
    proc_low = info["processName"].lower()
    tier = info["policyTier"]
    if tier == "blocked":
        return info, f"policy_blocked: Process '{info['processName']}' is on the mandatory Computer Use deny-list (terminals, password managers, OS security, and agent UIs are blocked)."
    with _STATE_LOCK:
        grant = STATE.get("grants", {}).get(proc_low)
        if grant is None:
            return info, f"app_not_granted: Process '{info['processName']}' (HWND {hwnd}) has not been granted access in this session. Call cu_grant_app first."
        grant["lastUsedEpoch"] = time.time()
        grant_mode = grant.get("mode")
    if need_input:
        if grant_mode == "look_only":
            return info, f"tier_look_only: '{info['processName']}' is granted in look_only mode (prefer a dedicated browser automation tool, or explicitly re-grant with allow_browser_input=true if user-authorized)."
        if info["isElevated"] and not is_process_elevated(os.getpid()):
            return info, f"elevated_window_uipi: Target '{info['processName']}' is running elevated (Administrator); Windows UIPI blocks non-elevated SendInput."
        sens_err = check_sensitive_editor_title(info["processName"], info["title"])
        if sens_err:
            return info, sens_err
    return info, None


def _detect_active_popup(owner_hwnd: int) -> int:
    """Detect an active modal dialog, dropdown, or context menu owned by owner_hwnd's thread/PID."""
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


_PRINTWINDOW_POOL_LOCK = threading.Lock()
_PRINTWINDOW_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="cua-pw")


def _get_printwindow_pool() -> ThreadPoolExecutor:
    global _PRINTWINDOW_POOL
    with _PRINTWINDOW_POOL_LOCK:
        if _PRINTWINDOW_POOL is None:
            _PRINTWINDOW_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="cua-pw")
        return _PRINTWINDOW_POOL


def _printwindow_worker(hwnd: int, w: int, h: int, pw_flags: int) -> bytes | None:
    """Own GDI DC & DIBSection entirely inside worker so timeout never frees handles under a running PrintWindow."""
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


def capture_window_printwindow(hwnd: int) -> tuple[Image.Image | None, dict[str, int]]:
    global _PRINTWINDOW_POOL
    rect = RECT()
    user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect))
    w = int(rect.right - rect.left)
    h = int(rect.bottom - rect.top)
    fallback_origin = {"left": int(rect.left), "top": int(rect.top), "width": w, "height": h}
    if w <= 4 or h <= 4 or user32.IsIconic(wintypes.HWND(hwnd)):
        return None, fallback_origin

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
            return None, fallback_origin
        if not raw:
            continue
        candidate = Image.frombuffer("RGBA", (w, h), raw, "raw", "BGRA", 0, 1).convert("RGB")
        if _image_has_signal(candidate):
            img = candidate
            break

    if img is None:
        return None, fallback_origin

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
        return None, origin
    return img, origin


def capture_desktop_gdi() -> tuple[Image.Image, dict[str, int]]:
    """Fast ~8ms zero-dependency Win32 GDI BitBlt virtual-desktop capture (mss-equivalent)."""
    vb = virtual_bounds()
    w, h = max(1, vb["width"]), max(1, vb["height"])
    hdc_screen = None
    hdc_mem = None
    hbmp = None
    old_obj = None
    try:
        hdc_screen = user32.GetDC(None)
        if not hdc_screen:
            return ImageGrab.grab(all_screens=True), vb
        hdc_mem = gdi32.CreateCompatibleDC(wintypes.HDC(hdc_screen))
        if not hdc_mem:
            return ImageGrab.grab(all_screens=True), vb
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h  # top-down DIB
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        bits_ptr = ctypes.c_void_p()
        hbmp = gdi32.CreateDIBSection(
            wintypes.HDC(hdc_mem), ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(bits_ptr), None, 0
        )
        if not hbmp or not bits_ptr.value:
            return ImageGrab.grab(all_screens=True), vb
        old_obj = gdi32.SelectObject(wintypes.HDC(hdc_mem), wintypes.HGDIOBJ(hbmp))
        ok = gdi32.BitBlt(
            wintypes.HDC(hdc_mem), 0, 0, w, h,
            wintypes.HDC(hdc_screen), vb["left"], vb["top"], SRCCOPY
        )
        if not ok:
            return ImageGrab.grab(all_screens=True), vb
        raw = ctypes.string_at(bits_ptr.value, w * h * 4)
        img = Image.frombuffer("RGBA", (w, h), raw, "raw", "BGRA", 0, 1).convert("RGB")
        return img, vb
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


def _get_som_font() -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
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


def _image_has_signal(image: Image.Image) -> bool:
    """Fast <0.15ms non-black surface check via 64x64 thumbnail instead of full-frame ImageStat."""
    thumb = image.resize((64, 64), Image.Resampling.NEAREST)
    ext = thumb.getextrema()
    if isinstance(ext[0], tuple):
        return any(hi > 0 for _, hi in ext)
    return ext[1] > 0


_LAST_FRAME_HASHES: dict[str, int] = {}


def compute_dhash(image: Image.Image) -> tuple[str, int]:
    """Compute a 64-bit perceptual difference hash in <0.2ms for instant UI change detection."""
    small = image.convert("L").resize((9, 8), Image.Resampling.BILINEAR)
    px = list(small.getdata())
    bits = 0
    for row in range(8):
        off = row * 9
        for col in range(8):
            bits = (bits << 1) | (1 if px[off + col] < px[off + col + 1] else 0)
    return f"{bits:016x}", bits


def annotate_som_elements(
    image: Image.Image,
    origin: dict[str, int],
    scale_x: float,
    scale_y: float,
    elements: list[dict[str, Any]],
) -> Image.Image:
    """Overlay crisp zero-ML Set-of-Mark (SoM) [index] badges onto screenshot from cached UIA/Win32 bounds."""
    annotated = image.convert("RGBA")
    overlay = Image.new("RGBA", annotated.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = _get_som_font()

    palette = [
        (14, 165, 233),
        (16, 185, 129),
        (245, 158, 11),
        (168, 85, 247),
        (236, 72, 153),
    ]
    iw, ih = annotated.size
    for el in elements[:60]:
        b = el.get("bounds")
        if not b:
            continue
        x1 = int(round((b["left"] - origin["left"]) / scale_x))
        y1 = int(round((b["top"] - origin["top"]) / scale_y))
        x2 = int(round((b["left"] + b["width"] - origin["left"]) / scale_x))
        y2 = int(round((b["top"] + b["height"] - origin["top"]) / scale_y))
        if x2 <= 2 or y2 <= 2 or x1 >= iw - 2 or y1 >= ih - 2:
            continue
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(iw - 1, x2), min(ih - 1, y2)
        if (x2 - x1) > iw * 0.92 and (y2 - y1) > ih * 0.92:
            continue  # Skip full-window root pane clutter
        idx = int(el["index"])
        rgb = palette[idx % len(palette)]
        draw.rectangle((x1, y1, x2, y2), outline=(*rgb, 185), width=1)
        tag = str(idx)
        tb = draw.textbbox((0, 0), tag, font=font)
        tw, th = (tb[2] - tb[0]) + 8, (tb[3] - tb[1]) + 4
        bx1 = max(0, min(iw - tw, x1))
        by1 = max(0, y1 - th if y1 >= th else y1)
        draw.rounded_rectangle((bx1, by1, bx1 + tw, by1 + th), radius=4, fill=(*rgb, 235), outline=(255, 255, 255, 230), width=1)
        draw.text((bx1 + 4, by1 + 1 - tb[1]), tag, font=font, fill=(255, 255, 255, 255))

    annotated.alpha_composite(overlay)
    return annotated.convert("RGB")


def encode_image(image: Image.Image, obs_id: str, fmt: str = "png", quality: int = 82) -> tuple[str, str, str, int]:
    """Encode screenshot rapidly in PNG (compress_level=1), WebP, or JPEG."""
    ensure_runtime_dirs()
    fmt_low = (fmt or "png").strip().lower()
    rgb = image.convert("RGB")
    buf = io.BytesIO()
    if fmt_low == "webp":
        ext, mime = "webp", "image/webp"
        rgb.save(buf, format="WEBP", quality=max(40, min(int(quality), 95)), method=0)
    elif fmt_low in ("jpg", "jpeg"):
        ext, mime = "jpg", "image/jpeg"
        rgb.save(buf, format="JPEG", quality=max(40, min(int(quality), 95)))
    else:
        ext, mime = "png", "image/png"
        rgb.save(buf, format="PNG", compress_level=1)
    raw = buf.getvalue()
    out_path = SHOTS_DIR / f"{obs_id}.{ext}"
    out_path.write_bytes(raw)
    try:
        shots = [p for p in SHOTS_DIR.iterdir() if p.is_file() and p != out_path]
        if len(shots) > 50:
            shots.sort(key=lambda p: p.stat().st_mtime)
            for old_p in shots[:-50]:
                try:
                    old_p.unlink()
                except OSError:
                    pass
    except Exception:
        pass
    return base64.b64encode(raw).decode("ascii"), str(out_path), mime, len(raw)


def encode_png(image: Image.Image, save_name: str) -> tuple[str, str]:
    obs_stem = save_name[:-4] if save_name.lower().endswith(".png") else save_name
    b64, path, _, _ = encode_image(image, obs_stem, fmt="png")
    return b64, path


def record_observation(obs: dict[str, Any]) -> str:
    sync_state()
    obs_id = f"obs_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
    obs["observationId"] = obs_id
    obs["timestamp"] = time.time()
    _store_observation(obs_id, obs)
    if not _STDIO_MODE:
        save_state(STATE)
    return obs_id


def resolve_screen_coords(arguments: dict[str, Any], x_key: str = "x", y_key: str = "y") -> tuple[int, int, int | None]:
    raw_x = float(arguments[x_key])
    raw_y = float(arguments[y_key])
    obs_id = arguments.get("observation_id")
    coord_space = str(arguments.get("coordinate_space", "observation" if obs_id else "screen"))
    if obs_id:
        sync_state()
        obs = _get_observation(str(obs_id))
        if not obs:
            raise ValueError(f"stale_observation: observation_id '{obs_id}' not found. Capture a fresh screenshot or UI tree.")
        hwnd = obs.get("hwnd")
        if hwnd is not None:
            cur = get_window_bounds(int(hwnd))
            orig = obs.get("bounds") or {}
            if abs(cur["left"] - orig.get("left", cur["left"])) > 4 or abs(cur["top"] - orig.get("top", cur["top"])) > 4 or abs(cur["width"] - orig.get("width", cur["width"])) > 4:
                raise ValueError(f"window_moved: Target window moved/resized since '{obs_id}'. Capture a fresh screenshot before clicking.")
        if coord_space == "observation":
            sx = int(round(obs["originX"] + raw_x * obs["scaleX"]))
            sy = int(round(obs["originY"] + raw_y * obs["scaleY"]))
            return sx, sy, hwnd
        if coord_space == "window" and hwnd is not None:
            b = get_window_bounds(int(hwnd))
            return int(round(b["left"] + raw_x)), int(round(b["top"] + raw_y)), int(hwnd)
    return int(round(raw_x)), int(round(raw_y)), (int(arguments["hwnd"]) if "hwnd" in arguments and arguments["hwnd"] is not None else None)


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


def verify_point_belongs_to_hwnd(sx: int, sy: int, target_hwnd: int) -> tuple[bool, int]:
    pt = POINT(sx, sy)
    hit = int(user32.WindowFromPoint(pt) or 0)
    if not hit:
        return False, 0
    root = int(user32.GetAncestor(wintypes.HWND(hit), GA_ROOT) or hit)
    if root == int(target_hwnd):
        return True, root
    # Allow owned popup/dialog of the same PID
    t_pid, _, _ = get_process_info(target_hwnd)
    h_pid, _, _ = get_process_info(root)
    return (t_pid > 0 and t_pid == h_pid), root


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
    if sent != 1 and not _IS_LOW_INTEGRITY:
        raise RuntimeError("SendInput mouse button event failed (blocked by UIPI or inactive desktop).")


def send_mouse_click(button: str = "left", clicks: int = 1, interval: float = 0.0) -> None:
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
        if sent != 2 and not _IS_LOW_INTEGRITY:
            raise RuntimeError(f"SendInput mouse click failed (sent {sent}/2 events).")
        if i + 1 < count and interval > 0:
            time.sleep(interval)


def send_mouse_wheel(clicks: int, *, horizontal: bool = False) -> None:
    steps = max(-50, min(int(clicks), 50))
    if steps == 0:
        return
    inp = INPUT()
    inp.type = INPUT_MOUSE
    flag = MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL
    inp.union.mi = MOUSEINPUT(0, 0, wintypes.DWORD((steps * WHEEL_DELTA) & 0xFFFFFFFF), flag, 0, _CUA_INPUT_TAG)
    sent = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    if sent != 1 and not _IS_LOW_INTEGRITY and current_desktop_name().lower() == "default":
        raise RuntimeError("SendInput mouse wheel event failed.")


def _ensure_child_canvas_focus(sx: int, sy: int, root_hwnd: int) -> None:
    """Sync child HWND focus via Win32 GetGUIThreadInfo so hosted child surfaces do not swallow first click/drag."""
    if _IS_LOW_INTEGRITY or current_desktop_name().lower() != "default":
        return
    try:
        hit = int(user32.WindowFromPoint(POINT(sx, sy)) or 0)
        if not hit or hit == int(root_hwnd) or not user32.IsWindowEnabled(wintypes.HWND(hit)):
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
    """Resolve key token to (vk, is_extended, required_shift_mask) using target HKL."""
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
    """Dispatch a single key or modifier chord via pure Win32 SendInput honoring target thread HKL & shift masks."""
    hkl = 0
    if target_hwnd > 0:
        tid = user32.GetWindowThreadProcessId(wintypes.HWND(target_hwnd), None)
        if tid:
            hkl = int(user32.GetKeyboardLayout(tid) or 0)

    resolved = [_resolve_vk_token(k, hkl=hkl) for k in keys]
    present_vks = {vk for vk, _, _ in resolved}
    seq: list[tuple[int, bool]] = []
    # Auto-add Shift/Ctrl/Alt modifiers when a single printable token (like '+') requires them on active HKL
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
    if sent != len(inputs) and not _IS_LOW_INTEGRITY and current_desktop_name().lower() == "default":
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
    if sent != len(inputs) and not _IS_LOW_INTEGRITY and current_desktop_name().lower() == "default":
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
    """Dispatch a plain Unicode substring in a single batched Win32 SendInput syscall (~20x faster)."""
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


# Embedded .NET UI Automation script (parameters injected before execution)
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

$keepTypes = @(
    'Button','Edit','Document','ComboBox','CheckBox','RadioButton',
    'MenuItem','TabItem','ListItem','Hyperlink','TreeItem','Slider','SplitButton','DataItem'
)

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
                if ($null -eq $focusedStr -and $c.HasKeyboardFocus) {
                    $focusedStr = "$ct '$name'"
                }
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
                            index = $idx
                            depth = $depth
                            controlType = $ct
                            name = $name
                            automationId = $aid
                            nativeHwnd = [int64]$c.NativeWindowHandle
                            enabled = [bool]$c.IsEnabled
                            value = $val
                            bounds = @{
                                left = [int]$r.Left
                                top = [int]$r.Top
                                width = [int]$r.Width
                                height = [int]$r.Height
                                cx = [int]($r.Left + $r.Width / 2)
                                cy = [int]($r.Top + $r.Height / 2)
                            }
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
        text_val = get_window_title_safe(child_hwnd, timeout_ms=80)
        cls_name = cls_buf.value or "Control"
        found.append(
            {
                "index": start_index + len(found),
                "depth": 1,
                "controlType": f"Win32:{cls_name}",
                "name": text_val,
                "automationId": f"hwnd:{child_hwnd}",
                "nativeHwnd": child_hwnd,
                "enabled": bool(user32.IsWindowEnabled(wintypes.HWND(child_hwnd))),
                "value": text_val if text_val else None,
                "bounds": {
                    "left": int(rect.left),
                    "top": int(rect.top),
                    "width": w,
                    "height": h,
                    "cx": int(rect.left + w // 2),
                    "cy": int(rect.top + h // 2),
                },
            }
        )
        return True

    cb_ref = WNDENUMPROC(cb)
    user32.EnumChildWindows(wintypes.HWND(hwnd), cb_ref, 0)
    return found


_UIA_LOCK = threading.Lock()
_UIA_PROC: subprocess.Popen[str] | None = None


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
foreach ($t in @('Button','Edit','Document','ComboBox','CheckBox','RadioButton','MenuItem','TabItem','ListItem','Hyperlink','TreeItem','Slider','SplitButton','DataItem')) {
    [void]$keepSet.Add($t)
}

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
        if ($req.ContainsKey('ping')) {
            [Console]::Out.WriteLine('{"pong":true}')
            [Console]::Out.Flush()
            continue
        }
        $hwnd = [int64]$req['hwnd']
        $maxElements = if ($req.ContainsKey('maxElements')) { [int]$req['maxElements'] } else { 80 }
        $setValueIndex = if ($req.ContainsKey('setValueIndex')) { [int]$req['setValueIndex'] } else { -1 }
        $valueText = if ($req.ContainsKey('valueText') -and $null -ne $req['valueText']) { [string]$req['valueText'] } else { '' }
        $invokeIndex = if ($req.ContainsKey('invokeIndex')) { [int]$req['invokeIndex'] } else { -1 }

        $cacheReq.Push()
        try {
            $root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$hwnd)
        } finally {
            $cacheReq.Pop()
        }
        if ($null -eq $root) { throw "Cannot bind AutomationElement for HWND $hwnd" }

        $focusedStr = $null

        $queue = New-Object System.Collections.Generic.Queue[object]
        $queue.Enqueue(@{ el = $root; depth = 0 })
        $items = New-Object System.Collections.Generic.List[object]
        $earlyResp = $null

        while ($queue.Count -gt 0 -and $items.Count -lt $maxElements -and $null -eq $earlyResp) {
            $cur = $queue.Dequeue()
            $el = $cur.el
            $depth = [int]$cur.depth
            if ($depth -gt 0) {
                try {
                    $c = $el.Cached
                    if ($null -ne $c.ControlType) {
                        $ct = $c.ControlType.ProgrammaticName.Replace('ControlType.','')
                        $name = [string]$c.Name
                        if ($null -eq $focusedStr -and $c.HasKeyboardFocus) {
                            $focusedStr = "$ct '$name'"
                        }
                        $r = $c.BoundingRectangle
                        $rectOk = (-not $r.IsEmpty) -and (-not [double]::IsNaN($r.Left)) -and (-not [double]::IsInfinity($r.Left)) -and (-not [double]::IsNaN($r.Top)) -and (-not [double]::IsInfinity($r.Top)) -and (-not [double]::IsNaN($r.Width)) -and (-not [double]::IsInfinity($r.Width)) -and (-not [double]::IsNaN($r.Height)) -and (-not [double]::IsInfinity($r.Height))
                        if (-not $c.IsOffscreen -and $rectOk -and $r.Width -gt 2 -and $r.Height -gt 2) {
                            $aid = [string]$c.AutomationId
                            if ($keepSet.Contains($ct) -or ($name.Length -gt 0 -and $depth -le 4)) {
                                $isPassword = $false
                                try { $isPassword = [bool]$c.IsPassword } catch { $isPassword = $true }
                                $val = $null
                                $vpObj = $null
                                $checkVal = ($setValueIndex -eq $items.Count) -or ($ct -in @('Edit','ComboBox','Document','Slider'))
                                if ($checkVal -and $el.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$vpObj)) {
                                    if (-not $isPassword) {
                                        try { $val = ([System.Windows.Automation.ValuePattern]$vpObj).Current.Value } catch {}
                                        if ($null -ne $val -and $val.Length -gt 120) { $val = $val.Substring(0, 120) + '...' }
                                    }
                                }
                                $idx = $items.Count
                                $cx = [int]($r.Left + $r.Width / 2)
                                $cy = [int]($r.Top + $r.Height / 2)
                                if ($setValueIndex -eq $idx) {
                                    if ($null -ne $vpObj) {
                                        ([System.Windows.Automation.ValuePattern]$vpObj).SetValue($valueText)
                                        $earlyResp = @{ action = 'set_value'; method = 'ValuePattern'; index = $idx; name = $name }
                                    } else {
                                        $earlyResp = @{ action = 'set_value'; method = 'fallback_needed'; index = $idx; cx = $cx; cy = $cy }
                                    }
                                    break
                                }
                                if ($invokeIndex -eq $idx) {
                                    $ipObj = $null
                                    if ($el.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$ipObj)) {
                                        ([System.Windows.Automation.InvokePattern]$ipObj).Invoke()
                                        $earlyResp = @{ action = 'invoke'; method = 'InvokePattern'; index = $idx; name = $name }
                                    } else {
                                        $earlyResp = @{ action = 'invoke'; method = 'fallback_click'; index = $idx; cx = $cx; cy = $cy }
                                    }
                                    break
                                }
                                $items.Add(@{
                                    index = $idx; depth = $depth; controlType = $ct; name = $name; automationId = $aid;
                                    nativeHwnd = [int64]$c.NativeWindowHandle; enabled = [bool]$c.IsEnabled; value = $val;
                                    bounds = @{ left = [int]$r.Left; top = [int]$r.Top; width = [int]$r.Width; height = [int]$r.Height; cx = $cx; cy = $cy }
                                })
                            }
                        }
                    }
                } catch {}
            }
            if ($depth -lt 8 -and $null -eq $earlyResp) {
                try {
                    $children = $el.CachedChildren
                    if ($null -ne $children) {
                        foreach ($ch in $children) {
                            try {
                                $uch = $ch.GetUpdatedCache($cacheReq)
                                $queue.Enqueue(@{ el = $uch; depth = ($depth + 1) })
                            } catch {}
                        }
                    }
                } catch {}
            }
        }
        $outObj = if ($null -ne $earlyResp) { $earlyResp } else { @{ focusedElement = $focusedStr; count = $items.Count; elements = $items } }
        [Console]::Out.WriteLine($json.Serialize($outObj))
        [Console]::Out.Flush()
    } catch {
        [Console]::Out.WriteLine($json.Serialize(@{ error = $_.Exception.Message }))
        [Console]::Out.Flush()
    }
}
"""


_CSC_ATTEMPTED = False
_CSC_EXE_CACHE: Path | None = None


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _ensure_compiled_uia_bridge() -> Path | None:
    """Compile uia_fast_bridge.cs once via built-in .NET Framework csc.exe for ~8-15ms UIA queries."""
    global _CSC_ATTEMPTED, _CSC_EXE_CACHE
    if _CSC_ATTEMPTED:
        return _CSC_EXE_CACHE
    _CSC_ATTEMPTED = True
    ensure_runtime_dirs()
    exe_path = RUNTIME_DIR / "uia_fast_bridge.exe"
    sha_path = RUNTIME_DIR / "uia_fast_bridge.sha256"
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
    if _IS_LOW_INTEGRITY and "CUA_RUNTIME_DIR" not in os.environ:
        return None
    windir = Path(os.environ.get("WINDIR") or r"C:\Windows")
    fx_dir = windir / "Microsoft.NET" / "Framework64" / "v4.0.30319"
    if not (fx_dir / "csc.exe").is_file():
        fx_dir = windir / "Microsoft.NET" / "Framework" / "v4.0.30319"
    csc = fx_dir / "csc.exe"
    if not csc.is_file() or not UIA_CS_FILE.is_file():
        return None
    wpf_dir = str(fx_dir / "WPF")
    cmd = [
        str(csc),
        "/nologo",
        "/optimize+",
        "/target:exe",
        f"/out:{exe_path}",
        f"/lib:{wpf_dir}",
        "/r:UIAutomationClient.dll",
        "/r:UIAutomationTypes.dll",
        "/r:WindowsBase.dll",
        "/r:System.Web.Extensions.dll",
        str(UIA_CS_FILE),
    ]
    flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    env = os.environ.copy()
    env["TMP"] = str(RUNTIME_DIR)
    env["TEMP"] = str(RUNTIME_DIR)
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10, creationflags=flags, env=env)
        if res.returncode == 0 and exe_path.is_file():
            try:
                sha_path.write_text(f"{cs_sha}:{_file_sha256(exe_path)}\n", encoding="utf-8")
            except OSError:
                pass
            _CSC_EXE_CACHE = exe_path
            return exe_path
    except Exception:
        pass
    return None


_UIA_OUT_Q: queue.Queue[str] | None = None


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

    t = threading.Thread(target=_pump, name="cua-uia-reader", daemon=True)
    t.start()
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
    """Boot or reuse persistent compiled C# UIA worker with non-blocking queue reader."""
    global _UIA_PROC, _UIA_OUT_Q
    if _UIA_PROC is not None and _UIA_PROC.poll() is None and _UIA_OUT_Q is not None:
        return _UIA_PROC
    flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))

    # 1. Try native compiled C# bridge first (~8-15ms/tree)
    compiled_exe = _ensure_compiled_uia_bridge()
    if compiled_exe is not None:
        proc: subprocess.Popen[str] | None = None
        try:
            proc = subprocess.Popen(
                [str(compiled_exe), "--parent-pid", str(os.getpid())],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                creationflags=flags,
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

    # 2. Fallback to warm PowerShell daemon (-File first, then -Command if Group Policy blocks -File)
    daemon_ps1 = Path(__file__).resolve().parent / "uia_daemon.ps1"
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
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                creationflags=flags,
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
        # Modal-blocked disabled windows can wedge cross-process COM calls; return empty cleanly unless explicit action
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
                try:
                    proc.kill()
                except Exception:
                    pass
                _UIA_PROC = None
                _UIA_OUT_Q = None

    if modal_blocked:
        return {"focusedElement": None, "count": 0, "modalBlocked": True, "elements": []}

    # Fallback to cold PowerShell if warm bridge ever fails
    val_b64 = base64.b64encode(value_text.encode("utf-8")).decode("ascii")
    invocation = (
        f"$Hwnd = [int64]{int(hwnd)}; $MaxElements = {int(max_elements)}; "
        f"$SetValueIndex = {int(set_value_index)}; "
        f"$ValueText = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{val_b64}')); "
        f"$InvokeIndex = {int(invoke_index)};\n"
    )
    full_ps = invocation + UIA_PS_SCRIPT
    encoded = base64.b64encode(full_ps.encode("utf-16-le")).decode("ascii")
    cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
    flags_ps = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    cproc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=12, creationflags=flags_ps)
    if cproc.returncode != 0:
        raise RuntimeError(f"UIA helper failed: {cproc.stderr.strip() or cproc.stdout.strip()}")
    raw_out = cproc.stdout.strip()
    return json.loads(raw_out) if raw_out else {"focusedElement": None, "count": 0, "elements": []}


_WOKE_CHROMIUM_HWNDS: set[int] = set()


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


def _wake_chromium_accessibility(hwnd: int) -> None:
    """Wake sleeping Chromium/Electron renderer accessibility (IAccessible/UIA) in ~15ms via OBJID_CLIENT."""
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


def run_uia_helper(hwnd: int, *, max_elements: int = 100, set_value_index: int = -1, value_text: str = "", invoke_index: int = -1) -> dict[str, Any]:
    _wake_chromium_accessibility(hwnd)
    parsed = _query_uia_raw(
        hwnd,
        max_elements=max_elements,
        set_value_index=set_value_index,
        value_text=value_text,
        invoke_index=invoke_index,
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


# Clipboard helpers (Win32 CF_UNICODETEXT)
def _open_clipboard_with_retry(retries: int = 8, delay_sec: float = 0.025) -> bool:
    for _ in range(retries):
        if user32.OpenClipboard(None):
            return True
        time.sleep(delay_sec)
    return False


# MCP response helpers
def text_content(text: str) -> dict[str, str]:
    return {"type": "text", "text": text}


def tool_result(
    text: str,
    *,
    structured: dict[str, Any] | None = None,
    image_b64: str | None = None,
    image_mime: str = "image/png",
) -> dict[str, Any]:
    content: list[dict[str, Any]] = [text_content(text)]
    if image_b64 is not None:
        content.append({"type": "image", "data": image_b64, "mimeType": image_mime})
    res: dict[str, Any] = {"content": content, "isError": False}
    if structured is not None:
        res["structuredContent"] = structured
    return res


def error_result(message: str) -> dict[str, Any]:
    return {"content": [text_content(message)], "isError": True}


def _build_window_entry(hwnd: int, title: str, fg_hwnd: int) -> dict[str, Any]:
    minimized = bool(user32.IsIconic(wintypes.HWND(hwnd)))
    bounds = get_window_bounds(hwnd)
    pid, proc_name, exe_path = get_process_info(hwnd)
    tier = classify_policy(exe_path or proc_name)
    with _STATE_LOCK:
        raw_grant = STATE.get("grants", {}).get(proc_name.lower())
        grant = dict(raw_grant) if isinstance(raw_grant, dict) else None
    return {
        "hwnd": hwnd,
        "pid": pid,
        "processName": proc_name,
        "exePath": exe_path,
        "title": title,
        "foreground": hwnd == fg_hwnd,
        "minimized": minimized,
        "elevated": is_process_elevated(pid),
        "policyTier": tier,
        "granted": grant is not None,
        "grantMode": grant.get("mode") if grant is not None else None,
        "bounds": bounds,
    }


# Tool Handlers
def cu_status(_: dict[str, Any]) -> dict[str, Any]:
    sync_state()
    px, py = get_cursor_pos()
    fg = get_foreground_hwnd()
    fg_info = inspect_hwnd(fg) if fg else None
    with _STATE_LOCK:
        grants_copy = dict(STATE.get("grants", {}))
    data = {
        "platform": sys.platform,
        "dpiAware": "PerMonitorV2",
        "serverVersion": SERVER_VERSION,
        "desktopName": current_desktop_name(),
        "desktopLocked": is_desktop_locked(),
        "virtualDesktop": virtual_bounds(),
        "pointer": {"x": px, "y": py},
        "foregroundWindow": fg_info,
        "grantedApps": grants_copy,
        "overlay": read_overlay_state() | {"daemonRunning": is_overlay_daemon_alive()},
    }
    return tool_result(json.dumps(data, ensure_ascii=False), structured=data)


def cu_list_windows(arguments: dict[str, Any]) -> dict[str, Any]:
    sync_state()
    max_results = max(1, min(int(arguments.get("max_results", 60)), 200))
    include_minimized = bool(arguments.get("include_minimized", True))
    fg = get_foreground_hwnd()
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for hwnd, title in enum_visible_windows(require_title=True):
        if len(out) >= max_results:
            break
        if hwnd in seen:
            continue
        minimized = bool(user32.IsIconic(wintypes.HWND(hwnd)))
        if minimized and not include_minimized:
            continue
        bounds = get_window_bounds(hwnd)
        if not minimized and (bounds["width"] < 20 or bounds["height"] < 20):
            continue
        seen.add(hwnd)
        out.append(_build_window_entry(hwnd, title, fg))
    data = {"count": len(out), "windows": out}
    return tool_result(json.dumps(data, ensure_ascii=False), structured=data)


def cu_wait_for_window(arguments: dict[str, Any]) -> dict[str, Any]:
    sync_state()
    title_contains = str(arguments.get("title_contains") or "").strip()
    process_name = str(arguments.get("process_name") or "").strip()
    if not title_contains and not process_name:
        return error_result("Provide at least one filter: title_contains and/or process_name.")
    timeout_sec = max(0.5, min(float(arguments.get("timeout_sec", 5.0)), 15.0))
    include_minimized = bool(arguments.get("include_minimized", True))

    title_needle = title_contains.lower()
    proc_needle = process_name.lower()

    t0 = time.monotonic()
    deadline = t0 + timeout_sec
    while True:
        sync_state()
        fg = get_foreground_hwnd()
        matches: list[dict[str, Any]] = []
        for hwnd, title in enum_visible_windows(require_title=True):
            minimized = bool(user32.IsIconic(wintypes.HWND(hwnd)))
            if minimized and not include_minimized:
                continue
            bounds = get_window_bounds(hwnd)
            if not minimized and (bounds["width"] < 20 or bounds["height"] < 20):
                continue
            if title_needle and title_needle not in title.lower():
                continue
            entry = _build_window_entry(hwnd, title, fg)
            if proc_needle:
                proc_low = entry["processName"].lower()
                if proc_low != proc_needle and not (not proc_needle.endswith(".exe") and proc_low == f"{proc_needle}.exe"):
                    continue
            matches.append(entry)

        if matches:
            data = {
                "matched": True,
                "elapsedSec": round(time.monotonic() - t0, 3),
                "count": len(matches),
                "window": matches[0],
                "windows": matches,
            }
            return tool_result(json.dumps(data, ensure_ascii=False), structured=data)

        if time.monotonic() >= deadline:
            break
        time.sleep(0.12)

    return error_result(
        f"timeout: No visible non-cloaked window matched title_contains={title_contains!r} "
        f"process_name={process_name!r} within {timeout_sec:.1f}s."
    )


def cu_grant_app(arguments: dict[str, Any]) -> dict[str, Any]:
    global STATE
    check_emergency_stop(check_corner=False)
    sync_state()
    proc_name = str(arguments.get("process_name") or "").strip()
    hwnd = arguments.get("hwnd")
    if not proc_name and hwnd is not None:
        info = inspect_hwnd(int(hwnd))
        if info is None:
            return error_result(f"HWND {hwnd} not found.")
        proc_name = info["processName"]
    if not proc_name:
        return error_result("Provide process_name (e.g. 'notepad.exe') or hwnd.")
    low = _norm_proc_name(proc_name)
    tier = classify_policy(proc_name)
    if tier == "blocked":
        return error_result(f"policy_blocked: '{proc_name}' cannot be granted (hard-blocked security/terminal/agent category).")
    allow_browser_input = bool(arguments.get("allow_browser_input", False))
    mode = "interactive" if (tier == "interactive" or allow_browser_input) else "look_only"
    now_ep = time.time()
    with _STATE_LOCK:
        STATE["grants"][low] = {
            "processName": low if "." not in proc_name else proc_name,
            "mode": mode,
            "grantedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ep)),
            "grantedEpoch": now_ep,
            "lastUsedEpoch": now_ep,
            "ttlSec": _GRANT_IDLE_TTL_SEC,
            "serverPid": os.getpid() if _STDIO_MODE else 0,
            "serverBootId": _SERVER_BOOT_ID,
        }
        save_state(STATE)
        grant_rec = dict(STATE["grants"][low])
    update_overlay_hud(True, "Active", ttl_sec=_HUD_IDLE_TIMEOUT_SEC, force_flush=True)
    return tool_result(f"Granted '{grant_rec['processName']}' ({mode}).", structured=grant_rec)


def cu_revoke_app(arguments: dict[str, Any]) -> dict[str, Any]:
    global STATE, _PENDING_USER_CANCEL_TS
    sync_state()
    if arguments.get("all"):
        _PENDING_USER_CANCEL_TS = 0.0
        with _STATE_LOCK:
            STATE["grants"] = {}
            STATE["observations"] = {}
            save_state(STATE)
        _purge_session_artifacts()
        update_overlay_hud(False, "Idle", force_flush=True)
        return tool_result("Revoked all Computer Use app grants.", structured={"grants": {}})
    proc_name = _norm_proc_name(str(arguments.get("process_name") or ""))
    with _STATE_LOCK:
        removed = STATE["grants"].pop(proc_name, None)
        save_state(STATE)
        has_more = bool(STATE["grants"])
    if not has_more:
        update_overlay_hud(False, "Idle", force_flush=True)
    else:
        update_overlay_hud(True, "Active", ttl_sec=_HUD_IDLE_TIMEOUT_SEC, force_flush=True)
    return tool_result(f"Revoked '{proc_name}' (was_granted={removed is not None}).")


def _window_matches_app_launch(inf: dict[str, Any] | None, app_key: str, proc_hints: tuple[str, ...]) -> bool:
    if not inf or inf.get("policyTier") == "blocked":
        return False
    p_low = inf["processName"].lower()
    if p_low in proc_hints:
        return True
    p_key = _norm_app_key(p_low)
    t_key = _norm_app_key(inf.get("title") or "")
    if len(app_key) >= 3 and (p_key == app_key or p_key.startswith(app_key) or t_key == app_key or app_key in t_key):
        return True
    return False


def cu_launch_app(arguments: dict[str, Any]) -> dict[str, Any]:
    check_emergency_stop()
    app = str(arguments["app"]).strip()
    auto_grant = bool(arguments.get("auto_grant", True))
    allow_browser_input = bool(arguments.get("allow_browser_input", False))
    reuse_existing = bool(arguments.get("reuse_existing", True))
    extra_args = [str(a) for a in (arguments.get("args") or []) if a is not None]

    if classify_policy(app) == "blocked" or _norm_proc_name(_canonical_proc_or_name(app)) in _BLOCKED_LAUNCH_RUNTIMES:
        return error_result(f"policy_blocked: '{app}' is on the mandatory deny-list.")

    cmd, proc_hints = _resolve_known_or_installed_app(app)
    if cmd is None:
        return error_result(
            f"Could not find installed app '{app}' via Windows App Paths, Uninstall registry, "
            f"WindowsApps, StartApps, or PATH. Pass an explicit existing .exe path or installed app name."
        )
    blocked_hit = _is_blocked_launch_target(app, cmd, extra_args)
    if blocked_hit is not None:
        return error_result(f"policy_blocked: '{blocked_hit}' is on the mandatory deny-list.")

    app_key = _norm_app_key(app)

    # Fast single-instance reuse (<8ms) if a visible non-cloaked window for this app is already open
    if reuse_existing and not extra_args:
        for h, _ in enum_visible_windows(require_title=True):
            inf = inspect_hwnd(h)
            if _window_matches_app_launch(inf, app_key, proc_hints):
                assert inf is not None
                if inf["isMinimized"]:
                    user32.ShowWindow(wintypes.HWND(h), SW_RESTORE)
                activate_hwnd(h)
                if auto_grant and inf["policyTier"] != "blocked":
                    cu_grant_app({"process_name": inf["processName"], "allow_browser_input": allow_browser_input})
                    inf = inspect_hwnd(h)
                data = {"launched": app, "reusedExisting": True, "pid": inf["pid"] if inf else 0, "window": inf}
                return tool_result(json.dumps(data, ensure_ascii=False), structured=data)

    before = {h for h, _ in enum_visible_windows(require_title=False)}
    # Always detach stdio to DEVNULL so spawned CLI/console executables can NEVER corrupt the MCP JSON-RPC stdout pipe
    spawn_flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB
    try:
        proc = subprocess.Popen(
            [*cmd, *extra_args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=spawn_flags,
        )
    except OSError:
        proc = subprocess.Popen(
            [*cmd, *extra_args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        )

    deadline = time.monotonic() + 5.0
    found_hwnd: int | None = None
    while time.monotonic() < deadline:
        time.sleep(0.08)
        for h, _ in enum_visible_windows(require_title=True):
            if h not in before:
                found_hwnd = h
                break
        if found_hwnd is not None:
            break
        for h, _ in enum_visible_windows(require_title=True):
            inf = inspect_hwnd(h)
            if _window_matches_app_launch(inf, app_key, proc_hints):
                found_hwnd = h
                break
        if found_hwnd is not None:
            break

    win_info = inspect_hwnd(found_hwnd) if found_hwnd is not None else None
    if auto_grant and win_info and win_info["policyTier"] != "blocked":
        cu_grant_app({"process_name": win_info["processName"], "allow_browser_input": allow_browser_input})
        win_info = inspect_hwnd(found_hwnd)

    data = {"launched": app, "reusedExisting": False, "pid": int(proc.pid), "window": win_info}
    return tool_result(json.dumps(data, ensure_ascii=False), structured=data)


def cu_screenshot(arguments: dict[str, Any]) -> dict[str, Any]:
    check_emergency_stop()
    hwnd = arguments.get("hwnd")
    max_width = max(320, min(int(arguments.get("max_width", 1366)), 3840))
    img_format = str(arguments.get("format", "png")).strip().lower()
    quality = max(40, min(int(arguments.get("quality", 82)), 95))
    annotate = bool(arguments.get("annotate", False))
    capture_method = "desktop_gdi_bitblt"
    info: dict[str, Any] = {}

    if hwnd is not None:
        hwnd = int(hwnd)
        info, err = require_window_permission(hwnd, need_input=False)
        if err:
            return error_result(err)
        if info["isMinimized"] and bool(arguments.get("restore_if_minimized", True)):
            activate_hwnd(hwnd)
        active_popup = _detect_active_popup(hwnd)
        img, origin = (None, {}) if (active_popup and current_desktop_name().lower() == "default") else capture_window_printwindow(hwnd)
        if img is not None:
            capture_method = "print_window_background"
        else:
            desk, vb = capture_desktop_gdi()
            b = get_window_bounds(hwnd)
            vis_left = max(vb["left"], b["left"])
            vis_top = max(vb["top"], b["top"])
            vis_right = min(vb["left"] + desk.width, b["left"] + b["width"])
            vis_bottom = min(vb["top"] + desk.height, b["top"] + b["height"])
            cx1, cy1 = vis_left - vb["left"], vis_top - vb["top"]
            cx2, cy2 = vis_right - vb["left"], vis_bottom - vb["top"]
            if cx2 <= cx1 + 4 or cy2 <= cy1 + 4:
                return error_result(f"Window {hwnd} has zero visible area ({b}).")
            img = desk.crop((cx1, cy1, cx2, cy2))
            origin = {"left": vis_left, "top": vis_top, "width": img.width, "height": img.height}
            capture_method = "popup_composited_capture" if active_popup else "screen_crop_fallback"
        bounds_record = get_window_bounds(hwnd)
    else:
        if not bool(arguments.get("allow_full_desktop", False)):
            return error_result("Prefer per-window capture by passing hwnd. To capture the entire desktop across all apps, pass allow_full_desktop=true explicitly.")
        img, vb = capture_desktop_gdi()
        origin = {"left": vb["left"], "top": vb["top"], "width": img.width, "height": img.height}
        bounds_record = vb

    orig_w, orig_h = img.width, img.height
    if img.width > max_width:
        new_h = max(1, round(img.height * max_width / img.width))
        img = img.resize((max_width, new_h), Image.Resampling.BILINEAR)

    scale_x = orig_w / img.width
    scale_y = orig_h / img.height

    hash_key = f"hwnd:{hwnd}" if hwnd is not None else "desktop"
    dhash_hex, dhash_int = compute_dhash(img)
    prev_hash = _LAST_FRAME_HASHES.get(hash_key)
    hamming = (prev_hash ^ dhash_int).bit_count() if prev_hash is not None else 64
    changed = prev_hash is None or hamming > 0
    _LAST_FRAME_HASHES[hash_key] = dhash_int

    elements: list[dict[str, Any]] = []
    focused_el: str | None = None
    popup_hwnd = _detect_active_popup(hwnd) if hwnd is not None else 0
    max_el_req = max(10, min(int(arguments.get("max_elements", 60)), 120))
    if annotate and hwnd is not None:
        uia_target = popup_hwnd if popup_hwnd else hwnd
        uia = run_uia_helper(uia_target, max_elements=max_el_req)
        raw_el = uia.get("elements") or []
        elements = [raw_el] if isinstance(raw_el, dict) else list(raw_el)
        focused_el = uia.get("focusedElement")
        img = annotate_som_elements(img, origin, scale_x, scale_y, elements)

    obs_payload: dict[str, Any] = {
        "kind": "observe" if annotate else "screenshot",
        "hwnd": hwnd,
        "activePopupHwnd": popup_hwnd or None,
        "maxElements": max_el_req,
        "captureMethod": capture_method,
        "originX": origin["left"],
        "originY": origin["top"],
        "originalWidth": orig_w,
        "originalHeight": orig_h,
        "returnedWidth": img.width,
        "returnedHeight": img.height,
        "scaleX": scale_x,
        "scaleY": scale_y,
        "bounds": bounds_record,
        "frameHash": dhash_hex,
        "changedFromPrevious": changed,
        "hammingDistance": hamming,
    }
    if annotate:
        obs_payload["elements"] = elements
        obs_payload["focusedElement"] = focused_el

    obs_id = record_observation(obs_payload)
    b64, saved_path, mime, byte_len = encode_image(img, obs_id, fmt=img_format, quality=quality)
    meta = (_get_observation(obs_id) or obs_payload) | {"savedPath": saved_path, "mimeType": mime, "bytes": byte_len}
    with _STATE_LOCK:
        has_grants = bool(STATE.get("grants"))
    if has_grants:
        update_overlay_hud(True, "Active", ttl_sec=_HUD_IDLE_TIMEOUT_SEC)

    popup_note = f", activePopupHwnd={popup_hwnd}" if popup_hwnd else ""
    summary_txt = f"Captured {capture_method} ({img.width}x{img.height}, {mime}, {byte_len // 1024}KB, changed={changed}{popup_note}, observation_id={obs_id})."
    if annotate and elements:
        lines = [summary_txt, f"Focused: {focused_el} | {len(elements)} numbered UI elements:"]
        for el in elements:
            b = el["bounds"]
            val_part = f" value={el['value']!r}" if el.get("value") not in (None, "") else ""
            lines.append(f"[{el['index']}] {el['controlType']} {el.get('name')!r}{val_part} @({b['cx']},{b['cy']})")
        summary_txt = "\n".join(lines)

    return tool_result(
        summary_txt,
        structured=meta,
        image_b64=b64,
        image_mime=mime,
    )


def cu_observe(arguments: dict[str, Any]) -> dict[str, Any]:
    """Single-turn hybrid observation: fast PrintWindow screenshot + Set-of-Mark [0..N] badges + cached UIA/Win32 tree."""
    merged = dict(arguments)
    merged.setdefault("annotate", True)
    merged.setdefault("format", "webp")
    merged.setdefault("max_width", 1280)
    merged.setdefault("max_elements", 100)
    return cu_screenshot(merged)


def cu_zoom(arguments: dict[str, Any]) -> dict[str, Any]:
    check_emergency_stop()
    try:
        sx, sy, hwnd = resolve_screen_coords(arguments, "x", "y")
    except ValueError as exc:
        return error_result(str(exc))
    if hwnd is None and not bool(arguments.get("allow_full_desktop", False)):
        return error_result(
            "allow_full_desktop_required: Pass hwnd (or a window-bound observation_id) for cu_zoom, "
            "or set allow_full_desktop=true explicitly."
        )
    obs_id = arguments.get("observation_id")
    obs = _get_observation(str(obs_id)) if obs_id else None
    scale_x = obs["scaleX"] if obs else 1.0
    scale_y = obs["scaleY"] if obs else 1.0
    w = max(32, min(int(round(float(arguments.get("width", 320)) * scale_x)), 1600))
    h = max(32, min(int(round(float(arguments.get("height", 240)) * scale_y)), 1200))
    upscale = max(1, min(int(arguments.get("upscale", 2)), 3))

    if hwnd is not None:
        _, err = require_window_permission(int(hwnd), need_input=False)
        if err:
            return error_result(err)
        win_img, origin = capture_window_printwindow(int(hwnd))
        if win_img is not None:
            rx1 = max(0, sx - origin["left"])
            ry1 = max(0, sy - origin["top"])
            rx2 = min(win_img.width, rx1 + w)
            ry2 = min(win_img.height, ry1 + h)
            if rx2 <= rx1 or ry2 <= ry1:
                return error_result(f"Zoom region ({sx},{sy} {w}x{h}) is outside visible bounds of HWND {hwnd}.")
            crop = win_img.crop((rx1, ry1, rx2, ry2))
            actual_left = origin["left"] + rx1
            actual_top = origin["top"] + ry1
        else:
            desk, vb = capture_desktop_gdi()
            wb = get_window_bounds(int(hwnd))
            vis_left = max(vb["left"], wb["left"])
            vis_top = max(vb["top"], wb["top"])
            vis_right = min(vb["left"] + desk.width, wb["left"] + wb["width"])
            vis_bottom = min(vb["top"] + desk.height, wb["top"] + wb["height"])
            rx1 = max(vis_left, sx) - vb["left"]
            ry1 = max(vis_top, sy) - vb["top"]
            rx2 = min(vis_right, sx + w) - vb["left"]
            ry2 = min(vis_bottom, sy + h) - vb["top"]
            if rx2 <= rx1 or ry2 <= ry1:
                return error_result(f"Zoom region ({sx},{sy} {w}x{h}) is outside visible bounds of HWND {hwnd} ({wb}).")
            crop = desk.crop((rx1, ry1, rx2, ry2))
            actual_left = vb["left"] + rx1
            actual_top = vb["top"] + ry1
    else:
        desk, vb = capture_desktop_gdi()
        rx1 = max(0, sx - vb["left"])
        ry1 = max(0, sy - vb["top"])
        rx2 = min(desk.width, rx1 + w)
        ry2 = min(desk.height, ry1 + h)
        if rx2 <= rx1 or ry2 <= ry1:
            return error_result(f"Zoom region ({sx},{sy} {w}x{h}) is outside desktop bounds.")
        crop = desk.crop((rx1, ry1, rx2, ry2))
        actual_left = vb["left"] + rx1
        actual_top = vb["top"] + ry1

    orig_cw, orig_ch = crop.width, crop.height
    if upscale > 1:
        crop = crop.resize((orig_cw * upscale, orig_ch * upscale), Image.Resampling.NEAREST)

    zoom_obs_payload = {
        "kind": "zoom",
        "hwnd": int(hwnd) if hwnd is not None else (obs.get("hwnd") if obs else arguments.get("hwnd")),
        "originX": actual_left,
        "originY": actual_top,
        "originalWidth": orig_cw,
        "originalHeight": orig_ch,
        "returnedWidth": crop.width,
        "returnedHeight": crop.height,
        "scaleX": 1.0 / upscale,
        "scaleY": 1.0 / upscale,
        "bounds": get_window_bounds(int(hwnd)) if hwnd is not None else (get_window_bounds(int(obs["hwnd"])) if (obs and obs.get("hwnd")) else virtual_bounds()),
    }
    zoom_obs_id = record_observation(zoom_obs_payload)
    b64, saved_path = encode_png(crop, f"{zoom_obs_id}.png")
    meta = (_get_observation(zoom_obs_id) or zoom_obs_payload) | {"savedPath": saved_path, "upscale": upscale}
    return tool_result(
        f"Zoomed region ({orig_cw}x{orig_ch} -> {crop.width}x{crop.height}, observation_id={zoom_obs_id}).",
        structured=meta,
        image_b64=b64,
    )


def cu_ui_tree(arguments: dict[str, Any]) -> dict[str, Any]:
    check_emergency_stop()
    hwnd = int(arguments["hwnd"])
    info, err = require_window_permission(hwnd, need_input=False)
    if err:
        return error_result(err)
    max_elements = max(10, min(int(arguments.get("max_elements", 80)), 250))
    uia = run_uia_helper(hwnd, max_elements=max_elements)
    elements = uia.get("elements") or []
    if isinstance(elements, dict):
        elements = [elements]

    lines = [f"Window HWND={hwnd} ({info['processName']}) \"{info['title']}\" | Focused: {uia.get('focusedElement')}"]
    for el in elements:
        b = el["bounds"]
        val_part = f" value={el['value']!r}" if el.get("value") not in (None, "") else ""
        aid_part = f" id={el['automationId']!r}" if el.get("automationId") else ""
        lines.append(
            f"[{el['index']}] {el['controlType']} {el.get('name')!r}{aid_part}{val_part} @({b['cx']},{b['cy']}) [{b['width']}x{b['height']}]"
        )

    obs_id = record_observation(
        {
            "kind": "ui_tree",
            "hwnd": hwnd,
            "originX": 0,
            "originY": 0,
            "scaleX": 1.0,
            "scaleY": 1.0,
            "bounds": get_window_bounds(hwnd),
            "maxElements": max_elements,
            "elements": elements,
            "focusedElement": uia.get("focusedElement"),
        }
    )
    data = {
        "observationId": obs_id,
        "hwnd": hwnd,
        "focusedElement": uia.get("focusedElement"),
        "count": len(elements),
        "elements": elements,
        "formattedTree": "\n".join(lines),
    }
    update_overlay_hud(True, "Active", ttl_sec=_HUD_IDLE_TIMEOUT_SEC)
    return tool_result(data["formattedTree"], structured=data)


def cu_focus_window(arguments: dict[str, Any]) -> dict[str, Any]:
    check_emergency_stop()
    hwnd = int(arguments["hwnd"])
    info, err = require_window_permission(hwnd, need_input=True)
    if err:
        return error_result(err)
    ok = activate_hwnd(hwnd)
    actual = get_foreground_hwnd()
    if not ok:
        return error_result(f"focus_failed: Requested HWND {hwnd}, actual foreground HWND is {actual}.")
    update_overlay_hud(True, "Active", ttl_sec=_HUD_IDLE_TIMEOUT_SEC)
    return tool_result(f"Focused HWND {hwnd} ({info['processName']}: {info['title']}).", structured={"hwnd": hwnd, "foregroundHwnd": actual})


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
        user32.SendMessageTimeoutW(
            wintypes.HWND(root_hwnd), WM_ACTIVATE, WA_ACTIVE, None, SMTO_ABORTIFHUNG, 120, ctypes.byref(res_len)
        )
        user32.GetKeyboardState(ks)
        orig_lbtn = ks[VK_LBUTTON]
        try:
            ks[VK_LBUTTON] = 0x00
            user32.SetKeyboardState(ks)
            user32.SendMessageTimeoutW(
                wintypes.HWND(child_hwnd), WM_MOUSEMOVE, 0, ctypes.c_void_p(lparam), SMTO_ABORTIFHUNG, 120, ctypes.byref(res_len)
            )
            ks[VK_LBUTTON] = 0x80
            user32.SetKeyboardState(ks)
            user32.SendMessageTimeoutW(
                wintypes.HWND(child_hwnd), WM_LBUTTONDOWN, MK_LBUTTON, ctypes.c_void_p(lparam), SMTO_ABORTIFHUNG, 120, ctypes.byref(res_len)
            )
            ks[VK_LBUTTON] = 0x00
            user32.SetKeyboardState(ks)
            user32.SendMessageTimeoutW(
                wintypes.HWND(child_hwnd), WM_LBUTTONUP, 0, ctypes.c_void_p(lparam), SMTO_ABORTIFHUNG, 120, ctypes.byref(res_len)
            )
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
    user32.SendMessageTimeoutW(
        wintypes.HWND(child_hwnd), WM_SETTEXT, 0, val_buf, SMTO_ABORTIFHUNG, 150, ctypes.byref(res_len)
    )
    if cls_low in ("edit", "richedit20w", "richedit50w"):
        return

    # For Tkinter (TkChild) and custom owner-drawn controls: focus via background click, clear, and stream WM_IME_CHAR
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
    hit = int(user32.WindowFromPoint(POINT(cx, cy)) or 0)
    return hit


def _is_tk_toplevel(hwnd: int) -> bool:
    cls_buf = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(wintypes.HWND(hwnd), cls_buf, 64)
    return (cls_buf.value or "").lower().startswith("tk")


def cu_click_element(arguments: dict[str, Any]) -> dict[str, Any]:
    check_emergency_stop()
    obs_id = str(arguments["observation_id"])
    elem_idx = int(arguments["element_index"])
    use_uia_invoke = bool(arguments.get("prefer_uia_invoke", False))
    sync_state()
    obs = _get_observation(obs_id)
    if not obs or obs.get("kind") not in ("ui_tree", "observe"):
        return error_result(f"stale_observation: '{obs_id}' is not a valid ui_tree/observe observation. Call cu_ui_tree or cu_observe first.")
    hwnd = int(obs["hwnd"])
    _, err = require_window_permission(hwnd, need_input=True)
    if err:
        return error_result(err)

    elements = obs.get("elements") or []
    match = next((e for e in elements if int(e["index"]) == elem_idx), None)
    if match is None:
        return error_result(f"element_index {elem_idx} not found in {obs_id} (count={len(elements)}).")

    cx = int(match["bounds"]["cx"])
    cy = int(match["bounds"]["cy"])
    aid = str(match.get("automationId") or "")
    is_tk = _is_tk_toplevel(hwnd)

    obs_max_el = max(int(obs.get("maxElements") or 100), elem_idx + 10)
    uia_target_hwnd = int(obs.get("activePopupHwnd") or hwnd)
    if use_uia_invoke or get_foreground_hwnd() == 0 or aid.startswith("hwnd:") or is_tk:
        if not aid.startswith("hwnd:") and not is_tk:
            res = run_uia_helper(uia_target_hwnd, max_elements=obs_max_el, invoke_index=elem_idx)
            if res.get("method") in ("InvokePattern", "SelectionItemPattern", "TogglePattern"):
                update_overlay_hud(True, "Active", click_xy=(cx, cy), ttl_sec=_HUD_IDLE_TIMEOUT_SEC)
                return tool_result(
                    f"Invoked [{elem_idx}] {match['controlType']} {match.get('name')!r} via UIA {res.get('method')}.",
                    structured=res,
                )
        child_hwnd = _resolve_element_child_hwnd(match, cx, cy)
        if child_hwnd > 0:
            send_child_hwnd_click(child_hwnd, cx, cy)
            update_overlay_hud(True, "Active", click_xy=(cx, cy), ttl_sec=_HUD_IDLE_TIMEOUT_SEC)
            return tool_result(
                f"Dispatched background window-message click to [{elem_idx}] {match['controlType']} (hwnd:{child_hwnd}).",
                structured={"action": "background_msg_click", "index": elem_idx, "childHwnd": child_hwnd},
            )

    return cu_click(
        {
            "hwnd": hwnd,
            "x": cx,
            "y": cy,
            "coordinate_space": "screen",
            "button": arguments.get("button", "left"),
            "clicks": arguments.get("clicks", 1),
        }
    )


def cu_set_value(arguments: dict[str, Any]) -> dict[str, Any]:
    check_emergency_stop()
    obs_id = str(arguments["observation_id"])
    elem_idx = int(arguments["element_index"])
    value = str(arguments["value"])
    sync_state()
    obs = _get_observation(obs_id)
    if not obs or obs.get("kind") not in ("ui_tree", "observe"):
        return error_result(f"stale_observation: '{obs_id}' is not a valid ui_tree/observe observation.")
    hwnd = int(obs["hwnd"])
    _, err = require_window_permission(hwnd, need_input=True)
    if err:
        return error_result(err)

    elements = obs.get("elements") or []
    match = next((e for e in elements if int(e["index"]) == elem_idx), None)
    if match is None:
        return error_result(f"element_index {elem_idx} not found in {obs_id}.")

    cx, cy = int(match["bounds"]["cx"]), int(match["bounds"]["cy"])
    aid = str(match.get("automationId") or "")
    is_tk = _is_tk_toplevel(hwnd)
    obs_max_el = max(int(obs.get("maxElements") or 100), elem_idx + 10)
    uia_target_hwnd = int(obs.get("activePopupHwnd") or hwnd)
    if not aid.startswith("hwnd:") and not is_tk:
        res = run_uia_helper(uia_target_hwnd, max_elements=obs_max_el, set_value_index=elem_idx, value_text=value)
        if res.get("method") == "ValuePattern":
            update_overlay_hud(True, "Active", click_xy=(cx, cy), ttl_sec=_HUD_IDLE_TIMEOUT_SEC)
            return tool_result(f"Set value on element [{elem_idx}] via UIA ValuePattern ({len(value)} chars).", structured=res)

    child_hwnd = _resolve_element_child_hwnd(match, cx, cy)
    off_default = current_desktop_name().lower() != "default" or get_foreground_hwnd() == 0
    pref_bg = arguments.get("prefer_background_msg")
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
            or (bool(pref_bg) if pref_bg is not None else cls_low in ("edit", "richedit20w", "richedit50w"))
        )
    )
    if allow_bg_msg:
        send_child_hwnd_text(child_hwnd, value, cx, cy)
        update_overlay_hud(True, "Active", click_xy=(cx, cy), ttl_sec=_HUD_IDLE_TIMEOUT_SEC)
        return tool_result(
            f"Dispatched background text ({len(value)} chars) to [{elem_idx}] (hwnd:{child_hwnd}).",
            structured={"action": "background_msg_text", "index": elem_idx, "childHwnd": child_hwnd},
        )

    # Reliable foreground fallback for Electron/Chromium/WinUI3/Qt/Java inputs: click -> Ctrl+A -> Unicode SendInput
    click_res = cu_click({"hwnd": hwnd, "x": cx, "y": cy, "coordinate_space": "screen"})
    if click_res.get("isError"):
        return click_res
    time.sleep(0.08)
    send_key_chord("ctrl", "a", target_hwnd=hwnd)
    time.sleep(0.04)
    return cu_type({"hwnd": hwnd, "text": value})


def resolve_drag_points(arguments: dict[str, Any]) -> tuple[list[tuple[int, int]], int | None]:
    obs_id = arguments.get("observation_id")
    coord_space = str(arguments.get("coordinate_space", "observation" if obs_id else "screen"))
    obs: dict[str, Any] | None = None
    hwnd: int | None = int(arguments["hwnd"]) if arguments.get("hwnd") is not None else None
    win_bounds: dict[str, int] | None = None

    if obs_id:
        sync_state()
        obs = _get_observation(str(obs_id))
        if not obs:
            raise ValueError(f"stale_observation: observation_id '{obs_id}' not found. Capture a fresh screenshot or UI tree.")
        if obs.get("hwnd") is not None:
            hwnd = int(obs["hwnd"])
            cur = get_window_bounds(hwnd)
            orig = obs.get("bounds") or {}
            if (
                abs(cur["left"] - orig.get("left", cur["left"])) > 4
                or abs(cur["top"] - orig.get("top", cur["top"])) > 4
                or abs(cur["width"] - orig.get("width", cur["width"])) > 4
            ):
                raise ValueError(f"window_moved: Target window moved/resized since '{obs_id}'. Capture a fresh screenshot.")
            win_bounds = cur

    raw_pts = arguments.get("points")
    pairs: list[tuple[float, float]] = []
    if raw_pts is not None:
        if not isinstance(raw_pts, list) or not (2 <= len(raw_pts) <= 256):
            raise ValueError("points must be an array of 2..256 [x, y] coordinate pairs.")
        for idx, item in enumerate(raw_pts):
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                pairs.append((float(item[0]), float(item[1])))
            elif isinstance(item, dict) and "x" in item and "y" in item:
                pairs.append((float(item["x"]), float(item["y"])))
            else:
                raise ValueError(f"points[{idx}] must be [x, y] or {{'x': x, 'y': y}}.")
    else:
        for req_k in ("from_x", "from_y", "to_x", "to_y"):
            if req_k not in arguments or arguments[req_k] is None:
                raise ValueError("Provide either 'points' ([[x,y], ...]) or 'from_x', 'from_y', 'to_x', 'to_y'.")
        pairs = [
            (float(arguments["from_x"]), float(arguments["from_y"])),
            (float(arguments["to_x"]), float(arguments["to_y"])),
        ]

    if coord_space == "window" and hwnd is not None and win_bounds is None:
        win_bounds = get_window_bounds(hwnd)

    screen_pts: list[tuple[int, int]] = []
    for rx, ry in pairs:
        if obs is not None and coord_space == "observation":
            sx = int(round(obs["originX"] + rx * obs["scaleX"]))
            sy = int(round(obs["originY"] + ry * obs["scaleY"]))
        elif coord_space == "window" and win_bounds is not None:
            sx = int(round(win_bounds["left"] + rx))
            sy = int(round(win_bounds["top"] + ry))
        else:
            sx, sy = int(round(rx)), int(round(ry))
        screen_pts.append((sx, sy))

    if bool(arguments.get("closed", False)) and screen_pts[-1] != screen_pts[0]:
        screen_pts.append(screen_pts[0])
    return screen_pts, hwnd


def _catmull_rom_spline(
    pts: list[tuple[float, float]], *, closed: bool = False, subdivisions: int = 8
) -> list[tuple[float, float]]:
    n = len(pts)
    if n < 3:
        return list(pts)
    sub = max(2, subdivisions)
    if closed:
        ring = pts[:-1] if (n > 3 and math.hypot(pts[-1][0] - pts[0][0], pts[-1][1] - pts[0][1]) < 1e-3) else pts
        if len(ring) < 3:
            return list(pts)
        ext = [ring[-1], *ring, ring[0], ring[1]]
        seg_count = len(ring)
    else:
        ext = [pts[0], *pts, pts[-1]]
        seg_count = n - 1

    out: list[tuple[float, float]] = [ext[1]]
    for i in range(1, 1 + seg_count):
        p0, p1, p2, p3 = ext[i - 1], ext[i], ext[i + 1], ext[i + 2]
        for j in range(1, sub + 1):
            t = j / sub
            t2 = t * t
            t3 = t2 * t
            x = 0.5 * (
                (2.0 * p1[0])
                + (-p0[0] + p2[0]) * t
                + (2.0 * p0[0] - 5.0 * p1[0] + 4.0 * p2[0] - p3[0]) * t2
                + (-p0[0] + 3.0 * p1[0] - 3.0 * p2[0] + p3[0]) * t3
            )
            y = 0.5 * (
                (2.0 * p1[1])
                + (-p0[1] + p2[1]) * t
                + (2.0 * p0[1] - 5.0 * p1[1] + 4.0 * p2[1] - p3[1]) * t2
                + (-p0[1] + 3.0 * p1[1] - 3.0 * p2[1] + p3[1]) * t3
            )
            out.append((x, y))
    return out


def _build_drag_samples(
    screen_pts: list[tuple[int, int]], *, duration: float, smooth: bool = False, closed: bool = False
) -> tuple[list[tuple[int, int]], float]:
    pts_f = [(float(x), float(y)) for x, y in screen_pts]
    seg_lens = [math.hypot(pts_f[i + 1][0] - pts_f[i][0], pts_f[i + 1][1] - pts_f[i][1]) for i in range(len(pts_f) - 1)]
    total_px = sum(seg_lens)
    target_steps = max(4, int(round(duration * 120.0)))

    if len(pts_f) == 2 and not smooth:
        sx, sy = pts_f[0]
        dx, dy = pts_f[1][0] - sx, pts_f[1][1] - sy
        samples: list[tuple[int, int]] = []
        for i in range(1, target_steps + 1):
            t = i / target_steps
            u = t ** 0.82
            s = u * u * u * (10.0 - 15.0 * u + 6.0 * u * u)
            samples.append((int(round(sx + dx * s)), int(round(sy + dy * s))))
        return samples, total_px

    if smooth and len(pts_f) >= 3:
        dense = _catmull_rom_spline(pts_f, closed=closed, subdivisions=8)
        cum = [0.0]
        for i in range(1, len(dense)):
            cum.append(cum[-1] + math.hypot(dense[i][0] - dense[i - 1][0], dense[i][1] - dense[i - 1][1]))
        arc_total = cum[-1]
        if arc_total < 1.0:
            return [(int(round(pts_f[-1][0])), int(round(pts_f[-1][1])))], arc_total
        n_steps = max(target_steps, len(pts_f) * 4)
        samples = []
        seg = 0
        for k in range(1, n_steps + 1):
            d = (k / n_steps) * arc_total
            while seg + 1 < len(cum) - 1 and cum[seg + 1] < d:
                seg += 1
            span = cum[seg + 1] - cum[seg]
            u = (d - cum[seg]) / span if span > 1e-6 else 1.0
            samples.append((
                int(round(dense[seg][0] + (dense[seg + 1][0] - dense[seg][0]) * u)),
                int(round(dense[seg][1] + (dense[seg + 1][1] - dense[seg][1]) * u)),
            ))
        samples[-1] = screen_pts[-1]
        return samples, arc_total

    if total_px < 1.0:
        return [screen_pts[-1]], total_px
    samples = []
    for i, slen in enumerate(seg_lens):
        p0, p1 = pts_f[i], pts_f[i + 1]
        k_seg = max(1, int(round(target_steps * (slen / total_px))))
        for j in range(1, k_seg + 1):
            u = j / k_seg
            samples.append((int(round(p0[0] + (p1[0] - p0[0]) * u)), int(round(p0[1] + (p1[1] - p0[1]) * u))))
    return samples, total_px


def cu_click(arguments: dict[str, Any]) -> dict[str, Any]:
    check_emergency_stop()
    try:
        sx, sy, hwnd = resolve_screen_coords(arguments, "x", "y")
    except ValueError as exc:
        return error_result(str(exc))
    if hwnd is None:
        return error_result("Target hwnd or window-bound observation_id is required for clicks.")
    info, err = require_window_permission(int(hwnd), need_input=True)
    if err:
        return error_result(err)

    # Preserve active context menus / dropdown popups owned by the same PID instead of dismissing them with SetForegroundWindow(parent)
    pre_belongs, pre_root = verify_point_belongs_to_hwnd(sx, sy, int(hwnd))
    hitting_owned_popup = pre_belongs and pre_root != int(hwnd)
    if not hitting_owned_popup:
        if not activate_hwnd(int(hwnd)):
            return error_result(f"focus_collision: Could not bring target HWND {hwnd} ({info['processName']}) to foreground.")

    belongs, hit_root = verify_point_belongs_to_hwnd(sx, sy, int(hwnd))
    if not belongs:
        return error_result(f"point_occluded: Screen point ({sx}, {sy}) hits HWND {hit_root}, not target HWND {hwnd}. Refresh window state.")
    if hit_root != int(hwnd):
        hit_info = inspect_hwnd(hit_root)
        if hit_info is not None:
            if hit_info.get("policyTier") == "blocked":
                return error_result(f"policy_blocked: Point ({sx}, {sy}) hits blocked window '{hit_info['processName']}'.")
            sens_err = check_sensitive_editor_title(hit_info["processName"], hit_info["title"])
            if sens_err:
                return error_result(sens_err)

    global _CURSOR_ENGAGED
    button = str(arguments.get("button", "left"))
    clicks = max(1, min(int(arguments.get("clicks", 1)), 3))
    raw_dur = arguments.get("duration")
    dur = max(0.0, min(float(raw_dur), 1.0)) if raw_dur is not None else None

    smooth_move_to(sx, sy, duration=dur)
    if not hitting_owned_popup:
        hit_w = int(user32.WindowFromPoint(POINT(sx, sy)) or 0)
        if hit_w and hit_w != int(hwnd) and not _has_child_windows(hit_w):
            c_buf = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(wintypes.HWND(hit_w), c_buf, 64)
            if (c_buf.value or "").lower() not in ("button", "static", "#32768", "combolbox"):
                _ensure_child_canvas_focus(sx, sy, int(hwnd))
    send_mouse_click(button=button, clicks=clicks, interval=0.04 if clicks > 1 else 0.0)
    update_overlay_hud(True, "Active", click_xy=(sx, sy), ttl_sec=_HUD_IDLE_TIMEOUT_SEC)
    _CURSOR_ENGAGED = True
    data = {"hwnd": int(hwnd), "screenX": sx, "screenY": sy, "button": button, "clicks": clicks}
    return tool_result(f"Clicked {button} ({clicks}x) at screen ({sx}, {sy}) in {info['processName']}.", structured=data)


def cu_move(arguments: dict[str, Any]) -> dict[str, Any]:
    check_emergency_stop()
    try:
        sx, sy, hwnd = resolve_screen_coords(arguments, "x", "y")
    except ValueError as exc:
        return error_result(str(exc))
    if hwnd is not None:
        _, err = require_window_permission(int(hwnd), need_input=False)
        if err:
            return error_result(err)
    global _CURSOR_ENGAGED
    duration = max(0.0, min(float(arguments.get("duration", 0.075)), 2.0))
    if STATE.get("grants"):
        update_overlay_hud(True, "Active", cursor_xy=(sx, sy), ttl_sec=_HUD_IDLE_TIMEOUT_SEC)
    smooth_move_to(sx, sy, duration=duration)
    _CURSOR_ENGAGED = True
    return tool_result(f"Moved pointer to screen ({sx}, {sy}).", structured={"screenX": sx, "screenY": sy})


def cu_drag(arguments: dict[str, Any]) -> dict[str, Any]:
    check_emergency_stop()
    try:
        screen_pts, hwnd = resolve_drag_points(arguments)
    except ValueError as exc:
        return error_result(str(exc))
    if hwnd is None:
        return error_result("Target hwnd or window-bound observation_id is required for drag.")
    info, err = require_window_permission(int(hwnd), need_input=True)
    if err:
        return error_result(err)
    if not activate_hwnd(int(hwnd)):
        return error_result(f"focus_collision: Could not focus HWND {hwnd}.")

    stride = max(1, (len(screen_pts) - 1) // 5)
    check_indices = sorted({0, len(screen_pts) - 1, *range(0, len(screen_pts), stride)})
    for ci in check_indices:
        px, py = screen_pts[ci]
        belongs, hit_root = verify_point_belongs_to_hwnd(px, py, int(hwnd))
        if not belongs:
            return error_result(
                f"point_occluded: Drag waypoint[{ci}] ({px}, {py}) hits HWND {hit_root}, not target HWND {hwnd}."
            )

    global _CURSOR_ENGAGED
    closed = bool(arguments.get("closed", False))
    smooth = bool(arguments.get("smooth", False))
    hold_ms = max(0, min(int(arguments.get("hold_ms", 0)), 1000))
    button = str(arguments.get("button", "left"))

    approx_px = sum(
        math.hypot(screen_pts[i + 1][0] - screen_pts[i][0], screen_pts[i + 1][1] - screen_pts[i][1])
        for i in range(len(screen_pts) - 1)
    )
    raw_dur = arguments.get("duration")
    if raw_dur is not None:
        duration = max(0.015, min(float(raw_dur), 5.0))
    elif len(screen_pts) == 2:
        duration = max(0.045, min(0.110, 0.035 + (min(approx_px, 1200.0) / 1200.0) * 0.075))
    else:
        duration = max(0.060, min(0.220, 0.040 + (min(approx_px, 2400.0) / 2400.0) * 0.140 + min(len(screen_pts), 40) * 0.001))

    samples, total_px = _build_drag_samples(screen_pts, duration=duration, smooth=smooth, closed=closed)
    sx0, sy0 = screen_pts[0]
    sx_end, sy_end = screen_pts[-1]

    cur_x, cur_y = get_cursor_pos()
    pre_dist = math.hypot(sx0 - cur_x, sy0 - cur_y)
    if pre_dist >= 2.0:
        pre_dur = min(0.026, max(0.010, 0.010 + (min(pre_dist, 1200.0) / 1200.0) * 0.016))
        smooth_move_to(sx0, sy0, duration=pre_dur)
    else:
        send_mouse_move_abs(sx0, sy0)

    check_emergency_stop(check_corner=False)
    _ensure_child_canvas_focus(sx0, sy0, int(hwnd))
    if _IS_LOW_INTEGRITY or current_desktop_name().lower() != "default":
        user32.SetCursorPos(sx_end, sy_end)
    else:
        _enter_1ms_timer()
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
                        _revoke_all_on_emergency("Idle")
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
            _exit_1ms_timer()

    update_overlay_hud(True, "Active", click_xy=(sx_end, sy_end), ttl_sec=_HUD_IDLE_TIMEOUT_SEC)
    _CURSOR_ENGAGED = True
    data = {
        "hwnd": int(hwnd),
        "from": [sx0, sy0],
        "to": [sx_end, sy_end],
        "vertexCount": len(screen_pts),
        "sampleCount": len(samples),
        "closed": closed,
        "smooth": smooth,
        "pathLengthPx": round(total_px, 1),
    }
    if len(screen_pts) <= 2 and not closed:
        msg = f"Dragged ({sx0},{sy0}) -> ({sx_end},{sy_end}) in {info['processName']}."
    else:
        msg = (
            f"Stroked {len(screen_pts)}-point {'smooth ' if smooth else ''}{'closed ' if closed else ''}path "
            f"({sx0},{sy0}) -> ({sx_end},{sy_end}) ({round(total_px)}px) in {info['processName']}."
        )
    return tool_result(msg, structured=data)


def cu_scroll(arguments: dict[str, Any]) -> dict[str, Any]:
    check_emergency_stop()
    try:
        sx, sy, hwnd = resolve_screen_coords(arguments, "x", "y")
    except ValueError as exc:
        return error_result(str(exc))
    if hwnd is None:
        return error_result("Target hwnd or window-bound observation_id is required for scroll.")
    _, err = require_window_permission(int(hwnd), need_input=True)
    if err:
        return error_result(err)
    if not activate_hwnd(int(hwnd)):
        return error_result(f"focus_collision: Could not focus HWND {hwnd}.")
    belongs, hit_root = verify_point_belongs_to_hwnd(sx, sy, int(hwnd))
    if not belongs:
        return error_result(f"point_occluded: Scroll point ({sx}, {sy}) hits HWND {hit_root}, not target HWND {hwnd}.")
    global _CURSOR_ENGAGED
    clicks = max(-50, min(int(arguments.get("clicks", -3)), 50))
    horizontal = bool(arguments.get("horizontal", False))
    smooth_move_to(sx, sy, duration=0.04)
    send_mouse_wheel(clicks, horizontal=horizontal)
    update_overlay_hud(True, "Active", click_xy=(sx, sy), ttl_sec=_HUD_IDLE_TIMEOUT_SEC)
    _CURSOR_ENGAGED = True
    return tool_result(
        f"Scrolled {clicks} {'horizontal ' if horizontal else ''}wheel steps at ({sx}, {sy}).",
        structured={"hwnd": int(hwnd), "screenX": sx, "screenY": sy, "clicks": clicks, "horizontal": horizontal},
    )


def cu_type(arguments: dict[str, Any]) -> dict[str, Any]:
    check_emergency_stop()
    hwnd = int(arguments["hwnd"])
    text = str(arguments["text"])
    if len(text) > 4000:
        return error_result("text_too_long: Max 4000 characters per cu_type call.")
    info, err = require_window_permission(hwnd, need_input=True)
    if err:
        return error_result(err)
    if bool(arguments.get("activate_first", False)):
        activate_hwnd(hwnd)
    fg = get_foreground_hwnd()
    if fg != hwnd:
        return error_result(f"focus_collision: Expected HWND {hwnd} in foreground before typing, but foreground is HWND {fg}. Focus or click target field first.")

    update_overlay_hud(True, "Active", ttl_sec=_HUD_IDLE_TIMEOUT_SEC)
    if bool(arguments.get("clear_first", False)):
        if get_foreground_hwnd() != hwnd:
            return error_result(f"focus_collision: HWND {hwnd} lost focus before clear_first.")
        send_key_chord("ctrl", "a", target_hwnd=hwnd)
        time.sleep(0.02)
        send_key_chord("backspace", target_hwnd=hwnd)

    interval = max(0.0, min(float(arguments.get("interval", 0.004)), 0.25))
    typed = 0
    if interval <= 0.008:
        # Fast batched SendInput path (~40x faster): dispatch 20-character chunks while checking focus & Esc before each chunk
        chunk_size = 20
        for start in range(0, len(text), chunk_size):
            check_emergency_stop()
            if get_foreground_hwnd() != hwnd:
                return error_result(f"focus_collision: Focus moved away from HWND {hwnd} after {typed} characters; halted immediately.")
            part = text[start : start + chunk_size]
            send_unicode_chunk(part)
            typed += len(part)
            if interval > 0:
                time.sleep(interval)
    else:
        for ch in text:
            check_emergency_stop()
            if get_foreground_hwnd() != hwnd:
                return error_result(f"focus_collision: Focus moved away from HWND {hwnd} after {typed} characters; halted immediately.")
            send_unicode_char(ch)
            typed += 1
            time.sleep(interval)

    if bool(arguments.get("press_enter", False)):
        check_emergency_stop()
        if get_foreground_hwnd() != hwnd:
            return error_result(f"focus_collision: HWND {hwnd} lost focus before press_enter.")
        send_key_chord("enter", target_hwnd=hwnd)

    update_overlay_hud(True, "Active", ttl_sec=_HUD_IDLE_TIMEOUT_SEC)
    return tool_result(
        f"Typed {typed} Unicode character(s) into {info['processName']} (HWND {hwnd}).",
        structured={"hwnd": hwnd, "characters": typed, "interval": interval},
    )


def cu_key(arguments: dict[str, Any]) -> dict[str, Any]:
    check_emergency_stop()
    hwnd = int(arguments["hwnd"])
    keys = arguments["keys"]
    if isinstance(keys, str):
        s_k = keys.strip()
        if s_k == "+":
            keys = ["+"]
        elif s_k.endswith("++"):
            keys = [*(p.strip() for p in s_k[:-2].split("+") if p.strip()), "+"]
        else:
            keys = [k.strip() for k in s_k.split("+") if k.strip()]
    if not isinstance(keys, list) or not keys:
        return error_result("keys must be a non-empty list or '+'-separated chord string.")
    _KEY_ALIAS_CANON = {
        "control": "ctrl", "ctrlleft": "ctrl", "ctrlright": "ctrl",
        "shiftleft": "shift", "shiftright": "shift",
        "escape": "esc",
    }
    for k in keys:
        if k.lower() in BLOCKED_KEYS:
            return error_result(f"policy_blocked: Windows/Super key chord '{k}' is forbidden.")
    canon_set = {_KEY_ALIAS_CANON.get(k.lower(), k.lower()) for k in keys}
    if canon_set in ({"ctrl", "esc"}, {"ctrl", "shift", "esc"}):
        return error_result(f"policy_blocked: System chord '{'+'.join(k.lower() for k in keys)}' is forbidden (Start menu / Task Manager).")
    info, err = require_window_permission(hwnd, need_input=True)
    if err:
        return error_result(err)
    if bool(arguments.get("activate_first", True)):
        activate_hwnd(hwnd)
    fg = get_foreground_hwnd()
    if fg != hwnd and not (fg == 0 and current_desktop_name().lower() != "default"):
        return error_result(f"focus_collision: Expected HWND {hwnd} in foreground before keys, but foreground is HWND {fg}.")
    if check_sensitive_editor_title(info["processName"], info["title"]):
        return error_result("sensitive_file_guard: Refused key chord in protected file editor.")
    norm = [k.lower() for k in keys]
    update_overlay_hud(True, "Active", ttl_sec=_HUD_IDLE_TIMEOUT_SEC)
    if not (_IS_LOW_INTEGRITY or (fg == 0 and current_desktop_name().lower() != "default")):
        send_key_chord(*norm, target_hwnd=hwnd)
    return tool_result(f"Pressed {'+'.join(norm)} in {info['processName']}.", structured={"hwnd": hwnd, "keys": norm})


def cu_overlay(arguments: dict[str, Any]) -> dict[str, Any]:
    active = bool(arguments.get("active", True))
    label = str(arguments.get("label") or ("Active" if active else "Idle"))
    ttl_sec = float(arguments.get("ttl_sec", _HUD_IDLE_TIMEOUT_SEC))
    allow_capture = bool(arguments.get("allow_capture", False))
    cursor_style = arguments.get("cursor_style")
    cx = arguments.get("click_x")
    cy = arguments.get("click_y")
    mx = arguments.get("cursor_x")
    my = arguments.get("cursor_y")
    click_xy = (int(cx), int(cy)) if (cx is not None and cy is not None) else None
    cursor_xy = (int(mx), int(my)) if (mx is not None and my is not None) else click_xy
    info = update_overlay_hud(
        active,
        label,
        click_xy=click_xy,
        cursor_xy=cursor_xy,
        cursor_style=str(cursor_style) if cursor_style else None,
        ttl_sec=ttl_sec,
        allow_capture=allow_capture,
        force_flush=True,
    )
    state_txt = "shown" if active else "hidden"
    return tool_result(
        f"Screen-border glow & native cursor HUD {state_txt} (style={info.get('cursorStyle')}, ttl={info['ttlSec']}s).",
        structured=info,
    )


def cu_choose(arguments: dict[str, Any]) -> dict[str, Any]:
    """Fast local UI element matcher (`e<idx>`, `done`, or `unsure`) in under 1ms with zero screenshot."""
    goal = str(arguments.get("goal") or "").strip()
    if not goal:
        return error_result("Provide a non-empty 'goal' string.")
    conf_floor = max(0.1, min(float(arguments.get("confidence_floor", 0.80)), 0.99))

    elements = arguments.get("elements")
    obs_id = arguments.get("observation_id")
    if not isinstance(elements, list) and obs_id:
        sync_state()
        obs = _get_observation(str(obs_id))
        if not obs or "elements" not in obs:
            return error_result(f"observation_id '{obs_id}' not found or carries no UIA elements.")
        elements = obs["elements"]
    if not isinstance(elements, list):
        return error_result("Provide 'elements' array or an 'observation_id' from cu_ui_tree / cu_observe.")

    res = match_ui_element(goal, elements, confidence_floor=conf_floor)
    summary = (
        f"Local matcher chose '{res['choice']}' (confidence={res['confidence']:.3f}, "
        f"act={res['act']}, engine={res['engine']}, {res['elapsedMs']}ms)."
    )
    return tool_result(summary, structured=res)


def cu_step(arguments: dict[str, Any]) -> dict[str, Any]:
    """Fast local multi-goal UI pipeline: UIA snapshot -> calibrated matcher choice -> click -> optional state verification."""
    check_emergency_stop()
    hwnd = int(arguments["hwnd"])
    raw_goals = arguments.get("goals")
    raw_goal = str(arguments.get("goal") or "").strip()
    verify_contains = str(arguments.get("verify_contains") or "").strip() or None

    matcher_mod = _get_local_matcher()
    sub_goals: list[str] = []
    if isinstance(raw_goals, list) and raw_goals:
        for g in raw_goals:
            gs = str(g or "").strip()
            if gs:
                parts, inferred_done = matcher_mod.decompose_goal_steps(gs)
                sub_goals.extend(parts or [gs])
                if inferred_done and not verify_contains:
                    verify_contains = inferred_done
    elif raw_goal:
        parts, inferred_done = matcher_mod.decompose_goal_steps(raw_goal)
        sub_goals = parts or [raw_goal]
        if inferred_done and not verify_contains:
            verify_contains = inferred_done

    if not sub_goals:
        return error_result("Provide a non-empty 'goal' string or 'goals' array.")
    if len(sub_goals) > 24:
        return error_result("Too many decomposed steps (max 24 per cu_step call).")

    is_multi_goal = len(sub_goals) > 1 or bool(isinstance(raw_goals, list) and raw_goals)
    max_steps = len(sub_goals) if is_multi_goal else max(1, min(int(arguments.get("max_steps", 1)), 24))
    conf_floor = max(0.5, min(float(arguments.get("confidence_floor", 0.80)), 0.99))
    prefer_uia = bool(arguments.get("prefer_uia_invoke", False))

    _, err = require_window_permission(hwnd, need_input=True)
    if err:
        return error_result(err)

    t0 = time.perf_counter()
    trace: list[dict[str, Any]] = []
    final_status = "completed_steps"
    last_obs_id: str | None = None
    current_hwnd = hwnd

    sync_state()
    try:
        for step_no in range(max_steps):
            check_emergency_stop()
            step_goal = sub_goals[step_no] if is_multi_goal else sub_goals[0]

            # Auto-follow newly opened foreground popup owned by an already-granted process
            fg_hw = get_foreground_hwnd()
            if fg_hw > 0 and fg_hw != current_hwnd:
                _, fg_err = require_window_permission(fg_hw, need_input=True)
                if fg_err is None:
                    current_hwnd = fg_hw

            uia = run_uia_helper(current_hwnd, max_elements=110)
            elems = uia.get("elements") or []
            if isinstance(elems, dict):
                elems = [elems]

            obs_id = f"obs_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
            _store_observation(
                obs_id,
                {
                    "id": obs_id,
                    "timestamp": time.time(),
                    "kind": "ui_tree",
                    "hwnd": current_hwnd,
                    "originX": 0,
                    "originY": 0,
                    "scaleX": 1.0,
                    "scaleY": 1.0,
                    "bounds": get_window_bounds(current_hwnd),
                    "elements": elems,
                    "focusedElement": uia.get("focusedElement"),
                },
            )
            last_obs_id = obs_id

            decision = matcher_mod.match_ui_element(step_goal, elems, confidence_floor=conf_floor)
            if decision["choice"] == "done":
                final_status = "done"
                trace.append({"step": step_no, "goal": step_goal, "status": "done", "hwnd": current_hwnd, "decision": decision})
                break
            if not decision["act"] or decision["elementIndex"] is None:
                final_status = "hold"
                trace.append({
                    "step": step_no,
                    "goal": step_goal,
                    "status": "hold",
                    "hwnd": current_hwnd,
                    "decision": decision,
                    "observationId": last_obs_id,
                })
                break

            idx = int(decision["elementIndex"])
            click_res = cu_click_element({
                "observation_id": last_obs_id,
                "element_index": idx,
                "prefer_uia_invoke": prefer_uia,
            })
            if click_res.get("isError"):
                return click_res
            clicked_el = next((e for e in elems if int(e["index"]) == idx), {})
            trace.append({
                "step": step_no,
                "goal": step_goal,
                "status": "clicked",
                "hwnd": current_hwnd,
                "elementIndex": idx,
                "elementName": clicked_el.get("name"),
                "decision": decision,
                "observationId": last_obs_id,
            })
            if step_no + 1 < max_steps:
                time.sleep(0.008)

        verification: dict[str, Any] | None = None
        if verify_contains and final_status != "hold":
            time.sleep(0.025)
            v_uia = run_uia_helper(current_hwnd, max_elements=110)
            v_elems = v_uia.get("elements") or []
            if isinstance(v_elems, dict):
                v_elems = [v_elems]
            v_obs_id = f"obs_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
            _store_observation(
                v_obs_id,
                {
                    "id": v_obs_id,
                    "timestamp": time.time(),
                    "kind": "ui_tree",
                    "hwnd": current_hwnd,
                    "originX": 0,
                    "originY": 0,
                    "scaleX": 1.0,
                    "scaleY": 1.0,
                    "bounds": get_window_bounds(current_hwnd),
                    "elements": v_elems,
                    "focusedElement": v_uia.get("focusedElement"),
                },
            )
            last_obs_id = v_obs_id
            verification = matcher_mod.check_state_contains(verify_contains, v_elems)
            if verification.get("matched"):
                final_status = "done"
    finally:
        with _STATE_LOCK:
            save_state(STATE)

    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)
    out: dict[str, Any] = {
        "status": final_status,
        "hwnd": current_hwnd,
        "goal": raw_goal or " -> ".join(sub_goals),
        "decomposedGoals": sub_goals,
        "stepsExecuted": len(trace),
        "lastObservationId": last_obs_id,
        "elapsedMs": elapsed_ms,
        "trace": trace,
    }
    if verification is not None:
        out["verification"] = verification
    v_suffix = f", verified={verification['matched']} ({verification.get('matchedText')!r})" if verification else ""
    msg = (
        f"Local step pipeline -> status={final_status} "
        f"({len(trace)}/{len(sub_goals)} step(s) in {elapsed_ms}ms{v_suffix}, obs={last_obs_id})."
    )
    return tool_result(msg, structured=out)


def cu_clipboard_read(arguments: dict[str, Any]) -> dict[str, Any]:
    check_emergency_stop()
    if is_desktop_locked():
        return error_result("desktop_locked: Windows desktop is locked.")
    if not bool(arguments.get("allow_clipboard", False)):
        return error_result(
            "clipboard_permission_required: Pass allow_clipboard=true explicitly to read CF_UNICODETEXT from the Windows clipboard."
        )
    max_chars = max(1, min(int(arguments.get("max_chars", 16000)), 64000))
    if not _open_clipboard_with_retry():
        return error_result("clipboard_busy: Could not open Windows clipboard.")
    try:
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            data = {"hasText": False, "text": "", "length": 0, "truncated": False}
            return tool_result(json.dumps(data, ensure_ascii=False), structured=data)
        h_data = user32.GetClipboardData(CF_UNICODETEXT)
        if not h_data:
            data = {"hasText": False, "text": "", "length": 0, "truncated": False}
            return tool_result(json.dumps(data, ensure_ascii=False), structured=data)
        ptr = kernel32.GlobalLock(wintypes.HGLOBAL(h_data))
        if not ptr:
            return error_result("clipboard_lock_failed: GlobalLock returned NULL.")
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
    data = {"hasText": True, "text": text, "length": len(text), "truncated": truncated}
    return tool_result(json.dumps(data, ensure_ascii=False), structured=data)


def cu_clipboard_write(arguments: dict[str, Any]) -> dict[str, Any]:
    check_emergency_stop()
    if is_desktop_locked():
        return error_result("desktop_locked: Windows desktop is locked.")
    if "text" not in arguments or arguments["text"] is None:
        return error_result("Provide 'text' string to write to the Windows clipboard.")
    text = str(arguments["text"])
    if len(text) > 16000:
        return error_result("text_too_long: Max 16000 characters per cu_clipboard_write call.")

    encoded = text.encode("utf-16-le") + b"\x00\x00"
    if not _open_clipboard_with_retry():
        return error_result("clipboard_busy: Could not open Windows clipboard.")
    try:
        if not user32.EmptyClipboard():
            return error_result("clipboard_empty_failed: EmptyClipboard failed.")
        h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
        if not h_mem:
            return error_result("clipboard_alloc_failed: GlobalAlloc returned NULL.")
        ptr = kernel32.GlobalLock(wintypes.HGLOBAL(h_mem))
        if not ptr:
            kernel32.GlobalFree(wintypes.HGLOBAL(h_mem))
            return error_result("clipboard_lock_failed: GlobalLock returned NULL.")
        try:
            ctypes.memmove(ptr, encoded, len(encoded))
        finally:
            kernel32.GlobalUnlock(wintypes.HGLOBAL(h_mem))
        placed = user32.SetClipboardData(CF_UNICODETEXT, wintypes.HANDLE(h_mem))
        if not placed:
            kernel32.GlobalFree(wintypes.HGLOBAL(h_mem))
            return error_result("clipboard_set_failed: SetClipboardData failed.")
    finally:
        user32.CloseClipboard()

    with _STATE_LOCK:
        has_grants = bool(STATE.get("grants"))
    if has_grants:
        update_overlay_hud(True, "Active", ttl_sec=_HUD_IDLE_TIMEOUT_SEC)

    data = {"characters": len(text), "bytes": len(encoded)}
    return tool_result(f"Wrote {len(text)} Unicode character(s) to Windows clipboard.", structured=data)


BATCH_ALLOWED_ACTIONS = {
    "status": cu_status,
    "focus_window": cu_focus_window,
    "wait_for_window": cu_wait_for_window,
    "list_windows": cu_list_windows,
    "launch_app": cu_launch_app,
    "grant_app": cu_grant_app,
    "revoke_app": cu_revoke_app,
    "overlay": cu_overlay,
    "clipboard_read": cu_clipboard_read,
    "clipboard_write": cu_clipboard_write,
    "click": cu_click,
    "click_element": cu_click_element,
    "drag": cu_drag,
    "path": cu_drag,
    "stroke": cu_drag,
    "set_value": cu_set_value,
    "type": cu_type,
    "key": cu_key,
    "move": cu_move,
    "scroll": cu_scroll,
    "screenshot": cu_screenshot,
    "zoom": cu_zoom,
    "ui_tree": cu_ui_tree,
    "observe": cu_observe,
    "choose": cu_choose,
    "step": cu_step,
}

_ROUTINE_COMPACT_ACTIONS = {"click", "drag", "path", "stroke", "move", "type", "key", "scroll", "wait"}


def cu_batch(arguments: dict[str, Any]) -> dict[str, Any]:
    """Execute 1..80 guarded UI actions (steps or steps_file) with optional live stage HUD label in 1 MCP turn."""
    global _IN_BATCH_DEPTH, _BATCH_STAGE_LABEL
    steps = arguments.get("steps")
    steps_file = arguments.get("steps_file")
    if steps is None and steps_file:
        raw_sf = os.path.expandvars(str(steps_file)).strip()
        if _is_unc_or_device_path(raw_sf):
            return error_result(f"policy_blocked: UNC/device paths are not allowed for steps_file ({steps_file}).")
        sf_path = Path(raw_sf).expanduser()
        if sf_path.suffix.lower() != ".json":
            return error_result(f"policy_blocked: steps_file must be a .json file ({sf_path.name}).")
        if sf_path.is_symlink():
            return error_result(f"policy_blocked: Symlinks are not allowed for steps_file ({sf_path}).")
        if not sf_path.is_file():
            return error_result(f"steps_file not found: {sf_path}")
        try:
            if sf_path.stat().st_size > 1_048_576:
                return error_result(f"steps_file_too_large: steps_file exceeds 1 MiB limit ({sf_path}).")
            loaded = json.loads(sf_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return error_result(f"Invalid JSON in steps_file {sf_path}: {exc}")
        steps = loaded.get("steps") if isinstance(loaded, dict) else loaded
    if not isinstance(steps, list) or not (1 <= len(steps) <= 80):
        return error_result("Provide 'steps' or 'steps_file' containing 1..80 action objects.")
    stop_on_error = bool(arguments.get("stop_on_error", True))
    compact = bool(arguments.get("compact", len(steps) > 16))
    active_hwnd: int | None = int(arguments["hwnd"]) if arguments.get("hwnd") is not None else None
    stage_label = str(arguments.get("stage") or "").strip()[:48] or None

    t0 = time.perf_counter()
    step_summaries: list[dict[str, Any]] = []
    last_image_block: dict[str, Any] | None = None

    prev_stage = _BATCH_STAGE_LABEL
    if stage_label:
        _BATCH_STAGE_LABEL = stage_label
        with _STATE_LOCK:
            has_grants = bool(STATE.get("grants"))
        if has_grants:
            update_overlay_hud(True, stage_label, ttl_sec=_HUD_IDLE_TIMEOUT_SEC, force_flush=True)

    _IN_BATCH_DEPTH += 1
    _enter_1ms_timer()
    try:
        for idx, raw_step in enumerate(steps):
            check_emergency_stop()
            if not isinstance(raw_step, dict):
                return error_result(f"step[{idx}] must be an object.")
            act = str(raw_step.get("action") or "").strip().lower()
            if act.startswith("cu_"):
                act = act[3:]

            if act == "wait":
                if "seconds" in raw_step and "ms" not in raw_step:
                    ms = max(10, min(int(round(float(raw_step["seconds"]) * 1000.0)), 5000))
                else:
                    ms = max(10, min(int(raw_step.get("ms", 120)), 5000))
                w_end = time.monotonic() + (ms / 1000.0)
                while True:
                    check_emergency_stop()
                    rem = w_end - time.monotonic()
                    if rem <= 0:
                        break
                    time.sleep(min(0.05, rem))
                step_summaries.append({"step": idx, "action": "wait", "ok": True, "hwnd": active_hwnd, "ms": ms})
                continue

            fn = BATCH_ALLOWED_ACTIONS.get(act)
            if fn is None:
                msg = f"step[{idx}]: Unsupported batch action {act!r}. Allowed: {sorted([*BATCH_ALLOWED_ACTIONS, 'wait'])}"
                if stop_on_error:
                    return error_result(msg)
                step_summaries.append({"step": idx, "action": act, "ok": False, "error": msg})
                continue

            step_args = {k: v for k, v in raw_step.items() if k not in ("action", "delay_ms")}
            raw_hw = step_args.get("hwnd")
            if raw_hw in ("foreground", "auto", 0) and not isinstance(raw_hw, bool):
                fg = get_foreground_hwnd()
                if fg > 0:
                    step_args["hwnd"] = fg
                    active_hwnd = fg
            elif raw_hw == "matched" and active_hwnd is not None:
                step_args["hwnd"] = active_hwnd
            elif (
                raw_hw is None
                and active_hwnd is not None
                and "observation_id" not in step_args
                and not step_args.get("allow_full_desktop")
                and act not in (
                    "status",
                    "wait_for_window",
                    "list_windows",
                    "launch_app",
                    "revoke_app",
                    "overlay",
                    "clipboard_read",
                    "clipboard_write",
                    "choose",
                )
            ):
                step_args["hwnd"] = active_hwnd

            res = fn(step_args)
            is_err = bool(res.get("isError", False))
            sc = res.get("structuredContent") or {}
            if not is_err:
                if isinstance(sc.get("window"), dict) and sc["window"].get("hwnd"):
                    active_hwnd = int(sc["window"]["hwnd"])
                elif sc.get("hwnd"):
                    active_hwnd = int(sc["hwnd"])

            txt = next((c["text"] for c in res.get("content", []) if c.get("type") == "text"), "")
            for c in res.get("content", []):
                if c.get("type") == "image":
                    last_image_block = c

            is_last = idx + 1 == len(steps)
            keep_full_msg = is_err or is_last or act in ("observe", "ui_tree", "list_windows", "choose", "step", "launch_app", "wait_for_window")
            max_msg = 4200 if keep_full_msg else (0 if compact else 320)
            entry: dict[str, Any] = {
                "step": idx,
                "action": act,
                "ok": not is_err,
                "hwnd": active_hwnd,
            }
            if max_msg > 0:
                entry["message"] = txt[:max_msg]
            if sc.get("observationId"):
                entry["observationId"] = sc.get("observationId")
            step_summaries.append(entry)
            if is_err and stop_on_error:
                return error_result(f"batch_halted at step[{idx}] ({act}): {txt}")

            delay_ms = int(raw_step.get("delay_ms", 2))
            if delay_ms > 0 and not is_last:
                time.sleep(min(delay_ms, 2000) / 1000.0)
    finally:
        _BATCH_STAGE_LABEL = prev_stage
        _exit_1ms_timer()
        _IN_BATCH_DEPTH = max(0, _IN_BATCH_DEPTH - 1)
        flush_overlay_hud()

    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)
    data = {"completedSteps": len(step_summaries), "elapsedMs": elapsed_ms, "steps": step_summaries}
    if stage_label:
        data["stage"] = stage_label
    summary_lines = [f"Executed {len(step_summaries)} batched step(s) in {elapsed_ms}ms:"]

    if compact:
        i = 0
        n_s = len(step_summaries)
        while i < n_s:
            s = step_summaries[i]
            if s["ok"] and s["action"] in _ROUTINE_COMPACT_ACTIONS and i < n_s - 1 and not s.get("message"):
                j = i + 1
                counts: dict[str, int] = {s["action"]: 1}
                while (
                    j < n_s - 1
                    and step_summaries[j]["ok"]
                    and step_summaries[j]["action"] in _ROUTINE_COMPACT_ACTIONS
                    and not step_summaries[j].get("message")
                ):
                    a_j = step_summaries[j]["action"]
                    counts[a_j] = counts.get(a_j, 0) + 1
                    j += 1
                if (j - i) >= 3:
                    breakdown = ", ".join(f"{cnt}x {k}" for k, cnt in counts.items())
                    summary_lines.append(f"  [{i}..{j - 1}] {j - i}x input ({breakdown}) in HWND {s.get('hwnd')}: all OK")
                    i = j
                    continue
            detail = s.get("message") or (f"{s.get('ms')}ms" if "ms" in s else "OK")
            summary_lines.append(f"  [{s['step']}] {s['action']}: {'OK' if s['ok'] else 'ERR'} — {detail}")
            i += 1
    else:
        for s in step_summaries:
            detail = s.get("message") or f"{s.get('ms')}ms"
            summary_lines.append(f"  [{s['step']}] {s['action']}: {'OK' if s['ok'] else 'ERR'} — {detail}")

    if last_image_block is not None:
        return tool_result(
            "\n".join(summary_lines),
            structured=data,
            image_b64=last_image_block["data"],
            image_mime=last_image_block.get("mimeType", "image/png"),
        )
    return tool_result("\n".join(summary_lines), structured=data)


READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
SAFE_MUTATION = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
INTERACTIVE = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "cu_status",
        "title": "Computer Use status",
        "description": "Return Windows 10/11 desktop bounds, DPI mode, lock status, pointer coordinates, foreground window, and session app grants.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": READ_ONLY,
    },
    {
        "name": "cu_list_windows",
        "title": "List open windows",
        "description": "Enumerate visible non-cloaked Windows 10/11 top-level windows with HWND, PID, process name, shadow-free DWM bounds, elevation, policy tier, and grant state.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "max_results": {"type": "integer", "minimum": 1, "maximum": 200, "default": 60},
                "include_minimized": {"type": "boolean", "default": True},
            },
            "additionalProperties": False,
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "cu_wait_for_window",
        "title": "Wait for window",
        "description": "Poll non-cloaked visible Windows 10/11 top-level windows up to timeout_sec (0.5..15s) until title_contains and/or process_name matches.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title_contains": {"type": "string"},
                "process_name": {"type": "string"},
                "timeout_sec": {"type": "number", "minimum": 0.5, "maximum": 15, "default": 5},
                "include_minimized": {"type": "boolean", "default": True},
            },
            "additionalProperties": False,
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "cu_grant_app",
        "title": "Grant app access",
        "description": "Grant session Computer Use access to a target process_name or hwnd. Browsers default to look_only; terminals, password managers, security dialogs, and agent UIs are hard-blocked.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "process_name": {"type": "string"},
                "hwnd": {"type": "integer"},
                "allow_browser_input": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
        "annotations": SAFE_MUTATION,
    },
    {
        "name": "cu_revoke_app",
        "title": "Revoke app access",
        "description": "Revoke session Computer Use access for one process_name or all apps.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "process_name": {"type": "string"},
                "all": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
        "annotations": SAFE_MUTATION,
    },
    {
        "name": "cu_launch_app",
        "title": "Launch or focus desktop app",
        "description": "Universally launch or reuse any installed Windows desktop app by friendly name (via App Paths, Uninstall registry, WindowsApps, StartApps, PATH) or explicit non-blocked .exe path, optionally granting interactive browser input in 1 call.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "app": {"type": "string"},
                "auto_grant": {"type": "boolean", "default": True},
                "allow_browser_input": {"type": "boolean", "default": False},
                "reuse_existing": {"type": "boolean", "default": True},
                "args": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["app"],
            "additionalProperties": False,
        },
        "annotations": INTERACTIVE,
    },
    {
        "name": "cu_screenshot",
        "title": "Capture window or desktop screenshot",
        "description": "Capture a single granted window via Win32 PrintWindow (recommended) or full desktop via fast GDI BitBlt (allow_full_desktop=true). Supports fast 'webp'/'jpeg'/'png' encoding, frame-diff hashing, and Set-of-Mark UIA badges (annotate=true).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "hwnd": {"type": "integer"},
                "max_width": {"type": "integer", "minimum": 320, "maximum": 3840, "default": 1366},
                "format": {"type": "string", "enum": ["png", "webp", "jpeg"], "default": "png"},
                "quality": {"type": "integer", "minimum": 40, "maximum": 95, "default": 82},
                "annotate": {"type": "boolean", "default": False},
                "max_elements": {"type": "integer", "minimum": 10, "maximum": 120, "default": 60},
                "restore_if_minimized": {"type": "boolean", "default": True},
                "allow_full_desktop": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "cu_observe",
        "title": "Hybrid Set-of-Mark screenshot + UIA tree",
        "description": "One-shot fast observation (~40ms): captures a compact WebP window screenshot overlaid with Set-of-Mark [0..N] element badges AND returns the cached UIA/Win32 control tree (including large child canvas HWNDs) + frame-diff status under a single observation_id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "hwnd": {"type": "integer"},
                "max_width": {"type": "integer", "minimum": 320, "maximum": 3840, "default": 1280},
                "format": {"type": "string", "enum": ["webp", "jpeg", "png"], "default": "webp"},
                "quality": {"type": "integer", "minimum": 40, "maximum": 95, "default": 82},
                "annotate": {"type": "boolean", "default": True},
                "max_elements": {"type": "integer", "minimum": 10, "maximum": 120, "default": 100},
            },
            "required": ["hwnd"],
            "additionalProperties": False,
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "cu_zoom",
        "title": "Zoom into UI region",
        "description": "Capture a native 1:1 or upscaled high-resolution crop around (x, y, width, height) to read tiny toolbar icons, dense labels, or cells accurately.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "observation_id": {"type": "string"},
                "hwnd": {"type": "integer"},
                "x": {"type": "number"},
                "y": {"type": "number"},
                "width": {"type": "number", "minimum": 32, "maximum": 1600, "default": 320},
                "height": {"type": "number", "minimum": 32, "maximum": 1200, "default": 240},
                "upscale": {"type": "integer", "minimum": 1, "maximum": 3, "default": 2},
                "coordinate_space": {"type": "string", "enum": ["observation", "window", "screen"]},
                "allow_full_desktop": {"type": "boolean", "default": False},
            },
            "required": ["x", "y"],
            "additionalProperties": False,
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "cu_ui_tree",
        "title": "Inspect UI Automation tree",
        "description": "Walk Windows UI Automation controls for a granted HWND, returning focused element state and numbered interactive elements [0..N] for cu_click_element / cu_set_value.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "hwnd": {"type": "integer"},
                "max_elements": {"type": "integer", "minimum": 10, "maximum": 250, "default": 80},
            },
            "required": ["hwnd"],
            "additionalProperties": False,
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "cu_focus_window",
        "title": "Focus window",
        "description": "Bring an exact granted HWND to the foreground.",
        "inputSchema": {
            "type": "object",
            "properties": {"hwnd": {"type": "integer"}},
            "required": ["hwnd"],
            "additionalProperties": False,
        },
        "annotations": SAFE_MUTATION,
    },
    {
        "name": "cu_click_element",
        "title": "Click UIA element by index",
        "description": "Click or invoke an indexed UI Automation element from the latest cu_observe or cu_ui_tree observation_id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "observation_id": {"type": "string"},
                "element_index": {"type": "integer", "minimum": 0},
                "prefer_uia_invoke": {"type": "boolean", "default": False},
                "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
                "clicks": {"type": "integer", "minimum": 1, "maximum": 3, "default": 1},
            },
            "required": ["observation_id", "element_index"],
            "additionalProperties": False,
        },
        "annotations": INTERACTIVE,
    },
    {
        "name": "cu_set_value",
        "title": "Set UIA editable element value",
        "description": "Replace text in an editable control from cu_observe or cu_ui_tree via UIA ValuePattern or background Win32 message dispatch (with guarded click + Ctrl+A + Unicode SendInput fallback).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "observation_id": {"type": "string"},
                "element_index": {"type": "integer", "minimum": 0},
                "value": {"type": "string"},
                "prefer_background_msg": {"type": "boolean", "default": True},
            },
            "required": ["observation_id", "element_index", "value"],
            "additionalProperties": False,
        },
        "annotations": INTERACTIVE,
    },
    {
        "name": "cu_click",
        "title": "Click window coordinate",
        "description": "Click at (x, y) using observation_id pixel coordinates (auto-scaled) or screen/window coordinates, with occlusion, child-canvas focus transfer, and foreground collision checks.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "observation_id": {"type": "string"},
                "hwnd": {"type": "integer"},
                "x": {"type": "number"},
                "y": {"type": "number"},
                "coordinate_space": {"type": "string", "enum": ["observation", "window", "screen"]},
                "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
                "clicks": {"type": "integer", "minimum": 1, "maximum": 3, "default": 1},
                "duration": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["x", "y"],
            "additionalProperties": False,
        },
        "annotations": INTERACTIVE,
    },
    {
        "name": "cu_move",
        "title": "Move mouse pointer",
        "description": "Move pointer to (x, y) without clicking.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "observation_id": {"type": "string"},
                "hwnd": {"type": "integer"},
                "x": {"type": "number"},
                "y": {"type": "number"},
                "coordinate_space": {"type": "string", "enum": ["observation", "window", "screen"]},
                "duration": {"type": "number", "minimum": 0, "maximum": 2, "default": 0.075},
            },
            "required": ["x", "y"],
            "additionalProperties": False,
        },
        "annotations": SAFE_MUTATION,
    },
    {
        "name": "cu_drag",
        "title": "Drag pointer or stroke multi-point polyline/spline",
        "description": "Drag from (from_x, from_y) to (to_x, to_y) OR stroke a continuous 2..256-vertex path via points=[[x,y], ...] (with optional closed=true polygon loop and smooth=true Catmull-Rom spline) within a granted window.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "observation_id": {"type": "string"},
                "hwnd": {"type": "integer"},
                "from_x": {"type": "number"},
                "from_y": {"type": "number"},
                "to_x": {"type": "number"},
                "to_y": {"type": "number"},
                "points": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 256,
                    "items": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
                },
                "closed": {"type": "boolean", "default": False},
                "smooth": {"type": "boolean", "default": False},
                "hold_ms": {"type": "integer", "minimum": 0, "maximum": 1000, "default": 0},
                "coordinate_space": {"type": "string", "enum": ["observation", "window", "screen"]},
                "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
                "duration": {"type": "number", "minimum": 0.015, "maximum": 5, "default": 0.10},
            },
            "additionalProperties": False,
        },
        "annotations": INTERACTIVE,
    },
    {
        "name": "cu_scroll",
        "title": "Scroll wheel",
        "description": "Scroll vertically or horizontally at (x, y) inside a granted window (negative clicks = down/left, positive = up/right).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "observation_id": {"type": "string"},
                "hwnd": {"type": "integer"},
                "x": {"type": "number"},
                "y": {"type": "number"},
                "clicks": {"type": "integer", "minimum": -50, "maximum": 50, "default": -3},
                "horizontal": {"type": "boolean", "default": False},
                "coordinate_space": {"type": "string", "enum": ["observation", "window", "screen"]},
            },
            "required": ["x", "y"],
            "additionalProperties": False,
        },
        "annotations": INTERACTIVE,
    },
    {
        "name": "cu_type",
        "title": "Type Unicode text",
        "description": "Type Unicode text via batched Win32 SendInput into a focused, granted HWND with live focus-collision and sensitive-editor guards, plus optional clear_first and press_enter.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "hwnd": {"type": "integer"},
                "text": {"type": "string", "maxLength": 4000},
                "interval": {"type": "number", "minimum": 0, "maximum": 0.25, "default": 0.004},
                "activate_first": {"type": "boolean", "default": False},
                "clear_first": {"type": "boolean", "default": False},
                "press_enter": {"type": "boolean", "default": False},
            },
            "required": ["hwnd", "text"],
            "additionalProperties": False,
        },
        "annotations": INTERACTIVE,
    },
    {
        "name": "cu_key",
        "title": "Press key or chord",
        "description": "Send a key or hotkey chord (e.g. 'ctrl+s', 'enter', 'alt+f4') to a granted HWND. Windows/Super keys are blocked.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "hwnd": {"type": "integer"},
                "keys": {"type": "string", "description": "Single key or '+'-separated hotkey chord (e.g. 'ctrl+s', 'enter', 'alt+f4')."},
                "activate_first": {"type": "boolean", "default": True},
            },
            "required": ["hwnd", "keys"],
            "additionalProperties": False,
        },
        "annotations": INTERACTIVE,
    },
    {
        "name": "cu_clipboard_read",
        "title": "Read clipboard Unicode text",
        "description": "Read CF_UNICODETEXT from the Windows clipboard via Win32 OpenClipboard/GetClipboardData/GlobalLock when allow_clipboard=true is passed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "allow_clipboard": {"type": "boolean", "default": False},
                "max_chars": {"type": "integer", "minimum": 1, "maximum": 64000, "default": 16000},
            },
            "additionalProperties": False,
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "cu_clipboard_write",
        "title": "Write clipboard Unicode text",
        "description": "Write Unicode text (up to 16000 characters) onto the Windows clipboard as CF_UNICODETEXT via GlobalAlloc(GMEM_MOVEABLE)/GlobalLock/SetClipboardData.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "maxLength": 16000},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
        "annotations": INTERACTIVE,
    },
    {
        "name": "cu_overlay",
        "title": "Control screen-border glow & native cursor HUD",
        "description": "Show, update, or dismiss the pitch-black top status pill, screen-border glow, and native Win32 OS hardware cursor takeover ('sleek_dart', 'obsidian_badge', 'frost_badge', or 'pure_brand'), excluded from screenshots.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "active": {"type": "boolean", "default": True},
                "label": {"type": "string"},
                "cursor_style": {"type": "string", "enum": ["sleek_dart", "obsidian_badge", "frost_badge", "pure_brand"], "default": "sleek_dart"},
                "ttl_sec": {"type": "number", "minimum": 2, "maximum": 1800, "default": 600},
                "click_x": {"type": "integer"},
                "click_y": {"type": "integer"},
                "cursor_x": {"type": "integer"},
                "cursor_y": {"type": "integer"},
                "allow_capture": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
        "annotations": SAFE_MUTATION,
    },
    {
        "name": "cu_choose",
        "title": "Fast local UI element matcher",
        "description": "Fast local matcher: picks e<index>, done, or unsure from UI Automation elements (or observation_id) in under 1ms using Unicode/color/symbol matching with optional local LLM fallback.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string"},
                "observation_id": {"type": "string"},
                "elements": {"type": "array", "items": {"type": "object"}},
                "confidence_floor": {"type": "number", "minimum": 0.1, "maximum": 0.99, "default": 0.80},
            },
            "required": ["goal"],
            "additionalProperties": False,
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "cu_step",
        "title": "Fast local UI step or multi-goal pipeline",
        "description": "One-call local UI automation pipeline (1..24 steps): accepts a single 'goal', a compound sequence ('Click A -> B -> C' or arithmetic expression), or an explicit 'goals' array, snapshots UIA before each step, clicks when confidence >= confidence_floor (0.80), follows granted popups, and optionally verifies final UI text via verify_contains.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "hwnd": {"type": "integer"},
                "goal": {"type": "string"},
                "goals": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 24},
                "verify_contains": {"type": "string"},
                "max_steps": {"type": "integer", "minimum": 1, "maximum": 24, "default": 1},
                "confidence_floor": {"type": "number", "minimum": 0.5, "maximum": 0.99, "default": 0.80},
                "prefer_uia_invoke": {"type": "boolean", "default": False},
            },
            "required": ["hwnd"],
            "additionalProperties": False,
        },
        "annotations": INTERACTIVE,
    },
    {
        "name": "cu_batch",
        "title": "Execute multi-step UI action batch",
        "description": "Execute 1..80 guarded UI actions (inline 'steps' or JSON 'steps_file') sequentially in a single MCP turn with active_hwnd chaining, optional live HUD 'stage' label, and compact run-length output.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "hwnd": {"type": "integer"},
                "stage": {"type": "string"},
                "compact": {"type": "boolean"},
                "stop_on_error": {"type": "boolean", "default": True},
                "steps_file": {"type": "string"},
                "steps": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 80,
                    "items": {"type": "object"},
                },
            },
            "additionalProperties": False,
        },
        "annotations": INTERACTIVE,
    },
]

HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "cu_status": cu_status,
    "cu_list_windows": cu_list_windows,
    "cu_wait_for_window": cu_wait_for_window,
    "cu_grant_app": cu_grant_app,
    "cu_revoke_app": cu_revoke_app,
    "cu_launch_app": cu_launch_app,
    "cu_screenshot": cu_screenshot,
    "cu_observe": cu_observe,
    "cu_zoom": cu_zoom,
    "cu_ui_tree": cu_ui_tree,
    "cu_focus_window": cu_focus_window,
    "cu_click_element": cu_click_element,
    "cu_set_value": cu_set_value,
    "cu_click": cu_click,
    "cu_move": cu_move,
    "cu_drag": cu_drag,
    "cu_scroll": cu_scroll,
    "cu_type": cu_type,
    "cu_key": cu_key,
    "cu_clipboard_read": cu_clipboard_read,
    "cu_clipboard_write": cu_clipboard_write,
    "cu_overlay": cu_overlay,
    "cu_choose": cu_choose,
    "cu_step": cu_step,
    "cu_batch": cu_batch,
}


def dispatch_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    global _IN_FLIGHT_TOOLS, _LAST_TOOL_ACTIVITY, _LAST_TOOL_DONE_AT
    handler = HANDLERS.get(name)
    if handler is None:
        return error_result(f"Unknown tool: {name}")
    _LAST_TOOL_ACTIVITY = time.time()
    _IN_FLIGHT_TOOLS += 1
    tool_ok = False
    _PASSIVE_HUD_TOOLS = ("cu_revoke_app", "cu_overlay", "cu_status", "cu_list_windows")
    try:
        with _STATE_LOCK:
            has_grants = bool(STATE.get("grants"))
        if name not in _PASSIVE_HUD_TOOLS and has_grants:
            start_lbl = (
                arguments.get("stage")
                if (name == "cu_batch" and isinstance(arguments, dict) and arguments.get("stage"))
                else "Active"
            )
            update_overlay_hud(True, str(start_lbl or "Active"), ttl_sec=_HUD_IDLE_TIMEOUT_SEC, force_flush=True)
        res = handler(arguments)
        tool_ok = not bool(res.get("isError"))
        return res
    except Exception as exc:
        print(traceback.format_exc(), file=sys.stderr, flush=True)
        return error_result(f"{type(exc).__name__}: {exc}")
    finally:
        _IN_FLIGHT_TOOLS = max(0, _IN_FLIGHT_TOOLS - 1)
        _LAST_TOOL_DONE_AT = time.time()
        _LAST_TOOL_ACTIVITY = _LAST_TOOL_DONE_AT
        with _STATE_LOCK:
            has_grants = bool(STATE.get("grants"))
        if _IN_FLIGHT_TOOLS == 0 and tool_ok and name not in _PASSIVE_HUD_TOOLS and has_grants:
            keep_lbl = (
                arguments.get("stage")
                if (name == "cu_batch" and isinstance(arguments, dict) and arguments.get("stage"))
                else "Active"
            )
            update_overlay_hud(True, str(keep_lbl or "Active"), ttl_sec=_HUD_IDLE_TIMEOUT_SEC, force_flush=True)


def handle_mcp_request(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    req_id = message.get("id")
    if req_id is None:
        return None
    params = message.get("params") or {}
    if not isinstance(params, dict):
        params = {}
    if method == "initialize":
        requested = params.get("protocolVersion")
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": requested or DEFAULT_PROTOCOL,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": "Use cu_batch for multi-step Windows desktop workflows (up to 80 steps including launch_app, step, click, drag, type, key, observe, and revoke_app) in a single turn. Confirm consequential actions with the user and call revoke_app when finished.",
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        res = dispatch_tool(str(params.get("name") or ""), params.get("arguments") or {})
        return {"jsonrpc": "2.0", "id": req_id, "result": res}
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}


def _get_ancestor_pids() -> list[int]:
    """Return [parent_pid, grandparent_pid] via Toolhelp32 snapshot for orphan-proof server exit."""
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
    for _ in range(2):
        if cur > 4 and cur not in ancestors:
            ancestors.append(cur)
            cur = ppid_map.get(cur, 0)
        else:
            break
    return ancestors


_EXIT_CLEANED = False


def _cleanup_on_exit() -> None:
    """Idempotent stdio server exit cleanup: revoke session grants, purge temp screenshots/observations, dismiss HUD, restore cursors."""
    global _EXIT_CLEANED
    if _EXIT_CLEANED:
        return
    _EXIT_CLEANED = True
    try:
        with _STATE_LOCK:
            if STATE.get("grants"):
                STATE["grants"] = {}
                save_state(STATE)
    except Exception:
        pass
    try:
        _purge_session_artifacts()
    except Exception:
        pass
    try:
        update_overlay_hud(False, "Idle", force_flush=True)
    except Exception:
        pass
    _restore_os_cursors_safe()
    _close_helper_job()


def _start_lifecycle_watchdog() -> threading.Event:
    stop_ev = threading.Event()
    handles: list[int] = []
    for apid in _get_ancestor_pids():
        h = int(kernel32.OpenProcess(SYNCHRONIZE, False, int(apid)) or 0)
        if h:
            handles.append(h)

    def _watch() -> None:
        try:
            while not stop_ev.wait(2.0):
                # 1. Exit immediately if parent cmd.exe or MCP host exited (eliminates orphaned server & bridge zombies)
                for h in handles:
                    if kernel32.WaitForSingleObject(wintypes.HANDLE(h), 0) == 0:
                        _cleanup_on_exit()
                        os._exit(0)

                # 2. Sync HUD exit reasons (Esc immediately revokes grants & queues user_closed_computer_use notification for model's next call)
                global _LAST_REVOKED_ESC_TS, _PENDING_USER_CANCEL_TS
                pid_info = _read_overlay_pid_info()
                reason = str(pid_info.get("reason") or "")
                hud_st = str(pid_info.get("status") or "").lower()
                stopped_at = float(pid_info.get("stoppedAt") or 0.0)
                if reason == "esc" and hud_st == "exiting" and stopped_at > _LAST_REVOKED_ESC_TS:
                    if (time.time() - stopped_at) <= _GRANT_IDLE_TTL_SEC:
                        _LAST_REVOKED_ESC_TS = stopped_at
                        _PENDING_USER_CANCEL_TS = stopped_at
                        _revoke_all_on_emergency("Idle")
                elif hud_st == "exiting" and reason in ("idle_timeout", "human_takeover", "lease_expired") and _IN_FLIGHT_TOOLS == 0:
                    ov_now = read_overlay_state(throttle_stat_sec=0.0)
                    ov_upd = float(ov_now.get("updatedAt") or 0.0)
                    if ov_now.get("active") and stopped_at >= ov_upd:
                        _write_overlay_state({"active": False, "reason": reason, "stoppedAt": time.time()})

                # 3. Auto-expire stale session grants when idle past _GRANT_IDLE_TTL_SEC
                if _IN_FLIGHT_TOOLS == 0:
                    prune_expired_grants(save_if_changed=True)
        finally:
            for h in handles:
                try:
                    kernel32.CloseHandle(wintypes.HANDLE(h))
                except Exception:
                    pass

    threading.Thread(target=_watch, name="cua-lifecycle-watchdog", daemon=True).start()
    return stop_ev


def serve_stdio() -> int:
    global _STDIO_MODE, _UIA_PROC, _UIA_OUT_Q, _PRINTWINDOW_POOL
    _STDIO_MODE = True
    # Startup hygiene: recover any leaked cursors/HUD state and drop grants from dead prior servers
    _restore_os_cursors_safe()
    prune_expired_grants(save_if_changed=True)
    with _STATE_LOCK:
        has_grants = bool(STATE.get("grants"))
    if not has_grants:
        update_overlay_hud(False, "Idle", force_flush=True)

    dev_reload = os.environ.get("CUA_DEV_HOT_RELOAD", "0").strip().lower() in ("1", "true", "yes")
    this_file = Path(__file__).resolve()
    loaded_mtime_ns = this_file.stat().st_mtime_ns if this_file.is_file() else -1
    live_handler = handle_mcp_request
    atexit.register(_cleanup_on_exit)
    wd_stop = _start_lifecycle_watchdog()
    rpc_pool = ThreadPoolExecutor(max_workers=1)
    try:
        for raw_line in sys.stdin.buffer:
            if not raw_line.strip():
                continue
            try:
                if dev_reload and this_file.is_file():
                    cur_mtime = this_file.stat().st_mtime_ns
                    if cur_mtime != loaded_mtime_ns:
                        import importlib.util
                        spec = importlib.util.spec_from_file_location("_cua_hot_server", str(this_file))
                        if spec and spec.loader:
                            if _UIA_PROC is not None:
                                try:
                                    _UIA_PROC.kill()
                                except Exception:
                                    pass
                                _UIA_PROC = None
                                _UIA_OUT_Q = None
                            hot_mod = importlib.util.module_from_spec(spec)
                            spec.loader.exec_module(hot_mod)
                            hot_mod._STDIO_MODE = True
                            hot_mod.STATE = load_state()
                            live_handler = hot_mod.handle_mcp_request
                            loaded_mtime_ns = cur_mtime
                try:
                    msg = json.loads(raw_line)
                except json.JSONDecodeError as exc:
                    err_resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"Parse error: {exc}"}}
                    sys.stdout.write(json.dumps(err_resp, ensure_ascii=True, separators=(",", ":")) + "\n")
                    sys.stdout.flush()
                    continue
                if not isinstance(msg, dict):
                    err_resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
                    sys.stdout.write(json.dumps(err_resp, ensure_ascii=True, separators=(",", ":")) + "\n")
                    sys.stdout.flush()
                    continue
                req_id = msg.get("id")
                _ABORT_EVENT.clear()
                fut = rpc_pool.submit(live_handler, msg)
                try:
                    resp = fut.result(timeout=45.0)
                except FuturesTimeoutError:
                    _ABORT_EVENT.set()
                    if _UIA_PROC is not None:
                        try:
                            _UIA_PROC.kill()
                        except Exception:
                            pass
                        _UIA_PROC = None
                        _UIA_OUT_Q = None
                    time.sleep(0.15)
                    rpc_pool.shutdown(wait=False, cancel_futures=True)
                    rpc_pool = ThreadPoolExecutor(max_workers=1)
                    _ABORT_EVENT.clear()
                    resp = (
                        {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": error_result("mcp_deadline_exceeded: Tool call exceeded 45s safety limit; recovered cleanly."),
                        }
                        if req_id is not None
                        else None
                    )
                if resp is not None:
                    sys.stdout.write(json.dumps(resp, ensure_ascii=True, separators=(",", ":")) + "\n")
                    sys.stdout.flush()
            except Exception as exc:
                print(f"MCP error: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    finally:
        wd_stop.set()
        rpc_pool.shutdown(wait=False, cancel_futures=True)
        with _PRINTWINDOW_POOL_LOCK:
            if _PRINTWINDOW_POOL is not None:
                _PRINTWINDOW_POOL.shutdown(wait=False, cancel_futures=True)
                _PRINTWINDOW_POOL = None
        _cleanup_on_exit()
    return 0


def run_cli_call(tool_name: str, raw_args: str) -> int:
    args = json.loads(raw_args) if raw_args.strip() else {}
    res = dispatch_tool(tool_name, args)
    # Strip bulky inline base64 image in CLI stdout since savedPath is already written for read_file
    slim_content = [c for c in res.get("content", []) if c.get("type") != "image"]
    out = {"isError": res.get("isError", False), "content": slim_content, "structuredContent": res.get("structuredContent")}
    print(json.dumps(out, ensure_ascii=True, indent=2))
    return 1 if out["isError"] else 0


def run_self_test() -> int:
    status = cu_status({})["structuredContent"]
    wins = cu_list_windows({"max_results": 15})["structuredContent"]
    # Verify policy blocks powershell.exe
    blocked_check = cu_grant_app({"process_name": "powershell.exe"})
    # Verify sensitive file guard
    sens_check = check_sensitive_editor_title("notepad.exe", "mcp_config.json - Notepad")
    summary = {
        "server": f"{SERVER_NAME}@{SERVER_VERSION}",
        "toolCount": len(TOOLS),
        "dpiAware": status["dpiAware"],
        "desktopLocked": status["desktopLocked"],
        "virtualDesktop": status["virtualDesktop"],
        "visibleWindowCount": wins["count"],
        "sampleWindows": [
            {"hwnd": w["hwnd"], "proc": w["processName"], "tier": w["policyTier"], "title": w["title"][:60]}
            for w in wins["windows"][:6]
        ],
        "terminalBlockVerified": blocked_check["isError"],
        "sensitiveEditorGuardVerified": sens_check is not None,
    }
    print(json.dumps(summary, ensure_ascii=True, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Windows 10/11 Computer Use MCP Server & CLI")
    parser.add_argument("--self-test", action="store_true", help="Run non-invasive readiness & security self-test")
    parser.add_argument("--call", type=str, help="Invoke a single tool by name via CLI")
    parser.add_argument("--args", type=str, default="{}", help="JSON object of tool arguments for --call")
    ns = parser.parse_args()
    if ns.self_test:
        return run_self_test()
    if ns.call:
        return run_cli_call(ns.call, ns.args)
    return serve_stdio()


if __name__ == "__main__":
    raise SystemExit(main())
