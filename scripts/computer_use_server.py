"""Computer Use MCP Server & CLI for macOS and Windows (v3.2.0).

Unified MCP JSON-RPC 2.0 server dispatching OS primitives to native platform
backends (`win32_backend.py` on Windows 10/11, `macos_backend.py` + compiled
Swift helpers on macOS) while sharing:
  - All 25 MCP tools and 1..80-step `cu_batch` engine with `steps_file` & live stages
  - Retina 2x logical-point and Per-Monitor DPI v2 coordinate calibration
  - Popup-aware screenshots, Set-of-Mark [0..N] element overlays, and `cu_zoom`
  - Fast local UI element matcher (`local_matcher.py`, <1ms Tier 1 + opt-in Tier 2)
  - Six safety layers: expiring session grants, terminal/password/security/agent
    deny-list, browser `look_only` tier, sensitive-file editor guard, focus/occlusion
    checks, and instant physical Esc / top-left corner emergency stop.
"""

from __future__ import annotations

import argparse
import atexit
import base64
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
import importlib
import io
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from typing import Any, Callable

_pylibs_env = os.environ.get("CUA_PYLIBS_DIR", "").strip()
if _pylibs_env and _pylibs_env not in sys.path:
    sys.path.insert(0, _pylibs_env)

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

# Load platform backend BEFORE PIL so Windows PerMonitorV2 DPI awareness runs first
if sys.platform == "win32":
    import win32_backend as _backend  # noqa: E402
elif sys.platform == "darwin":
    import macos_backend as _backend  # noqa: E402
else:
    raise RuntimeError(f"Unsupported platform for Antigravity Computer Use: {sys.platform}")

from PIL import Image, ImageDraw, ImageFont  # noqa: E402
import local_matcher as _local_matcher_mod  # noqa: E402

SERVER_NAME = "computer-use"
SERVER_VERSION = "3.2.0"
DEFAULT_PROTOCOL = "2024-11-05"

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
    if sys.platform == "darwin":
        candidates.append(Path.home() / "Library" / "Application Support" / "antigravity-computer-use")
        candidates.append(Path(tempfile.gettempdir()) / "antigravity-computer-use")
    else:
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
OVERLAY_STATE_FILE = RUNTIME_DIR / "overlay_state.json"
OVERLAY_PID_FILE = RUNTIME_DIR / "overlay_pid.json"

_backend.configure(RUNTIME_DIR, OVERLAY_PID_FILE)
_IS_LOW_INTEGRITY = _backend.IS_LOW_INTEGRITY

# Re-export Windows helpers expected directly by unit tests on win32
if sys.platform == "win32":
    _get_pe_subsystem = _backend._get_pe_subsystem
    _resolve_vk_token = _backend._resolve_vk_token


# Security Policy Tables (Unified Windows + macOS)
BLOCKED_PROCESSES = {
    # Windows shells, terminals & script/LOLBin launchers
    "cmd.exe", "powershell.exe", "pwsh.exe", "windowsterminal.exe", "wt.exe",
    "conhost.exe", "openconsole.exe", "mintty.exe", "alacritty.exe", "wezterm-gui.exe",
    "powershell_ise.exe", "wsl.exe", "git-bash.exe", "putty.exe", "tabby.exe",
    "hyper.exe", "conemu.exe", "conemu64.exe", "bash.exe", "sh.exe", "zsh.exe",
    "ubuntu.exe", "debian.exe", "kali.exe", "mshta.exe", "cscript.exe", "wscript.exe",
    "rundll32.exe", "regsvr32.exe", "reg.exe", "regedt32.exe", "runas.exe",
    "schtasks.exe", "sc.exe", "net.exe", "net1.exe", "certutil.exe", "bitsadmin.exe",
    "msiexec.exe",
    # Windows password managers, UAC, security, admin & debuggers
    "1password.exe", "bitwarden.exe", "keepass.exe", "keepassxc.exe", "dashlane.exe",
    "credentialuibroker.exe", "explorer.exe", "consent.exe", "lockapp.exe", "logonui.exe",
    "sechealthui.exe", "regedit.exe", "taskmgr.exe", "mmc.exe", "procexp.exe",
    "procexp64.exe", "procmon.exe", "procmon64.exe", "windbg.exe", "x64dbg.exe",
    # Windows agent hosts
    "antigravity.exe", "codex.exe", "claude.exe", "cursor.exe",
    # macOS terminals & shells
    "terminal", "iterm2", "iterm", "warp", "ghostty", "alacritty", "kitty",
    "wezterm", "wezterm-gui", "hyper", "tabby", "bash", "sh", "zsh", "fish",
    "osascript", "sudo", "su", "cmd", "powershell", "pwsh", "wsl", "wt",
    # macOS password managers, auth prompts, TCC / security agents & system monitor
    "keychain access", "passwords", "1password", "bitwarden", "keepassxc", "dashlane",
    "activity monitor", "securityagent", "coreservicesuiagent",
    "localauthenticationuicompanion", "authenticationservicesagent",
    "usernotificationcenter", "loginwindow", "screensaverengine",
    # macOS agent hosts (prevent self-approval / prompt-injection self-hijack)
    "antigravity", "cursor", "claude", "codex",
}

_BLOCKED_LAUNCH_RUNTIMES = {
    "python.exe", "pythonw.exe", "py.exe", "node.exe", "deno.exe", "bun.exe",
    "python", "python3", "node", "deno", "bun", "osascript", "open", "sh", "bash", "zsh",
    "perl", "ruby", "php", "lua", "swift", "xcrun", "env", "ssh", "tmux", "screen", "expect",
}

_BLOCKED_AUMID_FRAGMENTS = (
    "windowsterminal", "terminal", "iterm", "warp", "ghostty", "alacritty", "kitty",
    "powershell", "pwsh", "commandprompt", "cmd.exe", "wsl", "ubuntu", "debian",
    "kali", "opensuse", "regedit", "taskmgr", "keychainaccess", "activitymonitor",
    "1password", "onepassword", "bitwarden", "keepass", "dashlane", "passwords",
    "antigravity", "claude", "codex", "cursor",
)

BROWSER_PROCESSES = {
    # Windows
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe", "arc.exe",
    # macOS
    "safari", "arc", "google chrome", "brave browser", "firefox", "microsoft edge",
    "opera", "vivaldi", "chromium", "chrome", "edge", "brave",
}

EDITOR_PROCESSES = {
    # Windows
    "notepad.exe", "notepad++.exe", "wordpad.exe", "code.exe", "code - insiders.exe",
    "windsurf.exe", "vscodium.exe", "zed.exe", "sublime_text.exe",
    "devenv.exe", "idea64.exe", "pycharm64.exe", "webstorm64.exe",
    # macOS
    "textedit", "code", "visual studio code", "code - insiders", "windsurf",
    "vscodium", "zed", "sublime text", "sublime_text", "bbedit", "nova",
    "xcode", "pycharm", "intellij idea", "webstorm", "goland", "clion",
}

_MACOS_BLOCKED_SETTINGS_PROCS = {"system settings", "system preferences"}
_MACOS_BLOCKED_SETTINGS_TITLE_FRAGS = (
    "privacy", "security", "touch id", "password", "passkey", "filevault",
    "accessibility", "screen recording", "screen & system audio", "audio recording",
    "full disk access", "input monitoring", "automation", "profiles",
    "device management", "users & groups", "login items", "firewall",
    "gatekeeper", "lock screen", "apple id", "apple account", "icloud",
)

