"""Native macOS (darwin) platform backend for Antigravity Computer Use v3.2.0.

Zero third-party OS wrappers (no pyobjc). Talks natively to macOS Accessibility
(AXUIElement), CoreGraphics (CGWindowList, CGEvent 120Hz input), and AppKit via
on-demand compiled Swift helpers (`macos_ax_bridge.swift`, `overlay_hud_mac.swift`)
verified by SHA-256, with JXA (`osascript`) and `/usr/sbin/screencapture` fallback.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import plistlib
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from typing import Any, Callable

from PIL import Image, ImageFont

import antigravity_brand

SCRIPT_DIR = Path(__file__).resolve().parent
AX_SWIFT_FILE = SCRIPT_DIR / "macos_ax_bridge.swift"
HUD_SWIFT_FILE = SCRIPT_DIR / "overlay_hud_mac.swift"

IS_LOW_INTEGRITY = False
DPI_MODE = "RetinaLogicalPoints"
SELECT_ALL_MODIFIER = "cmd"

_RUNTIME_DIR: Path = SCRIPT_DIR.parent / ".runtime"
_OVERLAY_PID_FILE: Path = _RUNTIME_DIR / "overlay_pid.json"


def configure(runtime_dir: Path, overlay_pid_file: Path) -> None:
    global _RUNTIME_DIR, _OVERLAY_PID_FILE
    _RUNTIME_DIR = runtime_dir
    _OVERLAY_PID_FILE = overlay_pid_file


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


_COMPILE_LOCK = threading.Lock()
_BIN_CACHE: dict[str, Path | None] = {}


def _ensure_compiled_swift(src_file: Path, bin_name: str, timeout_sec: float = 45.0) -> Path | None:
    with _COMPILE_LOCK:
        if bin_name in _BIN_CACHE:
            cached = _BIN_CACHE[bin_name]
            if cached is None or cached.is_file():
                return cached
        if not src_file.is_file():
            _BIN_CACHE[bin_name] = None
            return None
        bin_dir = _RUNTIME_DIR / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        bin_path = bin_dir / bin_name
        sha_path = bin_dir / f"{bin_name}.sha256"
        src_sha = _file_sha256(src_file)
        if bin_path.is_file() and sha_path.is_file():
            try:
                recorded = sha_path.read_text(encoding="utf-8").strip()
                if recorded == f"{src_sha}:{_file_sha256(bin_path)}":
                    _BIN_CACHE[bin_name] = bin_path
                    return bin_path
            except OSError:
                pass

        xcrun = shutil.which("xcrun") or "/usr/bin/xcrun"
        swiftc = shutil.which("swiftc") or "/usr/bin/swiftc"
        cmd: list[str]
        if Path(xcrun).is_file():
            cmd = [xcrun, "swiftc", "-O", str(src_file), "-o", str(bin_path)]
        elif Path(swiftc).is_file():
            cmd = [swiftc, "-O", str(src_file), "-o", str(bin_path)]
        else:
            _BIN_CACHE[bin_name] = None
            return None

        tmp_bin = bin_dir / f".{bin_name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp"
        cmd[-1] = str(tmp_bin)
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
            if res.returncode == 0 and tmp_bin.is_file():
                os.chmod(tmp_bin, 0o755)
                tmp_bin.replace(bin_path)
                try:
                    sha_path.write_text(f"{src_sha}:{_file_sha256(bin_path)}\n", encoding="utf-8")
                except OSError:
                    pass
                _BIN_CACHE[bin_name] = bin_path
                return bin_path
        except Exception:
            pass
        finally:
            try:
                tmp_bin.unlink(missing_ok=True)
            except OSError:
                pass
        _BIN_CACHE[bin_name] = None
        return None


def ensure_ax_bridge_binary() -> Path | None:
    return _ensure_compiled_swift(AX_SWIFT_FILE, "macos_ax_bridge")


def ensure_overlay_hud_binary() -> Path | None:
    return _ensure_compiled_swift(HUD_SWIFT_FILE, "overlay_hud_mac")


# Persistent stdio JSON-lines bridge daemon
_BRIDGE_LOCK = threading.Lock()
_BRIDGE_PROC: subprocess.Popen[str] | None = None
_BRIDGE_OUT_Q: queue.Queue[str] | None = None


def _start_reader(proc: subprocess.Popen[str]) -> queue.Queue[str]:
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

    threading.Thread(target=_pump, name="cua-mac-bridge-reader", daemon=True).start()
    return q


def _drain_line(q: queue.Queue[str], timeout_sec: float) -> str:
    deadline = time.monotonic() + timeout_sec
    while True:
        rem = deadline - time.monotonic()
        if rem <= 0:
            raise TimeoutError("macOS AX bridge read timed out")
        line = q.get(timeout=rem)
        if not line:
            return ""
        s = line.strip()
        if s.startswith("{"):
            return s


def kill_ui_bridge() -> None:
    global _BRIDGE_PROC, _BRIDGE_OUT_Q
    if _BRIDGE_PROC is not None:
        try:
            _BRIDGE_PROC.kill()
        except Exception:
            pass
    _BRIDGE_PROC = None
    _BRIDGE_OUT_Q = None


def close_helpers() -> None:
    kill_ui_bridge()


def _get_bridge() -> subprocess.Popen[str] | None:
    global _BRIDGE_PROC, _BRIDGE_OUT_Q
    if _BRIDGE_PROC is not None and _BRIDGE_PROC.poll() is None and _BRIDGE_OUT_Q is not None:
        return _BRIDGE_PROC
    bin_path = ensure_ax_bridge_binary()
    if bin_path is None:
        return None
    proc: subprocess.Popen[str] | None = None
    try:
        proc = subprocess.Popen(
            [str(bin_path), "--parent-pid", str(os.getpid())],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
        )
        assert proc.stdin is not None and proc.stdout is not None
        q = _start_reader(proc)
        pong = _drain_line(q, 2.5)
        if '"pong":true' in pong:
            _BRIDGE_PROC = proc
            _BRIDGE_OUT_Q = q
            return _BRIDGE_PROC
        proc.kill()
    except Exception:
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
    _BRIDGE_PROC = None
    _BRIDGE_OUT_Q = None
    return None


def _active_hud_pid() -> int:
    try:
        if _OVERLAY_PID_FILE.is_file():
            data = json.loads(_OVERLAY_PID_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and str(data.get("status") or "").lower() == "running":
                pid = int(data.get("pid") or 0)
                if pid > 0 and pid_is_alive(pid):
                    return pid
    except Exception:
        pass
    return 0


def _call_bridge(payload: dict[str, Any], *, timeout_sec: float = 3.0) -> dict[str, Any] | None:
    global _BRIDGE_PROC, _BRIDGE_OUT_Q
    req = dict(payload)
    if "hudPid" not in req:
        req["hudPid"] = _active_hud_pid()
    with _BRIDGE_LOCK:
        proc = _get_bridge()
        if proc is not None and proc.stdin is not None and _BRIDGE_OUT_Q is not None:
            try:
                while not _BRIDGE_OUT_Q.empty():
                    _BRIDGE_OUT_Q.get_nowait()
                proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
                proc.stdin.flush()
                raw = _drain_line(_BRIDGE_OUT_Q, timeout_sec)
                if raw:
                    return json.loads(raw)
            except Exception:
                kill_ui_bridge()
    return None


# Lightweight JXA fallback when Command Line Tools / swiftc are unavailable
def _run_jxa(script: str, timeout_sec: float = 3.0) -> str:
    try:
        res = subprocess.run(
            ["/usr/bin/osascript", "-l", "JavaScript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        if res.returncode == 0:
            return res.stdout.strip()
    except Exception:
        pass
    return ""


def _jxa_list_windows() -> list[dict[str, Any]]:
    js = r'''
    ObjC.import('CoreGraphics');
    ObjC.import('AppKit');
    const out = [];
    const arr = ObjC.unwrap($.CGWindowListCopyWindowInfo($.kCGWindowListOptionOnScreenOnly | $.kCGWindowListExcludeDesktopElements, 0));
    const front = $.NSWorkspace.sharedWorkspace.frontmostApplication;
    const fpid = front ? front.processIdentifier : 0;
    for (let i = 0; i < arr.length; i++) {
      const d = ObjC.deepUnwrap(arr[i]);
      if ((d.kCGWindowLayer || 0) !== 0) continue;
      const b = d.kCGWindowBounds || {};
      if ((b.Width || 0) < 20 || (b.Height || 0) < 20) continue;
      const owner = String(d.kCGWindowOwnerName || '');
      if (['Window Server','Dock','Control Center','Notification Center','Wallpaper'].indexOf(owner) >= 0) continue;
      const title = String(d.kCGWindowName || owner);
      out.push({
        hwnd: Number(d.kCGWindowNumber || 0),
        pid: Number(d.kCGWindowOwnerPID || 0),
        processName: owner,
        exePath: '',
        title: title,
        bounds: {left: Math.round(b.X||0), top: Math.round(b.Y||0), right: Math.round((b.X||0)+(b.Width||0)), bottom: Math.round((b.Y||0)+(b.Height||0)), width: Math.round(b.Width||0), height: Math.round(b.Height||0)},
        foreground: Number(d.kCGWindowOwnerPID || 0) === fpid,
        minimized: false,
        visible: true,
        layer: 0,
        backingScale: 2.0
      });
    }
    JSON.stringify(out);
    '''
    raw = _run_jxa(js, timeout_sec=3.0)
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
    return []


# Cached short-lived window inspection map to avoid repeated IPC storms inside a single call
_WIN_SNAP_LOCK = threading.Lock()
_WIN_SNAP_CACHE: dict[int, tuple[float, dict[str, Any]]] = {}


def _inspect_raw(hwnd: int, *, max_age_sec: float = 0.08) -> dict[str, Any] | None:
    if hwnd <= 0:
        return None
    now = time.monotonic()
    with _WIN_SNAP_LOCK:
        hit = _WIN_SNAP_CACHE.get(hwnd)
        if hit is not None and (now - hit[0]) <= max_age_sec:
            return hit[1]
    resp = _call_bridge({"cmd": "inspect_window", "hwnd": int(hwnd)}, timeout_sec=2.0)
    if resp is not None:
        if resp.get("found") and isinstance(resp.get("window"), dict):
            win = resp["window"]
            with _WIN_SNAP_LOCK:
                if len(_WIN_SNAP_CACHE) > 256:
                    _WIN_SNAP_CACHE.clear()
                _WIN_SNAP_CACHE[hwnd] = (now, win)
            return win
        return None
    # JXA fallback
    for w in _jxa_list_windows():
        if int(w.get("hwnd") or 0) == int(hwnd):
            return w
    return None


def invalidate_window_cache(hwnd: int | None = None) -> None:
    with _WIN_SNAP_LOCK:
        if hwnd is None:
            _WIN_SNAP_CACHE.clear()
        else:
            _WIN_SNAP_CACHE.pop(int(hwnd), None)


def _status_raw() -> dict[str, Any]:
    resp = _call_bridge({"cmd": "status"}, timeout_sec=2.0)
    if isinstance(resp, dict) and "desktopName" in resp:
        return resp
    return {
        "accessibilityTrusted": False,
        "screenRecordingGranted": False,
        "desktopName": "Default",
        "desktopLocked": False,
        "cursor": {"x": 0, "y": 0},
        "virtualDesktop": {"left": 0, "top": 0, "width": 1440, "height": 900},
        "monitors": [[0.0, 0.0, 1440.0, 900.0, 2.0]],
        "escapePressed": False,
        "foregroundHwnd": 0,
    }


def platform_status_extra() -> dict[str, Any]:
    st = _status_raw()
    ax_ok = bool(st.get("accessibilityTrusted", False))
    sr_ok = bool(st.get("screenRecordingGranted", False))
    extra: dict[str, Any] = {
        "accessibilityTrusted": ax_ok,
        "screenRecordingGranted": sr_ok,
        "swiftBridgeReady": ensure_ax_bridge_binary() is not None,
    }
    missing: list[str] = []
    if not ax_ok:
        missing.append("Accessibility")
    if not sr_ok:
        missing.append("Screen & System Audio Recording")
    if missing:
        extra["tccSetupHint"] = (
            f"Enable {' and '.join(missing)} for Antigravity in "
            "System Settings > Privacy & Security, then restart Antigravity if prompted."
        )
    return extra


def current_desktop_name() -> str:
    return str(_status_raw().get("desktopName") or "Default")


def is_desktop_locked() -> bool:
    return bool(_status_raw().get("desktopLocked", False))


def is_escape_pressed() -> bool:
    resp = _call_bridge({"cmd": "escape_pressed"}, timeout_sec=1.0)
    return bool(resp.get("pressed", False)) if isinstance(resp, dict) else False


def virtual_bounds() -> dict[str, int]:
    vb = _status_raw().get("virtualDesktop")
    if isinstance(vb, dict):
        return {
            "left": int(vb.get("left", 0)),
            "top": int(vb.get("top", 0)),
            "width": max(1, int(vb.get("width", 1440))),
            "height": max(1, int(vb.get("height", 900))),
        }
    return {"left": 0, "top": 0, "width": 1440, "height": 900}


def get_monitor_rects() -> list[tuple[int, int, int, int]]:
    mons = _status_raw().get("monitors") or []
    out: list[tuple[int, int, int, int]] = []
    for m in mons:
        if isinstance(m, (list, tuple)) and len(m) >= 4:
            out.append((int(round(m[0])), int(round(m[1])), int(round(m[2])), int(round(m[3]))))
    if not out:
        vb = virtual_bounds()
        out.append((vb["left"], vb["top"], vb["left"] + vb["width"], vb["top"] + vb["height"]))
    return out


def get_cursor_pos() -> tuple[int, int]:
    resp = _call_bridge({"cmd": "cursor_pos"}, timeout_sec=1.0)
    if isinstance(resp, dict) and "x" in resp and "y" in resp:
        return int(resp["x"]), int(resp["y"])
    return 0, 0


def get_foreground_hwnd() -> int:
    return int(_status_raw().get("foregroundHwnd") or 0)


def pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def query_pid_exe(pid: int) -> str:
    if pid <= 0:
        return ""
    try:
        res = subprocess.run(
            ["/bin/ps", "-p", str(int(pid)), "-o", "comm="],
            capture_output=True,
            text=True,
            timeout=1.0,
        )
        if res.returncode == 0:
            return res.stdout.strip()
    except Exception:
        pass
    return ""


def is_process_elevated(pid: int) -> bool:
    if pid <= 0:
        return False
    is_self = pid == os.getpid()
    try:
        res = subprocess.run(
            ["/bin/ps", "-p", str(int(pid)), "-o", "uid="],
            capture_output=True,
            text=True,
            timeout=1.0,
        )
        if res.returncode == 0 and res.stdout.strip():
            return int(res.stdout.strip()) == 0
    except Exception:
        pass
    return False if is_self else True  # Fail closed for external processes when UID lookup fails


def get_proc_create_time(pid: int) -> int | None:
    if pid <= 0:
        return None
    try:
        res = subprocess.run(
            ["/bin/ps", "-p", str(int(pid)), "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=1.0,
        )
        if res.returncode == 0 and res.stdout.strip():
            return hash(res.stdout.strip())
    except Exception:
        pass
    return None


def restore_os_cursors_safe() -> None:
    pass


def enter_1ms_timer() -> None:
    pass


def exit_1ms_timer() -> None:
    pass


def is_window(hwnd: int) -> bool:
    return _inspect_raw(hwnd, max_age_sec=0.05) is not None


def is_window_visible(hwnd: int) -> bool:
    info = _inspect_raw(hwnd, max_age_sec=0.05)
    return bool(info and info.get("visible", False))


def is_window_minimized(hwnd: int) -> bool:
    info = _inspect_raw(hwnd, max_age_sec=0.05)
    return bool(info and info.get("minimized", False))


def is_window_cloaked(hwnd: int) -> bool:
    return False


def get_window_title_safe(hwnd: int, timeout_ms: int = 120) -> str:
    info = _inspect_raw(hwnd, max_age_sec=0.04)
    return str(info.get("title") or "") if info else ""


def get_window_bounds(hwnd: int) -> dict[str, int]:
    info = _inspect_raw(hwnd, max_age_sec=0.04)
    b = info.get("bounds") if isinstance(info, dict) else None
    if isinstance(b, dict):
        return {
            "left": int(b.get("left", 0)),
            "top": int(b.get("top", 0)),
            "right": int(b.get("right", 0)),
            "bottom": int(b.get("bottom", 0)),
            "width": max(0, int(b.get("width", 0))),
            "height": max(0, int(b.get("height", 0))),
        }
    return {"left": 0, "top": 0, "right": 0, "bottom": 0, "width": 0, "height": 0}


def get_window_pid(hwnd: int) -> int:
    info = _inspect_raw(hwnd, max_age_sec=0.08)
    return int(info.get("pid") or 0) if info else 0


def get_process_info(hwnd: int) -> tuple[int, str, str]:
    info = _inspect_raw(hwnd, max_age_sec=0.08)
    if not info:
        return 0, "unknown", ""
    return int(info.get("pid") or 0), str(info.get("processName") or "unknown"), str(info.get("exePath") or "")


def enum_visible_windows(*, require_title: bool = True, timeout_ms: int = 120) -> list[tuple[int, str]]:
    resp = _call_bridge(
        {"cmd": "list_windows", "includeMinimized": True, "requireTitle": require_title},
        timeout_sec=3.0,
    )
    wins = resp.get("windows") if isinstance(resp, dict) else None
    if not isinstance(wins, list):
        wins = _jxa_list_windows()
    now = time.monotonic()
    out: list[tuple[int, str]] = []
    with _WIN_SNAP_LOCK:
        for w in wins:
            if not isinstance(w, dict):
                continue
            wid = int(w.get("hwnd") or 0)
            if wid <= 0:
                continue
            title = str(w.get("title") or "")
            if require_title and not title:
                continue
            _WIN_SNAP_CACHE[wid] = (now, w)
            out.append((wid, title))
    return out


def detect_active_popup(owner_hwnd: int) -> int:
    resp = _call_bridge({"cmd": "detect_popup", "hwnd": int(owner_hwnd)}, timeout_sec=1.5)
    if isinstance(resp, dict):
        return int(resp.get("popupHwnd") or 0)
    return 0


def activate_hwnd(hwnd: int) -> bool:
    invalidate_window_cache(hwnd)
    resp = _call_bridge({"cmd": "activate_window", "hwnd": int(hwnd)}, timeout_sec=2.5)
    if isinstance(resp, dict):
        return bool(resp.get("ok", False))
    return False


def restore_window(hwnd: int) -> None:
    activate_hwnd(hwnd)


def is_foreground_for_window(hwnd: int, fg_hwnd: int) -> bool:
    if fg_hwnd == int(hwnd):
        return True
    if fg_hwnd <= 0:
        return False
    t_pid = get_window_pid(hwnd)
    f_pid = get_window_pid(fg_hwnd)
    return t_pid > 0 and t_pid == f_pid


def verify_point_belongs_to_hwnd(sx: int, sy: int, target_hwnd: int) -> tuple[bool, int]:
    resp = _call_bridge({"cmd": "hit_test", "x": float(sx), "y": float(sy)}, timeout_sec=1.5)
    if not isinstance(resp, dict):
        return False, 0
    hit_wid = int(resp.get("hwnd") or 0)
    hit_pid = int(resp.get("pid") or 0)
    if hit_wid <= 0:
        return False, 0
    if hit_wid == int(target_hwnd):
        return True, hit_wid
    t_pid = get_window_pid(target_hwnd)
    if t_pid > 0 and hit_pid == t_pid:
        return True, hit_wid
    return False, hit_wid


def ensure_child_canvas_focus(sx: int, sy: int, root_hwnd: int, *, hitting_owned_popup: bool = False) -> None:
    pass


def _image_has_signal(image: Image.Image) -> bool:
    thumb = image.resize((64, 64), Image.Resampling.NEAREST)
    ext = thumb.getextrema()
    if isinstance(ext[0], tuple):
        return any(hi > 0 for _, hi in ext)
    return ext[1] > 0


def capture_window(hwnd: int) -> tuple[Image.Image | None, dict[str, int], str]:
    """Capture an exact shadow-free macOS window via `screencapture -x -o -l <CGWindowID>`."""
    bounds = get_window_bounds(hwnd)
    origin = {
        "left": bounds["left"],
        "top": bounds["top"],
        "width": bounds["width"],
        "height": bounds["height"],
    }
    if bounds["width"] <= 4 or bounds["height"] <= 4:
        return None, origin, ""

    tmp_fd, tmp_path = tempfile.mkstemp(prefix="cua_win_", suffix=".png", dir=str(_RUNTIME_DIR))
    os.close(tmp_fd)
    try:
        res = subprocess.run(
            ["/usr/sbin/screencapture", "-x", "-o", "-l", str(int(hwnd)), tmp_path],
            capture_output=True,
            timeout=4.0,
        )
        if res.returncode != 0 or not Path(tmp_path).is_file() or Path(tmp_path).stat().st_size == 0:
            return None, origin, ""
        with Image.open(tmp_path) as raw_img:
            img = raw_img.convert("RGB")
        if img.width <= 4 or img.height <= 4:
            return None, origin, ""
        if not _image_has_signal(img):
            return None, origin, ""
        return img, origin, "screencapture_window"
    except Exception:
        return None, origin, ""
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except OSError:
            pass


def capture_desktop() -> tuple[Image.Image, dict[str, int], str]:
    """Capture the active display / desktop via `screencapture -x`."""
    vb = virtual_bounds()
    tmp_fd, tmp_path = tempfile.mkstemp(prefix="cua_desk_", suffix=".png", dir=str(_RUNTIME_DIR))
    os.close(tmp_fd)
    try:
        res = subprocess.run(
            ["/usr/sbin/screencapture", "-x", "-R", f"{vb['left']},{vb['top']},{vb['width']},{vb['height']}", tmp_path],
            capture_output=True,
            timeout=5.0,
        )
        if res.returncode != 0 or not Path(tmp_path).is_file() or Path(tmp_path).stat().st_size == 0:
            res = subprocess.run(["/usr/sbin/screencapture", "-x", tmp_path], capture_output=True, timeout=5.0)
        if res.returncode != 0 or not Path(tmp_path).is_file() or Path(tmp_path).stat().st_size == 0:
            raise RuntimeError(
                "screen_recording_permission_required: macOS screencapture failed. "
                "Enable Screen & System Audio Recording for Antigravity in System Settings > Privacy & Security."
            )
        with Image.open(tmp_path) as raw_img:
            img = raw_img.convert("RGB")
        return img, vb, "screencapture_desktop"
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except OSError:
            pass


_SOM_FONT_CACHE: ImageFont.FreeTypeFont | ImageFont.ImageFont | None = None


def get_som_font() -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    global _SOM_FONT_CACHE
    if _SOM_FONT_CACHE is None:
        for cand in (
            "/System/Library/Fonts/SFNS.ttf",
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
            "/Library/Fonts/Arial.ttf",
        ):
            if Path(cand).is_file():
                try:
                    _SOM_FONT_CACHE = ImageFont.truetype(cand, 11)
                    break
                except Exception:
                    continue
        if _SOM_FONT_CACHE is None:
            _SOM_FONT_CACHE = ImageFont.load_default()
    return _SOM_FONT_CACHE


# Hardware CGEvent Input via Swift Bridge
def send_mouse_move_abs(tx: int, ty: int) -> None:
    _call_bridge({"cmd": "mouse_move", "x": float(tx), "y": float(ty), "duration": 0.0, "checkAbort": False}, timeout_sec=1.5)


def smooth_move_to(
    tx: int,
    ty: int,
    duration: float | None = None,
    *,
    in_batch: bool = False,
    check_abort: bool = True,
    on_esc_abort: Callable[[], None] | None = None,
) -> None:
    sx, sy = get_cursor_pos()
    dist = ((tx - sx) ** 2 + (ty - sy) ** 2) ** 0.5
    if duration is None:
        if in_batch:
            dur = max(0.012, min(0.026, 0.012 + (min(dist, 1400.0) / 1400.0) * 0.014))
        else:
            dur = max(0.020, min(0.046, 0.020 + (min(dist, 1400.0) / 1400.0) * 0.026))
    else:
        dur = max(0.0, min(float(duration), 2.0))
    resp = _call_bridge(
        {"cmd": "mouse_move", "x": float(tx), "y": float(ty), "duration": dur, "checkAbort": check_abort},
        timeout_sec=max(2.0, dur + 2.0),
    )
    if isinstance(resp, dict) and resp.get("error"):
        err = str(resp["error"])
        if "emergency_stop" in err and on_esc_abort is not None:
            on_esc_abort()
        raise RuntimeError(err)


def send_mouse_button(button: str = "left", *, down: bool) -> None:
    cx, cy = get_cursor_pos()
    _call_bridge({"cmd": "mouse_button", "x": float(cx), "y": float(cy), "button": button, "down": down}, timeout_sec=1.5)


def send_mouse_click(button: str = "left", clicks: int = 1, interval: float = 0.0, *, x: int | None = None, y: int | None = None) -> None:
    if x is None or y is None:
        x, y = get_cursor_pos()
    resp = _call_bridge(
        {
            "cmd": "mouse_click",
            "x": float(x),
            "y": float(y),
            "button": button,
            "clicks": max(1, int(clicks)),
            "interval": float(interval),
        },
        timeout_sec=2.5,
    )
    if isinstance(resp, dict) and resp.get("error"):
        raise RuntimeError(str(resp["error"]))


def send_mouse_drag(
    samples: list[tuple[int, int]],
    *,
    button: str = "left",
    duration: float = 0.1,
    hold_ms: int = 0,
    on_esc_abort: Callable[[], None] | None = None,
) -> None:
    if len(samples) < 2:
        return
    resp = _call_bridge(
        {
            "cmd": "mouse_drag",
            "samples": [[int(p[0]), int(p[1])] for p in samples],
            "button": button,
            "duration": float(duration),
            "holdMs": int(hold_ms),
        },
        timeout_sec=max(4.0, float(duration) + hold_ms / 1000.0 + 3.0),
    )
    if isinstance(resp, dict) and resp.get("error"):
        err = str(resp["error"])
        if "emergency_stop" in err and on_esc_abort is not None:
            on_esc_abort()
        raise RuntimeError(err)


def send_mouse_wheel(clicks: int, *, horizontal: bool = False, x: int | None = None, y: int | None = None) -> None:
    if x is None or y is None:
        x, y = get_cursor_pos()
    _call_bridge(
        {"cmd": "mouse_scroll", "x": float(x), "y": float(y), "clicks": int(clicks), "horizontal": bool(horizontal)},
        timeout_sec=2.0,
    )


def send_key_chord(*keys: str, target_hwnd: int = 0) -> None:
    resp = _call_bridge({"cmd": "key_chord", "keys": [str(k) for k in keys]}, timeout_sec=2.0)
    if isinstance(resp, dict) and resp.get("error"):
        raise RuntimeError(str(resp["error"]))


def send_unicode_char(ch: str) -> None:
    resp = _call_bridge({"cmd": "type_text", "text": ch, "interval": 0.0}, timeout_sec=2.0)
    if isinstance(resp, dict) and resp.get("error"):
        raise RuntimeError(str(resp["error"]))


def send_unicode_chunk(chunk: str) -> None:
    resp = _call_bridge({"cmd": "type_text", "text": chunk, "interval": 0.001}, timeout_sec=max(3.0, len(chunk) * 0.03 + 2.0))
    if isinstance(resp, dict) and resp.get("error"):
        raise RuntimeError(str(resp["error"]))


# AXUIElement Tree & Element Direct Actions
def run_ui_tree_helper(
    hwnd: int,
    *,
    max_elements: int = 100,
    set_value_index: int = -1,
    value_text: str = "",
    invoke_index: int = -1,
) -> dict[str, Any]:
    resp = _call_bridge(
        {
            "cmd": "ui_tree",
            "hwnd": int(hwnd),
            "maxElements": int(max_elements),
            "setValueIndex": int(set_value_index),
            "valueText": value_text,
            "invokeIndex": int(invoke_index),
        },
        timeout_sec=4.0,
    )
    if resp is None:
        raise RuntimeError(
            "macos_ax_bridge_unavailable: Could not start Swift Accessibility bridge. "
            "Ensure Xcode Command Line Tools (`xcode-select --install`) are installed."
        )
    if "error" in resp:
        raise RuntimeError(str(resp["error"]))
    return resp


def click_element_direct(
    hwnd: int,
    obs: dict[str, Any],
    match: dict[str, Any],
    elem_idx: int,
    *,
    prefer_invoke: bool = False,
) -> dict[str, Any] | None:
    if not prefer_invoke:
        # On macOS, AXPress works reliably for buttons/checkboxes/radios/menu items even without stealing focus
        ct = str(match.get("controlType") or "")
        if ct not in ("Button", "CheckBox", "RadioButton", "MenuItem", "TabItem"):
            return None
    obs_max_el = max(int(obs.get("maxElements") or 100), elem_idx + 10)
    target_hwnd = int(obs.get("activePopupHwnd") or hwnd)
    res = run_ui_tree_helper(target_hwnd, max_elements=obs_max_el, invoke_index=elem_idx)
    if res.get("method") in ("AXPress", "AXConfirm", "AXPick"):
        return {
            "summary": f"Invoked [{elem_idx}] {match['controlType']} {match.get('name')!r} via macOS {res.get('method')}.",
            "structured": res,
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
    obs_max_el = max(int(obs.get("maxElements") or 100), elem_idx + 10)
    target_hwnd = int(obs.get("activePopupHwnd") or hwnd)
    res = run_ui_tree_helper(target_hwnd, max_elements=obs_max_el, set_value_index=elem_idx, value_text=value)
    if res.get("method") == "AXValue":
        return {
            "summary": f"Set value on element [{elem_idx}] via macOS AXValue ({len(value)} chars).",
            "structured": res,
        }
    return None


# Dynamic macOS .app Discovery & Detached Launch
_APP_INDEX_CACHE: tuple[float, dict[str, tuple[Path, tuple[str, ...]]]] = (0.0, {})


def _norm_app_key(s: str) -> str:
    low = (s or "").strip().lower()
    if low.endswith(".app"):
        low = low[:-4]
    elif low.endswith(".exe"):
        low = low[:-4]
    return re.sub(r"[\W_]+", "", low, flags=re.UNICODE)


def _read_app_bundle_hints(app_path: Path) -> tuple[str, ...]:
    hints: list[str] = [app_path.stem.lower()]
    plist_path = app_path / "Contents" / "Info.plist"
    if plist_path.is_file():
        try:
            with plist_path.open("rb") as fh:
                pl = plistlib.load(fh)
            for k in ("CFBundleDisplayName", "CFBundleName", "CFBundleExecutable"):
                v = str(pl.get(k) or "").strip()
                if v and v.lower() not in hints:
                    hints.append(v.lower())
        except Exception:
            pass
    return tuple(hints)


def _build_macos_app_index() -> dict[str, tuple[Path, tuple[str, ...]]]:
    idx: dict[str, tuple[Path, tuple[str, ...]]] = {}

    def _register(app_p: Path) -> None:
        if not app_p.is_dir() or app_p.suffix.lower() != ".app":
            return
        hints = _read_app_bundle_hints(app_p)
        for h in hints:
            k = _norm_app_key(h)
            if k and k not in idx:
                idx[k] = (app_p, hints)

    roots = [
        Path("/Applications"),
        Path.home() / "Applications",
        Path("/System/Applications"),
        Path("/System/Applications/Utilities"),
        Path("/System/Library/CoreServices/Applications"),
    ]
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for item in sorted(root.iterdir()):
                if item.suffix.lower() == ".app":
                    _register(item)
                elif item.is_dir() and not item.name.startswith("."):
                    try:
                        for sub in item.iterdir():
                            if sub.suffix.lower() == ".app":
                                _register(sub)
                    except OSError:
                        pass
        except OSError:
            pass

    # Spotlight index for any additional installed .app bundles
    try:
        res = subprocess.run(
            ["/usr/bin/mdfind", 'kMDItemContentType == "com.apple.application-bundle"'],
            capture_output=True,
            text=True,
            timeout=2.5,
        )
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                p = Path(line.strip())
                if p.suffix.lower() == ".app" and ".Trash" not in p.parts and p.is_dir():
                    _register(p)
    except Exception:
        pass
    return idx


def resolve_installed_app(
    app: str,
    *,
    classify_policy_fn: Callable[[str], str],
) -> tuple[list[str] | None, tuple[str, ...]]:
    global _APP_INDEX_CACHE
    raw = app.strip()
    if not raw:
        return None, ()

    exp = Path(os.path.expanduser(os.path.expandvars(raw)))
    if exp.suffix.lower() == ".app" and exp.is_dir():
        hints = _read_app_bundle_hints(exp)
        return ["/usr/bin/open", "-a", str(exp)], hints
    # Only allow explicit executables located inside a .app bundle (prevents launching arbitrary CLI interpreters/shells)
    if exp.is_file() and os.access(str(exp), os.X_OK):
        for parent in exp.parents:
            if parent.suffix.lower() == ".app" and parent.is_dir():
                hints = _read_app_bundle_hints(parent)
                return ["/usr/bin/open", "-a", str(parent)], hints
        return None, ()

    key = _norm_app_key(raw)
    if not key:
        return None, ()

    now = time.monotonic()
    if not _APP_INDEX_CACHE[1] or (now - _APP_INDEX_CACHE[0]) >= 60.0:
        _APP_INDEX_CACHE = (now, _build_macos_app_index())
    idx = _APP_INDEX_CACHE[1]

    if key in idx:
        app_p, hints = idx[key]
        return ["/usr/bin/open", "-a", str(app_p)], hints

    raw_words = [w for w in re.findall(r"[^\W_]+", raw.lower(), flags=re.UNICODE) if w]
    best: tuple[Path, tuple[str, ...]] | None = None
    best_sc = 0
    for k, entry in idx.items():
        if len(k) < 3:
            continue
        if k.startswith(key) or key.startswith(k):
            sc = 100 - abs(len(k) - len(key))
        elif len(raw_words) >= 2 and all(w in k for w in raw_words):
            sc = 75 - abs(len(k) - len(key))
        elif key in k:
            sc = 60 - abs(len(k) - len(key))
        else:
            continue
        if sc > best_sc:
            best_sc = sc
            best = entry
    if best is not None:
        return ["/usr/bin/open", "-a", str(best[0])], best[1]

    return None, ()


_VERSIONED_RUNTIME_RE = re.compile(r"^(?:python|perl|ruby|php|lua|node|bash|zsh|sh|fish|tclsh|wish)[\d._-]*$", re.I)


def _is_blocked_runtime_name(token: str, blocked_runtimes: set[str], blocked_aumid_fragments: tuple[str, ...]) -> bool:
    low = (token or "").strip(" \"'").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lower()
    if low.endswith(".app"):
        low = low[:-4]
    if low in blocked_runtimes or bool(_VERSIONED_RUNTIME_RE.match(low)):
        return True
    # Catch bundle IDs (e.g. com.apple.Terminal, com.googlecode.iterm2, com.agilebits.onepassword7)
    if "." in low:
        for frag in blocked_aumid_fragments:
            if frag in low:
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
    if classify_policy_fn(raw_app) == "blocked" or _is_blocked_runtime_name(raw_app, blocked_runtimes, blocked_aumid_fragments):
        return raw_app
    if norm_proc_fn(raw_app) in blocked_runtimes:
        return raw_app

    target_token = cmd[2] if (len(cmd) >= 3 and Path(cmd[0]).name == "open" and cmd[1] == "-a") else cmd[0]
    target_path = Path(target_token)
    if target_path.suffix.lower() == ".app":
        for h in _read_app_bundle_hints(target_path):
            if classify_policy_fn(h) == "blocked" or norm_proc_fn(h) in blocked_runtimes:
                return h
        plist_path = target_path / "Contents" / "Info.plist"
        if plist_path.is_file():
            try:
                with plist_path.open("rb") as fh:
                    bid = str(plistlib.load(fh).get("CFBundleIdentifier") or "").lower()
                if bid and _is_blocked_runtime_name(bid, blocked_runtimes, blocked_aumid_fragments):
                    return bid
            except Exception:
                pass
    if classify_policy_fn(target_token) == "blocked" or _is_blocked_runtime_name(target_token, blocked_runtimes, blocked_aumid_fragments):
        return target_path.name

    for arg in (extra_args or []):
        if classify_policy_fn(arg) == "blocked" or _is_blocked_runtime_name(arg, blocked_runtimes, blocked_aumid_fragments):
            return arg
    return None


def spawn_detached_app(cmd: list[str], extra_args: list[str]) -> subprocess.Popen[Any]:
    full_cmd = list(cmd)
    if extra_args:
        if len(cmd) >= 3 and Path(cmd[0]).name == "open" and cmd[1] == "-a":
            full_cmd.extend(["--args", *extra_args])
        else:
            full_cmd.extend(extra_args)
    return subprocess.Popen(
        full_cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )


# macOS Clipboard (UTF-8 pbpaste / pbcopy)
def clipboard_read(max_chars: int) -> tuple[dict[str, Any] | None, str | None]:
    env = os.environ.copy()
    env["LANG"] = "en_US.UTF-8"
    env["LC_ALL"] = "en_US.UTF-8"
    try:
        res = subprocess.run(["/usr/bin/pbpaste"], capture_output=True, timeout=2.0, env=env)
        if res.returncode != 0:
            return None, "clipboard_read_failed: pbpaste returned non-zero exit status."
        text = res.stdout.decode("utf-8", errors="replace")
        if not text:
            return {"hasText": False, "text": "", "length": 0, "truncated": False}, None
        truncated = len(text) > max_chars
        if truncated:
            text = text[:max_chars]
        return {"hasText": True, "text": text, "length": len(text), "truncated": truncated}, None
    except Exception as exc:
        return None, f"clipboard_read_failed: {exc}"


def clipboard_write(text: str) -> tuple[dict[str, Any] | None, str | None]:
    env = os.environ.copy()
    env["LANG"] = "en_US.UTF-8"
    env["LC_ALL"] = "en_US.UTF-8"
    encoded = text.encode("utf-8")
    try:
        res = subprocess.run(["/usr/bin/pbcopy"], input=encoded, capture_output=True, timeout=2.0, env=env)
        if res.returncode != 0:
            return None, "clipboard_set_failed: pbcopy returned non-zero exit status."
        return {"characters": len(text), "bytes": len(encoded)}, None
    except Exception as exc:
        return None, f"clipboard_set_failed: {exc}"


# macOS AppKit Overlay HUD Spawner
def _ensure_brand_mark_png(runtime_dir: Path) -> None:
    dst = runtime_dir / "brand_mark.png"
    if dst.is_file() and dst.stat().st_size > 200:
        return
    try:
        mark = antigravity_brand.load_antigravity_mark()
        tmp = runtime_dir / f".brand_mark.{os.getpid()}.tmp.png"
        mark.save(tmp, format="PNG")
        tmp.replace(dst)
    except Exception:
        pass


def spawn_overlay_hud(runtime_dir: Path) -> subprocess.Popen[Any] | None:
    bin_path = ensure_overlay_hud_binary()
    if bin_path is None:
        return None
    _ensure_brand_mark_png(runtime_dir)
    env = os.environ.copy()
    env["CUA_RUNTIME_DIR"] = str(runtime_dir)
    try:
        return subprocess.Popen(
            [str(bin_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
            env=env,
        )
    except Exception:
        return None


def is_overlay_exe_match(exe_low: str) -> bool:
    return (not exe_low) or ("overlay_hud" in exe_low) or ("python" in exe_low)


# POSIX Parent-PID Lifecycle Watchdog Hook
def make_parent_exit_checker() -> tuple[Callable[[], bool], Callable[[], None]]:
    initial_ppid = os.getppid()

    def _parent_exited() -> bool:
        if os.getppid() == 1:
            return True
        if initial_ppid > 1 and not pid_is_alive(initial_ppid):
            return True
        return False

    def _close() -> None:
        pass

    return _parent_exited, _close