SENSITIVE_TITLE_PATTERNS = [
    re.compile(r"(?:^|[\s\\/\[\(●])(\.bashrc|\.bash_profile|\.zshrc|\.zprofile|\.profile|profile\.ps1|microsoft\.powershell_profile\.ps1)(?:$|[\s\*—\-–\]\):,])", re.I),
    re.compile(r"(?:^|[\s\\/\[\(●])(authorized_keys|known_hosts|id_rsa|id_ed25519|id_ecdsa|\.ssh)(?:$|[\s\\/\*—\-–\]\):,])", re.I),
    re.compile(r"(?:^|[\s\\/\[\(●])(\.gitconfig|mcp_config\.json|claude_desktop_config\.json|\.claude\.json|settings\.json|config\.toml)(?:$|[\s\*—\-–\]\):,])", re.I),
    re.compile(r"(?:^|[\s\\/\[\(●])(\.env(?:\.[a-z0-9_.-]+)?|\.npmrc|\.pypirc|\.netrc)(?:$|[\s\*—\-–\]\):,])", re.I),
    re.compile(r"(?:^|[\s\\/\[\(●])credentials(?:\.[a-z0-9_.-]+)?(?:$|[\s\\/\*—\-–\]\):,])", re.I),
    re.compile(r"\.git[\\/]hooks", re.I),
    re.compile(r"\.(bat|cmd|reg|vbs|vbe|jse|wsf|wsh|hta|ps1|sh|zsh|pem|key|pfx|p12)(?:$|[\s\*—\-–\]\):,])", re.I),
]

BLOCKED_KEYS_WIN32 = {
    "win", "winleft", "winright", "windows", "super", "meta", "cmd", "command", "os",
}
BLOCKED_KEYS_DARWIN = {
    "win", "winleft", "winright", "windows", "os",
}


def _norm_app_key(s: str) -> str:
    low = (s or "").lower()
    if low.endswith(".exe"):
        low = low[:-4]
    elif low.endswith(".app"):
        low = low[:-4]
    return re.sub(r"[\W_]+", "", low, flags=re.UNICODE)


def _is_unc_or_device_path(s: str) -> bool:
    norm = (s or "").strip(" \"'").replace("/", "\\")
    return norm.startswith("\\\\")


def _canonical_proc_or_name(raw: str) -> str:
    cleaned = (raw or "").strip(" \"'")
    if cleaned and not _is_unc_or_device_path(cleaned) and ("\\" in cleaned or "/" in cleaned or (sys.platform == "win32" and ":" in cleaned)):
        try:
            p = Path(os.path.expanduser(os.path.expandvars(cleaned)))
            if p.exists():
                return p.resolve().name
        except OSError:
            pass
    return cleaned


def _norm_proc_name(name: str) -> str:
    s = (name or "").strip(" \"'").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].rstrip(".").lower()
    if sys.platform == "win32":
        if s and "." not in s:
            s = f"{s}.exe"
        return s
    if s.endswith(".app"):
        s = s[:-4]
    return s


def classify_policy(process_name: str) -> str:
    canon = _canonical_proc_or_name(process_name)
    low = _norm_proc_name(canon)
    stem = low[:-4] if low.endswith(".exe") else low
    candidates = {low, stem, f"{stem}.exe"} if stem else {low}
    if candidates & BLOCKED_PROCESSES:
        return "blocked"
    if candidates & BROWSER_PROCESSES:
        return "look_only"
    return "interactive"


def check_blocked_system_window(process_name: str, title: str) -> str | None:
    if sys.platform != "darwin":
        return None
    low_proc = _norm_proc_name(process_name)
    if low_proc in _MACOS_BLOCKED_SETTINGS_PROCS:
        low_title = (title or "").lower()
        for frag in _MACOS_BLOCKED_SETTINGS_TITLE_FRAGS:
            if frag in low_title:
                return (
                    f"policy_blocked: System Settings security/privacy pane '{title}' is blocked "
                    "(OS security, password, and TCC permission settings require manual human interaction)."
                )
    return None


def check_sensitive_editor_title(process_name: str, title: str) -> str | None:
    low_proc = _norm_proc_name(process_name)
    stem = low_proc[:-4] if low_proc.endswith(".exe") else low_proc
    low_title = (title or "").lower()
    is_editor = (
        low_proc in EDITOR_PROCESSES
        or stem in EDITOR_PROCESSES
        or f"{stem}.exe" in EDITOR_PROCESSES
        or " - notepad" in low_title
        or "visual studio code" in low_title
        or "textedit" in low_title
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


def _is_blocked_launch_target(raw_app: str, cmd: list[str], extra_args: list[str] | None = None) -> str | None:
    return _backend.is_blocked_launch_target(
        raw_app,
        cmd,
        extra_args,
        classify_policy_fn=classify_policy,
        norm_proc_fn=_norm_proc_name,
        blocked_runtimes=_BLOCKED_LAUNCH_RUNTIMES,
        blocked_aumid_fragments=_BLOCKED_AUMID_FRAGMENTS,
    )


# Runtime State & Atomic JSON Storage
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
_IN_BATCH_DEPTH: int = 0
_CURSOR_ENGAGED = False

STATE = load_state()
_STDIO_MODE = False
_OVERLAY_ALIVE_CACHE: tuple[float, bool] = (0.0, False)
_OVERLAY_PROC: subprocess.Popen[Any] | None = None


def prune_expired_grants(*, save_if_changed: bool = True) -> bool:
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
                if old_boot and old_boot != _SERVER_BOOT_ID and old_pid > 0 and not _backend.pid_is_alive(old_pid):
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


# Overlay HUD IPC & Esc Emergency Stop
_OVERLAY_READ_KEY: tuple[int, int] = (-1, -1)
_OVERLAY_READ_CACHE: dict[str, Any] = {"active": False}
_OVERLAY_LAST_STAT_MONO: float = 0.0
_OVERLAY_LAST_WRITE_MONO: float = 0.0
_OVERLAY_DIRTY_PAYLOAD: dict[str, Any] | None = None
_OVERLAY_MIN_WRITE_INTERVAL_SEC: float = 0.120
_BATCH_STAGE_LABEL: str | None = None

_LAST_HANDLED_ESC_TS: float = 0.0
_LAST_REVOKED_ESC_TS: float = 0.0
_PENDING_USER_CANCEL_TS: float = 0.0


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


def _read_overlay_pid_info() -> dict[str, Any]:
    try:
        if OVERLAY_PID_FILE.is_file():
            data = json.loads(OVERLAY_PID_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def is_overlay_daemon_alive(*, force: bool = False) -> bool:
    global _OVERLAY_ALIVE_CACHE, _OVERLAY_PROC
    now = time.monotonic()
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
        if st_hud == "running" and pid > 0 and _backend.pid_is_alive(pid):
            exe_low = _backend.query_pid_exe(pid).lower()
            if _backend.is_overlay_exe_match(exe_low):
                alive = True
    except Exception:
        pass
    _OVERLAY_ALIVE_CACHE = (now, alive)
    return alive


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
    elif active and isinstance(prev.get("click"), dict) and (now - float(prev["click"].get("ts", 0)) <= 1.0):
        payload["click"] = prev["click"]

    if not active:
        _write_overlay_state(payload)
        _OVERLAY_ALIVE_CACHE = (now_m, False)
        _OVERLAY_PROC = None
        _backend.restore_os_cursors_safe()
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
    desk = _backend.current_desktop_name().lower()
    if (desk == "default" or force_spawn) and not alive:
        if _OVERLAY_DIRTY_PAYLOAD is not None:
            _write_overlay_state(payload)
        proc = _backend.spawn_overlay_hud(RUNTIME_DIR)
        if proc is not None:
            _OVERLAY_PROC = proc
            spawned_pid = int(proc.pid)
            _OVERLAY_ALIVE_CACHE = (time.monotonic(), True)
            alive = True
            _atomic_write_json(OVERLAY_PID_FILE, {"pid": spawned_pid, "status": "running", "startedAt": now})

    return payload | {"daemonRunning": alive, "spawnedPid": spawned_pid}


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
    if _backend.is_escape_pressed():
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
    if check_corner and _CURSOR_ENGAGED and not _IS_LOW_INTEGRITY and _backend.current_desktop_name().lower() == "default":
        px, py = _backend.get_cursor_pos()
        for ml, mt, _, _ in _backend.get_monitor_rects():
            if ml <= px <= ml + 1 and mt <= py <= mt + 1:
                _revoke_all_on_emergency("Idle")
                raise RuntimeError("emergency_stop: Pointer moved to monitor upper-left corner failsafe; Computer Use halted.")


# Window Inspection & Permission Enforcement
_PROC_META_CACHE: dict[tuple[int, int], tuple[str, str, bool]] = {}
_NON_EDITOR_TITLE_CACHE: dict[tuple[int, int], tuple[float, str]] = {}


def _get_cached_proc_meta(hwnd: int) -> tuple[int, str, str, bool]:
    pid_val = _backend.get_window_pid(hwnd)
    if pid_val <= 0:
        return 0, "unknown", "", False
    ctime = _backend.get_proc_create_time(pid_val)
    cache_key = (pid_val, ctime) if ctime is not None else None
    if cache_key is not None:
        cached = _PROC_META_CACHE.get(cache_key)
        if cached is not None:
            return pid_val, cached[0], cached[1], cached[2]
    _, proc_name, exe_path = _backend.get_process_info(hwnd)
    elev = _backend.is_process_elevated(pid_val)
    if cache_key is not None and proc_name != "unknown":
        while len(_PROC_META_CACHE) >= 256:
            _PROC_META_CACHE.pop(next(iter(_PROC_META_CACHE)))
        _PROC_META_CACHE[cache_key] = (proc_name, exe_path, elev)
    return pid_val, proc_name, exe_path, elev


def inspect_hwnd(hwnd: int) -> dict[str, Any] | None:
    if not _backend.is_window(hwnd):
        return None
    pid, proc_name, exe_path, is_elev = _get_cached_proc_meta(hwnd)
    low_proc = _norm_proc_name(proc_name)
    if _IN_BATCH_DEPTH > 0 and low_proc not in EDITOR_PROCESSES and low_proc not in _MACOS_BLOCKED_SETTINGS_PROCS:
        now_m = time.monotonic()
        t_key = (int(hwnd), pid)
        cached_t = _NON_EDITOR_TITLE_CACHE.get(t_key)
        if cached_t is not None and (now_m - cached_t[0]) < 0.25:
            title = cached_t[1]
        else:
            title = _backend.get_window_title_safe(hwnd, timeout_ms=120)
            _NON_EDITOR_TITLE_CACHE[t_key] = (now_m, title)
    else:
        title = _backend.get_window_title_safe(hwnd, timeout_ms=120)
    bounds = _backend.get_window_bounds(hwnd)
    fg = _backend.get_foreground_hwnd()
    tier = classify_policy(proc_name) if sys.platform == "darwin" else classify_policy(exe_path or proc_name)
    if tier != "blocked" and check_blocked_system_window(proc_name, title) is not None:
        tier = "blocked"
    with _STATE_LOCK:
        grants = STATE.get("grants", {})
        raw_grant = grants.get(low_proc) or grants.get(proc_name.lower())
        grant = dict(raw_grant) if isinstance(raw_grant, dict) else None
    return {
        "hwnd": int(hwnd),
        "pid": pid,
        "processName": proc_name,
        "exePath": exe_path,
        "title": title,
        "bounds": bounds,
        "isForeground": _backend.is_foreground_for_window(hwnd, fg),
        "isMinimized": _backend.is_window_minimized(hwnd),
        "isVisible": _backend.is_window_visible(hwnd),
        "isElevated": is_elev,
        "policyTier": tier,
        "granted": grant is not None,
        "grantMode": grant.get("mode") if grant is not None else None,
    }


def require_window_permission(hwnd: int, *, need_input: bool) -> tuple[dict[str, Any], str | None]:
    sync_state()
    if _backend.is_desktop_locked():
        return {}, "desktop_locked: Desktop session is locked. Ask the user to unlock before using Computer Use."
    info = inspect_hwnd(hwnd)
    if info is None or not info["isVisible"]:
        return {}, f"window_unavailable: Window {hwnd} is no longer open or visible."
    proc_low = _norm_proc_name(info["processName"])
    tier = info["policyTier"]
    sys_blk = check_blocked_system_window(info["processName"], info["title"])
    if sys_blk:
        return info, sys_blk
    if tier == "blocked":
        return info, f"policy_blocked: Process '{info['processName']}' is on the mandatory Computer Use deny-list (terminals, password managers, OS security, and agent UIs are blocked)."
    with _STATE_LOCK:
        grants = STATE.get("grants", {})
        grant = grants.get(proc_low) or grants.get(info["processName"].lower())
        if grant is None:
            return info, f"app_not_granted: Process '{info['processName']}' (window {hwnd}) has not been granted access in this session. Call cu_grant_app first."
        grant["lastUsedEpoch"] = time.time()
        grant_mode = grant.get("mode")
    if need_input:
        if grant_mode == "look_only":
            return info, f"tier_look_only: '{info['processName']}' is granted in look_only mode (prefer a dedicated browser automation tool, or explicitly re-grant with allow_browser_input=true if user-authorized)."
        if info["isElevated"] and not _backend.is_process_elevated(os.getpid()):
            return info, f"elevated_window_uipi: Target '{info['processName']}' is running elevated (root/Administrator); standard-user input is blocked."
        sens_err = check_sensitive_editor_title(info["processName"], info["title"])
        if sens_err:
            return info, sens_err
    return info, None


# Image Hashing, Set-of-Mark Annotation & Encoding
_LAST_FRAME_HASHES: dict[str, int] = {}


def compute_dhash(image: Image.Image) -> tuple[str, int]:
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
    annotated = image.convert("RGBA")
    overlay = Image.new("RGBA", annotated.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = _backend.get_som_font()

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
            continue
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
            cur = _backend.get_window_bounds(int(hwnd))
            orig = obs.get("bounds") or {}
            if abs(cur["left"] - orig.get("left", cur["left"])) > 4 or abs(cur["top"] - orig.get("top", cur["top"])) > 4 or abs(cur["width"] - orig.get("width", cur["width"])) > 4:
                raise ValueError(f"window_moved: Target window moved/resized since '{obs_id}'. Capture a fresh screenshot before clicking.")
        if coord_space == "observation":
            sx = int(round(obs["originX"] + raw_x * obs["scaleX"]))
            sy = int(round(obs["originY"] + raw_y * obs["scaleY"]))
            return sx, sy, hwnd
        if coord_space == "window" and hwnd is not None:
            b = _backend.get_window_bounds(int(hwnd))
            return int(round(b["left"] + raw_x)), int(round(b["top"] + raw_y)), int(hwnd)
    hwnd_arg = int(arguments["hwnd"]) if "hwnd" in arguments and arguments["hwnd"] is not None else None
    if coord_space == "window" and hwnd_arg is not None:
        b = _backend.get_window_bounds(hwnd_arg)
        return int(round(b["left"] + raw_x)), int(round(b["top"] + raw_y)), hwnd_arg
    return int(round(raw_x)), int(round(raw_y)), hwnd_arg


# MCP Response Helpers
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
    minimized = _backend.is_window_minimized(hwnd)
    bounds = _backend.get_window_bounds(hwnd)
    pid, proc_name, exe_path = _backend.get_process_info(hwnd)
    tier = classify_policy(proc_name) if sys.platform == "darwin" else classify_policy(exe_path or proc_name)
    if tier != "blocked" and check_blocked_system_window(proc_name, title) is not None:
        tier = "blocked"
    low_proc = _norm_proc_name(proc_name)
    with _STATE_LOCK:
        grants = STATE.get("grants", {})
        raw_grant = grants.get(low_proc) or grants.get(proc_name.lower())
        grant = dict(raw_grant) if isinstance(raw_grant, dict) else None
    return {
        "hwnd": hwnd,
        "pid": pid,
        "processName": proc_name,
        "exePath": exe_path,
        "title": title,
        "foreground": _backend.is_foreground_for_window(hwnd, fg_hwnd),
        "minimized": minimized,
        "elevated": _backend.is_process_elevated(pid),
        "policyTier": tier,
        "granted": grant is not None,
        "grantMode": grant.get("mode") if grant is not None else None,
        "bounds": bounds,
    }


# Tool Handlers (25 Tools)
def cu_status(_: dict[str, Any]) -> dict[str, Any]:
    sync_state()
    px, py = _backend.get_cursor_pos()
    fg = _backend.get_foreground_hwnd()
    fg_info = inspect_hwnd(fg) if fg else None
    with _STATE_LOCK:
        grants_copy = dict(STATE.get("grants", {}))
    data: dict[str, Any] = {
        "platform": sys.platform,
        "dpiAware": _backend.DPI_MODE,
        "serverVersion": SERVER_VERSION,
        "desktopName": _backend.current_desktop_name(),
        "desktopLocked": _backend.is_desktop_locked(),
        "virtualDesktop": _backend.virtual_bounds(),
        "pointer": {"x": px, "y": py},
        "foregroundWindow": fg_info,
        "grantedApps": grants_copy,
        "overlay": read_overlay_state() | {"daemonRunning": is_overlay_daemon_alive()},
    }
    data.update(_backend.platform_status_extra())
    return tool_result(json.dumps(data, ensure_ascii=False), structured=data)


def cu_list_windows(arguments: dict[str, Any]) -> dict[str, Any]:
    sync_state()
    max_results = max(1, min(int(arguments.get("max_results", 60)), 200))
    include_minimized = bool(arguments.get("include_minimized", True))
    fg = _backend.get_foreground_hwnd()
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for hwnd, title in _backend.enum_visible_windows(require_title=True):
        if len(out) >= max_results:
            break
        if hwnd in seen:
            continue
        minimized = _backend.is_window_minimized(hwnd)
        if minimized and not include_minimized:
            continue
        bounds = _backend.get_window_bounds(hwnd)
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
    proc_needle = _norm_proc_name(process_name) if process_name else ""
    proc_raw_low = process_name.lower()

    t0 = time.monotonic()
    deadline = t0 + timeout_sec
    while True:
        sync_state()
        fg = _backend.get_foreground_hwnd()
        matches: list[dict[str, Any]] = []
        for hwnd, title in _backend.enum_visible_windows(require_title=True):
            minimized = _backend.is_window_minimized(hwnd)
            if minimized and not include_minimized:
                continue
            bounds = _backend.get_window_bounds(hwnd)
            if not minimized and (bounds["width"] < 20 or bounds["height"] < 20):
                continue
            if title_needle and title_needle not in title.lower():
                continue
            entry = _build_window_entry(hwnd, title, fg)
            if proc_needle:
                proc_low = _norm_proc_name(entry["processName"])
                entry_raw_low = entry["processName"].lower()
                if (
                    proc_low != proc_needle
                    and entry_raw_low != proc_raw_low
                    and not (not proc_raw_low.endswith(".exe") and entry_raw_low == f"{proc_raw_low}.exe")
                ):
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
        f"timeout: No visible window matched title_contains={title_contains!r} "
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
            return error_result(f"Window {hwnd} not found.")
        sys_blk = check_blocked_system_window(info["processName"], info["title"])
        if sys_blk:
            return error_result(sys_blk)
        proc_name = info["processName"]
    if not proc_name:
        return error_result("Provide process_name (e.g. 'TextEdit' or 'notepad.exe') or hwnd.")
    low = _norm_proc_name(proc_name)
    tier = classify_policy(proc_name)
    if tier == "blocked":
        return error_result(f"policy_blocked: '{proc_name}' cannot be granted (hard-blocked security/terminal/agent category).")
    allow_browser_input = bool(arguments.get("allow_browser_input", False))
    mode = "interactive" if (tier == "interactive" or allow_browser_input) else "look_only"
    now_ep = time.time()
    display_proc = (low if (sys.platform == "win32" and "." not in proc_name) else proc_name)
    with _STATE_LOCK:
        STATE["grants"][low] = {
            "processName": display_proc,
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
    if p_low in proc_hints or _norm_proc_name(p_low) in proc_hints:
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

    cmd, proc_hints = _backend.resolve_installed_app(app, classify_policy_fn=classify_policy)
    if cmd is None:
        return error_result(
            f"Could not find installed app '{app}'. Pass an installed desktop app name or explicit path."
        )
    blocked_hit = _is_blocked_launch_target(app, cmd, extra_args)
    if blocked_hit is not None:
        return error_result(f"policy_blocked: '{blocked_hit}' is on the mandatory deny-list.")

    app_key = _norm_app_key(app)

    if reuse_existing and not extra_args:
        for h, _ in _backend.enum_visible_windows(require_title=True):
            inf = inspect_hwnd(h)
            if _window_matches_app_launch(inf, app_key, proc_hints) and (inf["isVisible"] or inf["isMinimized"]):
                assert inf is not None
                if inf["isMinimized"]:
                    _backend.restore_window(h)
                _backend.activate_hwnd(h)
                if auto_grant and inf["policyTier"] != "blocked":
                    cu_grant_app({"process_name": inf["processName"], "allow_browser_input": allow_browser_input})
                    inf = inspect_hwnd(h)
                data = {"launched": app, "reusedExisting": True, "pid": inf["pid"] if inf else 0, "window": inf}
                return tool_result(json.dumps(data, ensure_ascii=False), structured=data)

    before = {h for h, _ in _backend.enum_visible_windows(require_title=False)}
    proc = _backend.spawn_detached_app(cmd, extra_args)

    deadline = time.monotonic() + 5.0
    found_hwnd: int | None = None
    while time.monotonic() < deadline:
        time.sleep(0.08)
        for h, _ in _backend.enum_visible_windows(require_title=True):
            if h not in before:
                inf_new = inspect_hwnd(h)
                if inf_new and inf_new.get("isVisible") and (_window_matches_app_launch(inf_new, app_key, proc_hints) or sys.platform == "win32"):
                    found_hwnd = h
                    break
        if found_hwnd is not None:
            break
        for h, _ in _backend.enum_visible_windows(require_title=True):
            inf = inspect_hwnd(h)
            if inf and inf.get("isVisible") and _window_matches_app_launch(inf, app_key, proc_hints):
                found_hwnd = h
                break
        if found_hwnd is not None:
            break

    win_info = inspect_hwnd(found_hwnd) if found_hwnd is not None else None
    if sys.platform == "darwin" and found_hwnd is not None:
        # Guard against transient macOS splash/open-panel windows that immediately close and get replaced
        time.sleep(0.15)
        win_info = inspect_hwnd(found_hwnd)
        if win_info is None or not win_info.get("isVisible"):
            for h, _ in _backend.enum_visible_windows(require_title=True):
                inf = inspect_hwnd(h)
                if _window_matches_app_launch(inf, app_key, proc_hints):
                    found_hwnd = h
                    win_info = inf
                    break
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
    capture_method = "desktop_capture"

    if hwnd is not None:
        hwnd = int(hwnd)
        info, err = require_window_permission(hwnd, need_input=False)
        if err:
            return error_result(err)
        if info["isMinimized"] and bool(arguments.get("restore_if_minimized", True)):
            _backend.activate_hwnd(hwnd)
        active_popup = _backend.detect_active_popup(hwnd)
        if active_popup and _backend.current_desktop_name().lower() == "default":
            img, origin, cap_m = None, {}, ""
        else:
            img, origin, cap_m = _backend.capture_window(hwnd)
        if img is not None:
            capture_method = cap_m
        else:
            desk, vb, _ = _backend.capture_desktop()
            b = _backend.get_window_bounds(hwnd)
            vis_left = max(vb["left"], b["left"])
            vis_top = max(vb["top"], b["top"])
            vis_right = min(vb["left"] + vb["width"], b["left"] + b["width"])
            vis_bottom = min(vb["top"] + vb["height"], b["top"] + b["height"])
            if vis_right <= vis_left + 4 or vis_bottom <= vis_top + 4:
                return error_result(f"Window {hwnd} has zero visible area ({b}).")
            desk_sx = desk.width / max(1, vb["width"])
            desk_sy = desk.height / max(1, vb["height"])
            cx1 = max(0, int(round((vis_left - vb["left"]) * desk_sx)))
            cy1 = max(0, int(round((vis_top - vb["top"]) * desk_sy)))
            cx2 = min(desk.width, int(round((vis_right - vb["left"]) * desk_sx)))
            cy2 = min(desk.height, int(round((vis_bottom - vb["top"]) * desk_sy)))
            if cx2 <= cx1 + 4 or cy2 <= cy1 + 4:
                return error_result(f"Window {hwnd} has zero visible area ({b}).")
            img = desk.crop((cx1, cy1, cx2, cy2))
            origin = {"left": vis_left, "top": vis_top, "width": vis_right - vis_left, "height": vis_bottom - vis_top}
            capture_method = "popup_composited_capture" if active_popup else "screen_crop_fallback"
        bounds_record = _backend.get_window_bounds(hwnd)
    else:
        if not bool(arguments.get("allow_full_desktop", False)):
            return error_result("Prefer per-window capture by passing hwnd. To capture the entire desktop across all apps, pass allow_full_desktop=true explicitly.")
        img, vb, capture_method = _backend.capture_desktop()
        origin = {"left": vb["left"], "top": vb["top"], "width": vb["width"], "height": vb["height"]}
        bounds_record = vb

    orig_w, orig_h = img.width, img.height
    if img.width > max_width:
        new_h = max(1, round(img.height * max_width / img.width))
        img = img.resize((max_width, new_h), Image.Resampling.BILINEAR)

    # Maps returned observation pixels -> logical screen coordinates (exact on both 1x Windows and 2x macOS Retina)
    logical_w = max(1, int(origin.get("width") or orig_w))
    logical_h = max(1, int(origin.get("height") or orig_h))
    scale_x = logical_w / img.width
    scale_y = logical_h / img.height

    hash_key = f"hwnd:{hwnd}" if hwnd is not None else "desktop"
    dhash_hex, dhash_int = compute_dhash(img)
    prev_hash = _LAST_FRAME_HASHES.get(hash_key)
    hamming = bin(prev_hash ^ dhash_int).count("1") if prev_hash is not None else 64
    changed = prev_hash is None or hamming > 0
    _LAST_FRAME_HASHES[hash_key] = dhash_int

    elements: list[dict[str, Any]] = []
    focused_el: str | None = None
    popup_hwnd = _backend.detect_active_popup(hwnd) if hwnd is not None else 0
    max_el_req = max(10, min(int(arguments.get("max_elements", 60)), 120))
    if annotate and hwnd is not None:
        uia_target = popup_hwnd if popup_hwnd else hwnd
        uia = _backend.run_ui_tree_helper(uia_target, max_elements=max_el_req)
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

    return tool_result(summary_txt, structured=meta, image_b64=b64, image_mime=mime)


def cu_observe(arguments: dict[str, Any]) -> dict[str, Any]:
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
    scale_x = float(obs["scaleX"]) if obs else 1.0
    scale_y = float(obs["scaleY"]) if obs else 1.0
    w_logical = max(16.0, min(float(arguments.get("width", 320)) * scale_x, 1600.0))
    h_logical = max(16.0, min(float(arguments.get("height", 240)) * scale_y, 1200.0))
    upscale = max(1, min(int(arguments.get("upscale", 2)), 3))

    backing_x = 1.0
    backing_y = 1.0
    if hwnd is not None:
        _, err = require_window_permission(int(hwnd), need_input=False)
        if err:
            return error_result(err)
        win_img, origin, _ = _backend.capture_window(int(hwnd))
        if win_img is not None:
            backing_x = win_img.width / max(1, origin["width"])
            backing_y = win_img.height / max(1, origin["height"])
            rx1 = max(0, int(round((sx - origin["left"]) * backing_x)))
            ry1 = max(0, int(round((sy - origin["top"]) * backing_y)))
            rw = max(1, int(round(w_logical * backing_x)))
            rh = max(1, int(round(h_logical * backing_y)))
            rx2 = min(win_img.width, rx1 + rw)
            ry2 = min(win_img.height, ry1 + rh)
            if rx2 <= rx1 or ry2 <= ry1:
                return error_result(f"Zoom region ({sx},{sy} {w_logical:.0f}x{h_logical:.0f}) is outside visible bounds of window {hwnd}.")
            crop = win_img.crop((rx1, ry1, rx2, ry2))
            actual_left = int(round(origin["left"] + rx1 / backing_x))
            actual_top = int(round(origin["top"] + ry1 / backing_y))
        else:
            desk, vb, _ = _backend.capture_desktop()
            backing_x = desk.width / max(1, vb["width"])
            backing_y = desk.height / max(1, vb["height"])
            wb = _backend.get_window_bounds(int(hwnd))
            vis_left = max(vb["left"], wb["left"])
            vis_top = max(vb["top"], wb["top"])
            vis_right = min(vb["left"] + vb["width"], wb["left"] + wb["width"])
            vis_bottom = min(vb["top"] + vb["height"], wb["top"] + wb["height"])
            rx1 = max(0, int(round((max(vis_left, sx) - vb["left"]) * backing_x)))
            ry1 = max(0, int(round((max(vis_top, sy) - vb["top"]) * backing_y)))
            rx2 = min(desk.width, int(round((min(vis_right, sx + w_logical) - vb["left"]) * backing_x)))
            ry2 = min(desk.height, int(round((min(vis_bottom, sy + h_logical) - vb["top"]) * backing_y)))
            if rx2 <= rx1 or ry2 <= ry1:
                return error_result(f"Zoom region ({sx},{sy} {w_logical:.0f}x{h_logical:.0f}) is outside visible bounds of window {hwnd} ({wb}).")
            crop = desk.crop((rx1, ry1, rx2, ry2))
            actual_left = int(round(vb["left"] + rx1 / backing_x))
            actual_top = int(round(vb["top"] + ry1 / backing_y))
    else:
        desk, vb, _ = _backend.capture_desktop()
        backing_x = desk.width / max(1, vb["width"])
        backing_y = desk.height / max(1, vb["height"])
        rx1 = max(0, int(round((sx - vb["left"]) * backing_x)))
        ry1 = max(0, int(round((sy - vb["top"]) * backing_y)))
        rw = max(1, int(round(w_logical * backing_x)))
        rh = max(1, int(round(h_logical * backing_y)))
        rx2 = min(desk.width, rx1 + rw)
        ry2 = min(desk.height, ry1 + rh)
        if rx2 <= rx1 or ry2 <= ry1:
            return error_result(f"Zoom region ({sx},{sy} {w_logical:.0f}x{h_logical:.0f}) is outside desktop bounds.")
        crop = desk.crop((rx1, ry1, rx2, ry2))
        actual_left = int(round(vb["left"] + rx1 / backing_x))
        actual_top = int(round(vb["top"] + ry1 / backing_y))

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
        "scaleX": (orig_cw / backing_x) / crop.width,
        "scaleY": (orig_ch / backing_y) / crop.height,
        "bounds": _backend.get_window_bounds(int(hwnd)) if hwnd is not None else (_backend.get_window_bounds(int(obs["hwnd"])) if (obs and obs.get("hwnd")) else _backend.virtual_bounds()),
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
    uia = _backend.run_ui_tree_helper(hwnd, max_elements=max_elements)
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
            "bounds": _backend.get_window_bounds(hwnd),
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
    ok = _backend.activate_hwnd(hwnd)
    actual = _backend.get_foreground_hwnd()
    if not ok:
        return error_result(f"focus_failed: Requested window {hwnd}, actual foreground window is {actual}.")
    update_overlay_hud(True, "Active", ttl_sec=_HUD_IDLE_TIMEOUT_SEC)
    return tool_result(f"Focused window {hwnd} ({info['processName']}: {info['title']}).", structured={"hwnd": hwnd, "foregroundHwnd": actual})


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
    direct = _backend.click_element_direct(hwnd, obs, match, elem_idx, prefer_invoke=use_uia_invoke)
    if direct is not None:
        update_overlay_hud(True, "Active", click_xy=(cx, cy), ttl_sec=_HUD_IDLE_TIMEOUT_SEC)
        return tool_result(direct["summary"], structured=direct["structured"])

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
    direct = _backend.set_value_direct(
        hwnd, obs, match, elem_idx, value, prefer_background_msg=arguments.get("prefer_background_msg")
    )
    if direct is not None:
        update_overlay_hud(True, "Active", click_xy=(cx, cy), ttl_sec=_HUD_IDLE_TIMEOUT_SEC)
        return tool_result(direct["summary"], structured=direct["structured"])

    click_res = cu_click({"hwnd": hwnd, "x": cx, "y": cy, "coordinate_space": "screen"})
    if click_res.get("isError"):
        return click_res
    time.sleep(0.08)
    _backend.send_key_chord(_backend.SELECT_ALL_MODIFIER, "a", target_hwnd=hwnd)
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
            cur = _backend.get_window_bounds(hwnd)
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
        win_bounds = _backend.get_window_bounds(hwnd)

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

    pre_belongs, pre_root = _backend.verify_point_belongs_to_hwnd(sx, sy, int(hwnd))
    hitting_owned_popup = pre_belongs and pre_root != int(hwnd)
    if not hitting_owned_popup:
        if not _backend.activate_hwnd(int(hwnd)):
            return error_result(f"focus_collision: Could not bring target window {hwnd} ({info['processName']}) to foreground.")

    belongs, hit_root = _backend.verify_point_belongs_to_hwnd(sx, sy, int(hwnd))
    if not belongs:
        return error_result(f"point_occluded: Screen point ({sx}, {sy}) hits window {hit_root}, not target window {hwnd}. Refresh window state.")
    if hit_root != int(hwnd):
        hit_info = inspect_hwnd(hit_root)
        if hit_info is not None:
            if hit_info.get("policyTier") == "blocked":
                return error_result(f"policy_blocked: Point ({sx}, {sy}) hits blocked window '{hit_info['processName']}'.")
            sys_blk = check_blocked_system_window(hit_info["processName"], hit_info["title"])
            if sys_blk:
                return error_result(sys_blk)
            sens_err = check_sensitive_editor_title(hit_info["processName"], hit_info["title"])
            if sens_err:
                return error_result(sens_err)

    global _CURSOR_ENGAGED
    button = str(arguments.get("button", "left"))
    clicks = max(1, min(int(arguments.get("clicks", 1)), 3))
    raw_dur = arguments.get("duration")
    dur = max(0.0, min(float(raw_dur), 1.0)) if raw_dur is not None else None

    _backend.smooth_move_to(
        sx, sy, duration=dur, in_batch=_IN_BATCH_DEPTH > 0,
        on_esc_abort=lambda: update_overlay_hud(False, "Stopped (Esc)", force_flush=True),
    )
    _backend.ensure_child_canvas_focus(sx, sy, int(hwnd), hitting_owned_popup=hitting_owned_popup)
    _backend.send_mouse_click(button=button, clicks=clicks, interval=0.04 if clicks > 1 else 0.0, x=sx, y=sy)
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
    _backend.smooth_move_to(
        sx, sy, duration=duration, in_batch=_IN_BATCH_DEPTH > 0,
        on_esc_abort=lambda: update_overlay_hud(False, "Stopped (Esc)", force_flush=True),
    )
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
    if not _backend.activate_hwnd(int(hwnd)):
        return error_result(f"focus_collision: Could not focus window {hwnd}.")

    stride = max(1, (len(screen_pts) - 1) // 5)
    check_indices = sorted({0, len(screen_pts) - 1, *range(0, len(screen_pts), stride)})
    for ci in check_indices:
        px, py = screen_pts[ci]
        belongs, hit_root = _backend.verify_point_belongs_to_hwnd(px, py, int(hwnd))
        if not belongs:
            return error_result(
                f"point_occluded: Drag waypoint[{ci}] ({px}, {py}) hits window {hit_root}, not target window {hwnd}."
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

    cur_x, cur_y = _backend.get_cursor_pos()
    pre_dist = math.hypot(sx0 - cur_x, sy0 - cur_y)
    if pre_dist >= 2.0:
        pre_dur = min(0.026, max(0.010, 0.010 + (min(pre_dist, 1200.0) / 1200.0) * 0.016))
        _backend.smooth_move_to(
            sx0, sy0, duration=pre_dur, in_batch=_IN_BATCH_DEPTH > 0,
            on_esc_abort=lambda: update_overlay_hud(False, "Stopped (Esc)", force_flush=True),
        )
    else:
        _backend.send_mouse_move_abs(sx0, sy0)

    check_emergency_stop(check_corner=False)
    _backend.ensure_child_canvas_focus(sx0, sy0, int(hwnd))
    drag_seq = [(sx0, sy0), *samples]
    _backend.send_mouse_drag(
        drag_seq, button=button, duration=duration, hold_ms=hold_ms,
        on_esc_abort=lambda: _revoke_all_on_emergency("Idle"),
    )

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
    if not _backend.activate_hwnd(int(hwnd)):
        return error_result(f"focus_collision: Could not focus window {hwnd}.")
    belongs, hit_root = _backend.verify_point_belongs_to_hwnd(sx, sy, int(hwnd))
    if not belongs:
        return error_result(f"point_occluded: Scroll point ({sx}, {sy}) hits window {hit_root}, not target window {hwnd}.")
    global _CURSOR_ENGAGED
    clicks = max(-50, min(int(arguments.get("clicks", -3)), 50))
    horizontal = bool(arguments.get("horizontal", False))
    _backend.smooth_move_to(
        sx, sy, duration=0.04, in_batch=_IN_BATCH_DEPTH > 0,
        on_esc_abort=lambda: update_overlay_hud(False, "Stopped (Esc)", force_flush=True),
    )
    _backend.send_mouse_wheel(clicks, horizontal=horizontal, x=sx, y=sy)
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
        _backend.activate_hwnd(hwnd)
    fg = _backend.get_foreground_hwnd()
    if not _backend.is_foreground_for_window(hwnd, fg):
        return error_result(f"focus_collision: Expected window {hwnd} in foreground before typing, but foreground is window {fg}. Focus or click target field first.")

    update_overlay_hud(True, "Active", ttl_sec=_HUD_IDLE_TIMEOUT_SEC)
    if bool(arguments.get("clear_first", False)):
        if not _backend.is_foreground_for_window(hwnd, _backend.get_foreground_hwnd()):
            return error_result(f"focus_collision: Window {hwnd} lost focus before clear_first.")
        _backend.send_key_chord(_backend.SELECT_ALL_MODIFIER, "a", target_hwnd=hwnd)
        time.sleep(0.02)
        _backend.send_key_chord("backspace", target_hwnd=hwnd)

    interval = max(0.0, min(float(arguments.get("interval", 0.004)), 0.25))
    typed = 0
    if interval <= 0.008:
        chunk_size = 20
        for start in range(0, len(text), chunk_size):
            check_emergency_stop()
            if not _backend.is_foreground_for_window(hwnd, _backend.get_foreground_hwnd()):
                return error_result(f"focus_collision: Focus moved away from window {hwnd} after {typed} characters; halted immediately.")
            part = text[start : start + chunk_size]
            _backend.send_unicode_chunk(part)
            typed += len(part)
            if interval > 0:
                time.sleep(interval)
    else:
        for ch in text:
            check_emergency_stop()
            if not _backend.is_foreground_for_window(hwnd, _backend.get_foreground_hwnd()):
                return error_result(f"focus_collision: Focus moved away from window {hwnd} after {typed} characters; halted immediately.")
            _backend.send_unicode_char(ch)
            typed += 1
            time.sleep(interval)

    if bool(arguments.get("press_enter", False)):
        check_emergency_stop()
        if not _backend.is_foreground_for_window(hwnd, _backend.get_foreground_hwnd()):
            return error_result(f"focus_collision: Window {hwnd} lost focus before press_enter.")
        _backend.send_key_chord("enter", target_hwnd=hwnd)

    update_overlay_hud(True, "Active", ttl_sec=_HUD_IDLE_TIMEOUT_SEC)
    return tool_result(
        f"Typed {typed} Unicode character(s) into {info['processName']} (window {hwnd}).",
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
        "option": "opt", "alt": "opt", "altleft": "opt", "altright": "opt",
        "command": "cmd", "meta": "cmd", "super": "cmd",
        "spacebar": "space",
        "escape": "esc",
    }
    blocked_single = BLOCKED_KEYS_DARWIN if sys.platform == "darwin" else BLOCKED_KEYS_WIN32
    for k in keys:
        if k.lower() in blocked_single:
            return error_result(f"policy_blocked: System/Super key chord '{k}' is forbidden.")
    canon_set = {_KEY_ALIAS_CANON.get(k.lower(), k.lower()) for k in keys}
    if canon_set in ({"ctrl", "esc"}, {"ctrl", "shift", "esc"}):
        return error_result(f"policy_blocked: System chord '{'+'.join(k.lower() for k in keys)}' is forbidden (Start menu / Task Manager).")
    if sys.platform == "darwin" and canon_set in ({"cmd", "space"}, {"cmd", "opt", "esc"}, {"cmd", "ctrl", "q"}):
        return error_result(f"policy_blocked: System chord '{'+'.join(k.lower() for k in keys)}' is forbidden (Spotlight / Force Quit / Lock Screen).")

    info, err = require_window_permission(hwnd, need_input=True)
    if err:
        return error_result(err)
    if bool(arguments.get("activate_first", True)):
        _backend.activate_hwnd(hwnd)
    fg = _backend.get_foreground_hwnd()
    if not _backend.is_foreground_for_window(hwnd, fg) and not (fg == 0 and _backend.current_desktop_name().lower() != "default"):
        return error_result(f"focus_collision: Expected window {hwnd} in foreground before keys, but foreground is window {fg}.")
    if check_sensitive_editor_title(info["processName"], info["title"]):
        return error_result("sensitive_file_guard: Refused key chord in protected file editor.")
    norm = [k.lower() for k in keys]
    update_overlay_hud(True, "Active", ttl_sec=_HUD_IDLE_TIMEOUT_SEC)
    if not (_IS_LOW_INTEGRITY or (fg == 0 and _backend.current_desktop_name().lower() != "default")):
        _backend.send_key_chord(*norm, target_hwnd=hwnd)
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
        active, label, click_xy=click_xy, cursor_xy=cursor_xy,
        cursor_style=str(cursor_style) if cursor_style else None,
        ttl_sec=ttl_sec, allow_capture=allow_capture, force_flush=True,
    )
    state_txt = "shown" if active else "hidden"
    return tool_result(
        f"Screen-border glow & status pill HUD {state_txt} (style={info.get('cursorStyle')}, ttl={info['ttlSec']}s).",
        structured=info,
    )


def cu_choose(arguments: dict[str, Any]) -> dict[str, Any]:
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
            return error_result(f"observation_id '{obs_id}' not found or carries no UI elements.")
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

            fg_hw = _backend.get_foreground_hwnd()
            if fg_hw > 0 and fg_hw != current_hwnd:
                _, fg_err = require_window_permission(fg_hw, need_input=True)
                if fg_err is None:
                    current_hwnd = fg_hw

            uia = _backend.run_ui_tree_helper(current_hwnd, max_elements=110)
            elems = uia.get("elements") or []
            if isinstance(elems, dict):
                elems = [elems]

            obs_id = f"obs_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
            _store_observation(
                obs_id,
                {
                    "id": obs_id, "timestamp": time.time(), "kind": "ui_tree",
                    "hwnd": current_hwnd, "originX": 0, "originY": 0,
                    "scaleX": 1.0, "scaleY": 1.0,
                    "bounds": _backend.get_window_bounds(current_hwnd),
                    "elements": elems, "focusedElement": uia.get("focusedElement"),
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
                    "step": step_no, "goal": step_goal, "status": "hold",
                    "hwnd": current_hwnd, "decision": decision, "observationId": last_obs_id,
                })
                break

            idx = int(decision["elementIndex"])
            click_res = cu_click_element({
                "observation_id": last_obs_id, "element_index": idx, "prefer_uia_invoke": prefer_uia,
            })
            if click_res.get("isError"):
                return click_res
            clicked_el = next((e for e in elems if int(e["index"]) == idx), {})
            trace.append({
                "step": step_no, "goal": step_goal, "status": "clicked",
                "hwnd": current_hwnd, "elementIndex": idx, "elementName": clicked_el.get("name"),
                "decision": decision, "observationId": last_obs_id,
            })
            if step_no + 1 < max_steps:
                time.sleep(0.008)

        verification: dict[str, Any] | None = None
        if verify_contains and final_status != "hold":
            time.sleep(0.025)
            v_uia = _backend.run_ui_tree_helper(current_hwnd, max_elements=110)
            v_elems = v_uia.get("elements") or []
            if isinstance(v_elems, dict):
                v_elems = [v_elems]
            v_obs_id = f"obs_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
            _store_observation(
                v_obs_id,
                {
                    "id": v_obs_id, "timestamp": time.time(), "kind": "ui_tree",
                    "hwnd": current_hwnd, "originX": 0, "originY": 0,
                    "scaleX": 1.0, "scaleY": 1.0,
                    "bounds": _backend.get_window_bounds(current_hwnd),
                    "elements": v_elems, "focusedElement": v_uia.get("focusedElement"),
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
        "status": final_status, "hwnd": current_hwnd,
        "goal": raw_goal or " -> ".join(sub_goals), "decomposedGoals": sub_goals,
        "stepsExecuted": len(trace), "lastObservationId": last_obs_id,
        "elapsedMs": elapsed_ms, "trace": trace,
    }
    if verification is not None:
        out["verification"] = verification
    v_suffix = f", verified={verification['matched']} ({verification.get('matchedText')!r})" if verification else ""
    msg = f"Local step pipeline -> status={final_status} ({len(trace)}/{len(sub_goals)} step(s) in {elapsed_ms}ms{v_suffix}, obs={last_obs_id})."
    return tool_result(msg, structured=out)


def cu_clipboard_read(arguments: dict[str, Any]) -> dict[str, Any]:
    check_emergency_stop()
    if _backend.is_desktop_locked():
        return error_result("desktop_locked: Desktop is locked.")
    if not bool(arguments.get("allow_clipboard", False)):
        return error_result("clipboard_permission_required: Pass allow_clipboard=true explicitly to read Unicode text from the clipboard.")
    max_chars = max(1, min(int(arguments.get("max_chars", 16000)), 64000))
    data, err = _backend.clipboard_read(max_chars)
    if err or data is None:
        return error_result(err or "clipboard_read_failed")
    return tool_result(json.dumps(data, ensure_ascii=False), structured=data)


def cu_clipboard_write(arguments: dict[str, Any]) -> dict[str, Any]:
    check_emergency_stop()
    if _backend.is_desktop_locked():
        return error_result("desktop_locked: Desktop is locked.")
    if "text" not in arguments or arguments["text"] is None:
        return error_result("Provide 'text' string to write to the clipboard.")
    text = str(arguments["text"])
    if len(text) > 16000:
        return error_result("text_too_long: Max 16000 characters per cu_clipboard_write call.")
    data, err = _backend.clipboard_write(text)
    if err or data is None:
        return error_result(err or "clipboard_set_failed")
    with _STATE_LOCK:
        has_grants = bool(STATE.get("grants"))
    if has_grants:
        update_overlay_hud(True, "Active", ttl_sec=_HUD_IDLE_TIMEOUT_SEC)
    return tool_result(f"Wrote {len(text)} Unicode character(s) to clipboard.", structured=data)


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
    _backend.enter_1ms_timer()
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
                fg = _backend.get_foreground_hwnd()
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
                    "status", "wait_for_window", "list_windows", "launch_app",
                    "revoke_app", "overlay", "clipboard_read", "clipboard_write", "choose",
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
            entry: dict[str, Any] = {"step": idx, "action": act, "ok": not is_err, "hwnd": active_hwnd}
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
        _backend.exit_1ms_timer()
        _IN_BATCH_DEPTH = max(0, _IN_BATCH_DEPTH - 1)
        flush_overlay_hud()

    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)
    data: dict[str, Any] = {"completedSteps": len(step_summaries), "elapsedMs": elapsed_ms, "steps": step_summaries}
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
                    summary_lines.append(f"  [{i}..{j - 1}] {j - i}x input ({breakdown}) in window {s.get('hwnd')}: all OK")
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
            "\n".join(summary_lines), structured=data,
            image_b64=last_image_block["data"], image_mime=last_image_block.get("mimeType", "image/png"),
        )
    return tool_result("\n".join(summary_lines), structured=data)


READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
SAFE_MUTATION = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
INTERACTIVE = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "cu_status",
        "title": "Computer Use status",
        "description": "Return desktop bounds, DPI/Retina mode, lock status, macOS TCC permission status (on macOS), pointer coordinates, foreground window, and session app grants.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": READ_ONLY,
    },
    {
        "name": "cu_list_windows",
        "title": "List open windows",
        "description": "Enumerate visible top-level desktop windows with window handle (HWND/CGWindowID), PID, process name, shadow-free bounds, elevation, policy tier, and grant state.",
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
        "description": "Poll visible top-level windows up to timeout_sec (0.5..15s) until title_contains and/or process_name matches.",
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
        "description": "Universally launch or reuse any installed macOS (.app) or Windows (.exe/StartApps) desktop app by friendly name or explicit path, optionally granting interactive browser input in 1 call.",
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
        "description": "Capture a single granted window (shadow-free PrintWindow on Windows or Retina screencapture on macOS) or full desktop (allow_full_desktop=true). Supports fast 'webp'/'jpeg'/'png' encoding, frame-diff hashing, and Set-of-Mark UI badges (annotate=true).",
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
        "title": "Hybrid Set-of-Mark screenshot + Accessibility tree",
        "description": "One-shot fast observation: captures a compact WebP window screenshot overlaid with Set-of-Mark [0..N] element badges AND returns the native UI Automation / Accessibility control tree + frame-diff status under a single observation_id.",
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
        "title": "Inspect UI Automation / Accessibility tree",
        "description": "Walk native Accessibility (macOS AXUIElement) or UI Automation (Windows) controls for a granted window, returning focused element state and numbered interactive elements [0..N] for cu_click_element / cu_set_value.",
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
        "description": "Bring an exact granted window to the foreground.",
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
        "title": "Click UI element by index",
        "description": "Click or invoke an indexed accessibility element from the latest cu_observe or cu_ui_tree observation_id.",
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
        "title": "Set editable element value",
        "description": "Replace text in an editable control from cu_observe or cu_ui_tree via native AXValue/ValuePattern (with guarded click + select-all + Unicode typing fallback).",
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
        "description": "Click at (x, y) using observation_id pixel coordinates (auto-scaled for Retina/DPI) or screen/window coordinates, with occlusion, same-PID popup preservation, and foreground checks.",
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
        "description": "Type Unicode text via native OS events into a focused, granted window with live focus-collision and sensitive-editor guards, plus optional clear_first and press_enter.",
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
        "description": "Send a key or hotkey chord (e.g. 'cmd+s' on macOS, 'ctrl+s' on Windows, 'enter') to a granted window. Dangerous OS chords are blocked.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "hwnd": {"type": "integer"},
                "keys": {"type": "string", "description": "Single key or '+'-separated hotkey chord (e.g. 'cmd+s', 'ctrl+s', 'enter')."},
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
        "description": "Read Unicode text from the OS clipboard when allow_clipboard=true is passed.",
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
        "description": "Write Unicode text (up to 16000 characters) onto the OS clipboard.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "maxLength": 16000}},
            "required": ["text"],
            "additionalProperties": False,
        },
        "annotations": INTERACTIVE,
    },
    {
        "name": "cu_overlay",
        "title": "Control screen-border glow & status pill HUD",
        "description": "Show, update, or dismiss the pitch-black top status pill, screen-border glow, and click ripple HUD ('sleek_dart', 'obsidian_badge', 'frost_badge', or 'pure_brand'), excluded from screenshots.",
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
        "description": "Fast local matcher: picks e<index>, done, or unsure from UI elements (or observation_id) in under 1ms using Unicode/color/symbol matching with optional local LLM fallback.",
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
        "description": "One-call local UI automation pipeline (1..24 steps): accepts a single 'goal', a compound sequence ('Click A -> B -> C' or arithmetic expression), or an explicit 'goals' array, snapshots UI before each step, clicks when confidence >= confidence_floor (0.80), follows granted popups, and optionally verifies final UI text via verify_contains.",
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
                "instructions": "Use cu_batch for multi-step macOS and Windows desktop workflows (up to 80 steps including launch_app, step, click, drag, type, key, observe, and revoke_app) in a single turn. Confirm consequential actions with the user and call revoke_app when finished.",
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


_EXIT_CLEANED = False


def _cleanup_on_exit() -> None:
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
    _backend.restore_os_cursors_safe()
    _backend.close_helpers()


def _start_lifecycle_watchdog() -> threading.Event:
    stop_ev = threading.Event()
    parent_exited_fn, close_parent_fn = _backend.make_parent_exit_checker()

    def _watch() -> None:
        try:
            while not stop_ev.wait(2.0):
                if parent_exited_fn():
                    _cleanup_on_exit()
                    os._exit(0)

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

                if _IN_FLIGHT_TOOLS == 0:
                    prune_expired_grants(save_if_changed=True)
        finally:
            close_parent_fn()

    threading.Thread(target=_watch, name="cua-lifecycle-watchdog", daemon=True).start()
    return stop_ev


def serve_stdio() -> int:
    global _STDIO_MODE
    _STDIO_MODE = True
    _backend.restore_os_cursors_safe()
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
                            _backend.kill_ui_bridge()
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
                    _backend.kill_ui_bridge()
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
        _cleanup_on_exit()
    return 0


def run_cli_call(tool_name: str, raw_args: str) -> int:
    args = json.loads(raw_args) if raw_args.strip() else {}
    res = dispatch_tool(tool_name, args)
    slim_content = [c for c in res.get("content", []) if c.get("type") != "image"]
    out = {"isError": res.get("isError", False), "content": slim_content, "structuredContent": res.get("structuredContent")}
    print(json.dumps(out, ensure_ascii=True, indent=2))
    return 1 if out["isError"] else 0


def run_self_test() -> int:
    status = cu_status({})["structuredContent"]
    wins = cu_list_windows({"max_results": 15})["structuredContent"]
    blocked_target = "Terminal" if sys.platform == "darwin" else "powershell.exe"
    blocked_check = cu_grant_app({"process_name": blocked_target})
    editor_target = ("TextEdit", "mcp_config.json - TextEdit") if sys.platform == "darwin" else ("notepad.exe", "mcp_config.json - Notepad")
    sens_check = check_sensitive_editor_title(*editor_target)
    summary: dict[str, Any] = {
        "server": f"{SERVER_NAME}@{SERVER_VERSION}",
        "platform": sys.platform,
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
    if sys.platform == "darwin":
        summary["accessibilityTrusted"] = status.get("accessibilityTrusted")
        summary["screenRecordingGranted"] = status.get("screenRecordingGranted")
        if status.get("tccSetupHint"):
            summary["tccSetupHint"] = status["tccSetupHint"]
    print(json.dumps(summary, ensure_ascii=True, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Computer Use MCP Server & CLI for macOS and Windows")
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
