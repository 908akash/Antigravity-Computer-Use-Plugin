"""End-to-end verification suite for Antigravity Computer Use MCP server (v3.2.0).

Runs natively on both macOS (darwin, using the compiled Swift AppKit NSWindow
harness `tests/macos_test_harness.swift`) and Windows 10/11 (win32, keeping the
Win32 TkinterHarness untouched), spawning `computer_use_server.py` over stdio
MCP JSON-RPC 2.0 and verifying:
  1. initialize + notifications/initialized + ping + tools/list (all 25 tools)
  2. cu_status + cu_list_windows + cu_wait_for_window
  3. cu_grant_app + defense-in-depth blocks (terminals/shells/password managers/
     agent hosts, System Settings security panes / Terminal AUMID, blocked chords,
     browser look_only, sensitive editor guard)
  4. cu_ui_tree + cu_set_value (Unicode + ASCII) + cu_click_element (live UI state)
  5. cu_screenshot (Retina screencapture on macOS / PrintWindow on Windows) + cu_zoom
  6. cu_clipboard_write + cu_clipboard_read Unicode round-trip & guards
  7. HUD pill caption sanitization, constant 2.15s shimmer, execution/idle IPC,
     Unicode/boundary local matcher checks, modal non-deadlock (<1.5s), Esc cancel,
     grant TTL auto-expiry, zero-user-path / zero-codename / AST-import static scan,
     and clean grant cleanup.
"""

from __future__ import annotations

import ast
import base64
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Any

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

if IS_WIN:
    from ctypes import wintypes
    import tkinter as tk

PLUGIN_DIR = Path(__file__).resolve().parent.parent
SERVER_SCRIPT = PLUGIN_DIR / "scripts" / "computer_use_server.py"
RUNTIME_ROOT = Path(os.environ.get("CUA_RUNTIME_DIR") or (PLUGIN_DIR / ".runtime"))
STATE_FILE = RUNTIME_ROOT / "session_state.json"
LATEST_VERIFIED_PNG = RUNTIME_ROOT / "screenshots" / "verified_window_latest.png"

_py_tag = f"pylibs_cp{sys.version_info[0]}{sys.version_info[1]}"
_pylibs_candidates: list[Path] = []
_env_pylibs = os.environ.get("CUA_PYLIBS_DIR", "").strip()
if _env_pylibs:
    _pylibs_candidates.append(Path(_env_pylibs))
else:
    for _rt_base in (RUNTIME_ROOT, PLUGIN_DIR / ".runtime"):
        _exact = _rt_base / _py_tag
        if _exact.is_dir() and _exact not in _pylibs_candidates:
            _pylibs_candidates.append(_exact)
for _cand in reversed(_pylibs_candidates):
    if _cand.is_dir() and str(_cand) not in sys.path:
        sys.path.insert(0, str(_cand))

from PIL import Image, ImageStat

if str(PLUGIN_DIR / "scripts") not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR / "scripts"))
import computer_use_server as server_mod  # noqa: E402

EXPECTED_TOOLS = {
    "cu_status",
    "cu_list_windows",
    "cu_wait_for_window",
    "cu_grant_app",
    "cu_revoke_app",
    "cu_launch_app",
    "cu_screenshot",
    "cu_observe",
    "cu_zoom",
    "cu_ui_tree",
    "cu_focus_window",
    "cu_click_element",
    "cu_set_value",
    "cu_click",
    "cu_move",
    "cu_drag",
    "cu_scroll",
    "cu_type",
    "cu_key",
    "cu_clipboard_read",
    "cu_clipboard_write",
    "cu_overlay",
    "cu_choose",
    "cu_step",
    "cu_batch",
}

if IS_WIN:
    user32 = ctypes.windll.user32
    user32.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
    user32.SetWindowTextW.restype = wintypes.BOOL


class TkinterHarness:
    """Thread-safe dark-themed Win32 Tkinter verification window."""

    WINDOW_TITLE = "Antigravity CUA Verification Harness"

    def __init__(self) -> None:
        self.ready = threading.Event()
        self.stop_event = threading.Event()
        self.cmd_queue: queue.Queue[tuple[str, Any, threading.Event]] = queue.Queue()
        self.lock = threading.Lock()
        self.hwnd: int = 0
        self.entry_hwnd: int = 0
        self.apply_btn_hwnd: int = 0
        self.ping_btn_hwnd: int = 0
        self.entry_value: str = ""
        self.apply_clicks: int = 0
        self.ping_clicks: int = 0
        self.status_text: str = ""
        self._thread = threading.Thread(target=self._run_gui, name="cua-tk-gui", daemon=True)

    def start(self) -> None:
        self._thread.start()
        if not self.ready.wait(timeout=8.0):
            raise RuntimeError("Timed out waiting for Tkinter verification window to initialize.")

    def set_title(self, new_title: str) -> None:
        done = threading.Event()
        self.cmd_queue.put(("set_title", new_title, done))
        done.wait(timeout=3.0)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "hwnd": self.hwnd,
                "entry_hwnd": self.entry_hwnd,
                "apply_btn_hwnd": self.apply_btn_hwnd,
                "ping_btn_hwnd": self.ping_btn_hwnd,
                "entry_value": self.entry_value,
                "apply_clicks": self.apply_clicks,
                "ping_clicks": self.ping_clicks,
                "status_text": self.status_text,
            }

    def stop(self) -> None:
        self.stop_event.set()
        self._thread.join(timeout=4.0)

    def _run_gui(self) -> None:
        root = tk.Tk()
        root.title(self.WINDOW_TITLE)
        root.geometry("580x360+110+110")
        root.configure(bg="#0f172a")
        root.resizable(False, False)

        header = tk.Frame(root, bg="#1e293b", padx=20, pady=14)
        header.pack(fill="x", padx=16, pady=(16, 10))
        tk.Label(
            header,
            text="Antigravity Computer Use",
            fg="#f8fafc",
            bg="#1e293b",
            font=("Segoe UI", 14, "bold"),
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            header,
            text="Background Win32 PrintWindow + Hybrid UIA/HWND Verification",
            fg="#94a3b8",
            bg="#1e293b",
            font=("Segoe UI", 9),
            anchor="w",
        ).pack(fill="x", pady=(2, 0))

        body = tk.Frame(root, bg="#0f172a", padx=20, pady=6)
        body.pack(fill="both", expand=True)

        tk.Label(
            body,
            text="Automation Payload Input:",
            fg="#cbd5e1",
            bg="#0f172a",
            font=("Segoe UI", 10, "bold"),
            anchor="w",
        ).pack(fill="x", pady=(0, 4))

        entry = tk.Entry(
            body,
            bg="#020617",
            fg="#38bdf8",
            insertbackground="#38bdf8",
            relief="solid",
            bd=1,
            font=("Consolas", 11),
        )
        entry.pack(fill="x", ipady=6, pady=(0, 12))
        entry.insert(0, "initial_placeholder")

        btn_row = tk.Frame(body, bg="#0f172a")
        btn_row.pack(fill="x", pady=(0, 14))

        status_var = tk.StringVar(value="PENDING — Clicks: 0 | Value: initial_placeholder")
        status_banner = tk.Label(
            body,
            textvariable=status_var,
            fg="#e2e8f0",
            bg="#1e293b",
            font=("Segoe UI", 10, "bold"),
            padx=12,
            pady=10,
            anchor="w",
        )

        def on_apply() -> None:
            val = entry.get()
            with self.lock:
                self.apply_clicks += 1
                clicks = self.apply_clicks
                new_status = f"VERIFIED — Clicks: {clicks} | Value: {val}"
                self.entry_value = val
                self.status_text = new_status
            status_var.set(new_status)
            status_banner.configure(bg="#064e3b", fg="#4ade80")

        def on_ping() -> None:
            with self.lock:
                self.ping_clicks += 1

        apply_btn = tk.Button(
            btn_row,
            text="Apply Action",
            command=on_apply,
            bg="#2563eb",
            fg="#ffffff",
            activebackground="#1d4ed8",
            activeforeground="#ffffff",
            relief="flat",
            padx=16,
            pady=6,
            font=("Segoe UI", 10, "bold"),
        )
        apply_btn.pack(side="left", padx=(0, 10))

        ping_btn = tk.Button(
            btn_row,
            text="Secondary Ping",
            command=on_ping,
            bg="#334155",
            fg="#f1f5f9",
            activebackground="#475569",
            activeforeground="#ffffff",
            relief="flat",
            padx=14,
            pady=6,
            font=("Segoe UI", 10),
        )
        ping_btn.pack(side="left")

        status_banner.pack(fill="x")

        root.update_idletasks()
        root.update()

        top_hwnd = int(root.wm_frame(), 16)
        ent_hwnd = int(entry.winfo_id())
        app_btn_hwnd = int(apply_btn.winfo_id())
        png_btn_hwnd = int(ping_btn.winfo_id())

        user32.SetWindowTextW(wintypes.HWND(app_btn_hwnd), "Apply Action")
        user32.SetWindowTextW(wintypes.HWND(png_btn_hwnd), "Secondary Ping")

        with self.lock:
            self.hwnd = top_hwnd
            self.entry_hwnd = ent_hwnd
            self.apply_btn_hwnd = app_btn_hwnd
            self.ping_btn_hwnd = png_btn_hwnd
            self.entry_value = entry.get()
            self.status_text = status_var.get()

        self.ready.set()

        def _pump_tick() -> None:
            while True:
                try:
                    cmd, arg, ev = self.cmd_queue.get_nowait()
                except queue.Empty:
                    break
                if cmd == "set_title":
                    root.title(str(arg))
                    root.update_idletasks()
                ev.set()

            with self.lock:
                self.entry_value = entry.get()
                self.status_text = status_var.get()

            if self.stop_event.is_set():
                root.quit()
                return
            root.after(10, _pump_tick)

        root.after(10, _pump_tick)
        root.mainloop()

        del status_var, status_banner, apply_btn, ping_btn, entry, btn_row, body, header
        root.destroy()
        del root


class MacOSAppKitHarness:
    """Native macOS AppKit NSWindow test harness (avoids deprecated Apple CLT Tk 8.5)."""

    WINDOW_TITLE = "Antigravity CUA Verification Harness"

    def __init__(self) -> None:
        self.proc: subprocess.Popen[str] | None = None
        self.hwnd: int = 0
        self.pid: int = 0
        self.process_name: str = "macos_test_harness"
        self._lock = threading.Lock()

    def _compile(self) -> Path:
        src = PLUGIN_DIR / "tests" / "macos_test_harness.swift"
        bin_dir = RUNTIME_ROOT / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        bin_path = bin_dir / "macos_test_harness"
        sha_path = bin_dir / "macos_test_harness.sha256"
        src_sha = hashlib.sha256(src.read_bytes()).hexdigest()
        if bin_path.is_file() and sha_path.is_file():
            bin_sha = hashlib.sha256(bin_path.read_bytes()).hexdigest()
            if sha_path.read_text(encoding="utf-8").strip() == f"{src_sha}:{bin_sha}":
                return bin_path
        subprocess.run(["xcrun", "swiftc", "-O", str(src), "-o", str(bin_path)], check=True, timeout=45)
        os.chmod(bin_path, 0o755)
        bin_sha = hashlib.sha256(bin_path.read_bytes()).hexdigest()
        sha_path.write_text(f"{src_sha}:{bin_sha}\n", encoding="utf-8")
        return bin_path

    def start(self) -> None:
        bin_path = self._compile()
        self.proc = subprocess.Popen(
            [str(bin_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline()
        if not line:
            err = self.proc.stderr.read() if self.proc.stderr else ""
            raise RuntimeError(f"MacOSAppKitHarness exited prematurely: {err}")
        ready = json.loads(line)
        self.hwnd = int(ready["hwnd"])
        self.pid = int(ready["pid"])
        time.sleep(0.15)

    def _send_cmd(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            assert self.proc is not None and self.proc.stdin is not None and self.proc.stdout is not None
            self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("MacOSAppKitHarness closed stdout unexpectedly.")
            return json.loads(line)

    def set_title(self, new_title: str) -> None:
        self._send_cmd({"cmd": "set_title", "title": new_title})
        time.sleep(0.08)

    def set_modal(self, active: bool) -> None:
        self._send_cmd({"cmd": "set_modal", "active": bool(active)})
        time.sleep(0.12)

    def snapshot(self) -> dict[str, Any]:
        data = self._send_cmd({"cmd": "snapshot"})
        return {
            "hwnd": int(data["hwnd"]),
            "entry_hwnd": 0,
            "apply_btn_hwnd": 0,
            "ping_btn_hwnd": 0,
            "entry_value": str(data["entry_value"]),
            "apply_clicks": int(data["apply_clicks"]),
            "ping_clicks": int(data["ping_clicks"]),
            "status_text": str(data["status_text"]),
        }

    def stop(self) -> None:
        if self.proc is None:
            return
        try:
            if self.proc.stdin and self.proc.poll() is None:
                self.proc.stdin.write('{"cmd":"quit"}\n')
                self.proc.stdin.flush()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=2.0)


class McpStdioClient:
    """Minimal JSON-RPC 2.0 stdio client for computer_use_server.py."""

    def __init__(self, python_exe: str, server_script: Path) -> None:
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        if not env.get("CUA_PYLIBS_DIR") and _pylibs_candidates:
            env["CUA_PYLIBS_DIR"] = str(_pylibs_candidates[0])
        self.proc = subprocess.Popen(
            [python_exe, str(server_script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        self._next_id = 1

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        assert self.proc.stdin is not None
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self.proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        assert self.proc.stdin is not None and self.proc.stdout is not None
        req_id = self._next_id
        self._next_id += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            payload["params"] = params
        self.proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

        raw_line = self.proc.stdout.readline()
        if not raw_line:
            stderr_out = self.proc.stderr.read().decode("utf-8", errors="replace") if self.proc.stderr else ""
            raise RuntimeError(f"MCP server closed stdout unexpectedly during {method}. Stderr:\n{stderr_out}")
        resp = json.loads(raw_line.decode("utf-8"))
        assert resp.get("id") == req_id, f"Mismatched JSON-RPC id: expected {req_id}, got {resp.get('id')}"
        return resp

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        assert "result" in resp, f"JSON-RPC error calling {name}: {resp}"
        return resp["result"]

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=2.0)


def _get_stdlib_module_names() -> set[str]:
    names = getattr(sys, "stdlib_module_names", None)
    if names is not None:
        return set(names)
    # Python 3.9 fallback (Apple CLT /usr/bin/python3)
    base = set(sys.builtin_module_names) | {
        "__future__", "argparse", "ast", "atexit", "base64", "collections", "concurrent",
        "contextlib", "ctypes", "datetime", "difflib", "errno", "fnmatch", "functools",
        "glob", "hashlib", "heapq", "http", "importlib", "inspect", "io", "ipaddress", "itertools",
        "json", "logging", "math", "os", "pathlib", "platform", "plistlib", "posix",
        "posixpath", "queue", "random", "re", "shutil", "signal", "socket", "ssl", "stat",
        "string", "struct", "subprocess", "sys", "tempfile", "textwrap", "threading",
        "time", "tkinter", "traceback", "typing", "unicodedata", "urllib", "uuid",
        "warnings", "weakref", "winreg", "xml", "zipfile", "zlib",
    }
    return base


def main() -> int:
    harness: Any = MacOSAppKitHarness() if IS_MAC else TkinterHarness()
    harness.start()
    client = McpStdioClient(sys.executable, SERVER_SCRIPT)
    results_summary: dict[str, Any] = {"platform": sys.platform}

    try:
        # 1. initialize + notifications/initialized + ping + tools/list
        init_resp = client.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "cua-e2e-test", "version": "1.0.0"},
            },
        )
        server_info = init_resp["result"]["serverInfo"]
        assert server_info["name"] == "computer-use", server_info
        assert server_info["version"] == "3.2.0", server_info
        client.notify("notifications/initialized", {})
        ping_resp = client.request("ping")
        assert ping_resp["result"] == {}

        client.call_tool("cu_revoke_app", {"all": True})

        tools_resp = client.request("tools/list")
        tools = tools_resp["result"]["tools"]
        tool_names = {t["name"] for t in tools}
        assert len(tools) == 25, f"Expected 25 tools, got {len(tools)}: {sorted(tool_names)}"
        assert tool_names == EXPECTED_TOOLS, f"Tool mismatch: diff={tool_names ^ EXPECTED_TOOLS}"

        by_name = {t["name"]: t for t in tools}
        assert by_name["cu_clipboard_read"]["annotations"]["readOnlyHint"] is True
        assert by_name["cu_wait_for_window"]["annotations"]["readOnlyHint"] is True
        assert by_name["cu_clipboard_write"]["annotations"]["readOnlyHint"] is False
        assert by_name["cu_overlay"]["annotations"]["readOnlyHint"] is False
        assert "horizontal" in by_name["cu_scroll"]["inputSchema"]["properties"]
        results_summary["toolsVerified"] = len(tools)

        # 2. cu_status + cu_list_windows + cu_wait_for_window
        status_res = client.call_tool("cu_status")
        assert not status_res["isError"], status_res
        status_data = status_res["structuredContent"]
        assert status_data["serverVersion"] == "3.2.0", status_data
        assert status_data["dpiAware"] == ("PerMonitorV2" if IS_WIN else "RetinaLogicalPoints"), status_data
        assert status_data["desktopLocked"] is False

        if IS_MAC:
            results_summary["macosTccStatus"] = {
                "accessibilityTrusted": status_data.get("accessibilityTrusted"),
                "screenRecordingGranted": status_data.get("screenRecordingGranted"),
            }
            assert status_data.get("accessibilityTrusted") is True, (
                f"macOS Accessibility permission required for live E2E tests. Status: {status_data}"
            )
            assert status_data.get("screenRecordingGranted") is True, (
                f"macOS Screen Recording permission required for live E2E tests. Status: {status_data}"
            )

        snap0 = harness.snapshot()
        target_hwnd = snap0["hwnd"]
        assert target_hwnd > 0

        harness_proc = "macos_test_harness" if IS_MAC else Path(sys.executable).name
        wait_res = client.call_tool(
            "cu_wait_for_window",
            {
                "title_contains": "Antigravity CUA Verification",
                "process_name": harness_proc,
                "timeout_sec": 5.0,
            },
        )
        assert not wait_res["isError"], wait_res
        wait_data = wait_res["structuredContent"]
        assert wait_data["matched"] is True
        assert any(int(w["hwnd"]) == target_hwnd for w in wait_data["windows"])

        timeout_res = client.call_tool(
            "cu_wait_for_window",
            {"title_contains": "__NonExistentWindow_99999__", "timeout_sec": 0.5},
        )
        assert timeout_res["isError"] is True
        assert "timeout:" in timeout_res["content"][0]["text"]

        list_res = client.call_tool("cu_list_windows", {"max_results": 50})
        assert not list_res["isError"], list_res
        listed_hwnds = {int(w["hwnd"]) for w in list_res["structuredContent"]["windows"]}
        assert target_hwnd in listed_hwnds
        results_summary["windowDiscoveryVerified"] = True

        # 3. cu_grant_app + defense-in-depth security guards
        ungranted_shot = client.call_tool("cu_screenshot", {"hwnd": target_hwnd})
        assert ungranted_shot["isError"] is True
        assert "app_not_granted" in ungranted_shot["content"][0]["text"]

        full_desk_guard = client.call_tool("cu_screenshot", {})
        assert full_desk_guard["isError"] is True
        assert "allow_full_desktop" in full_desk_guard["content"][0]["text"]

        zoom_desk_guard = client.call_tool("cu_zoom", {"x": 100, "y": 100})
        assert zoom_desk_guard["isError"] is True
        assert "allow_full_desktop" in zoom_desk_guard["content"][0]["text"]

        blocked_procs = (
            ("Terminal", "iTerm2", "Keychain Access", "Passwords", "1Password", "Activity Monitor", "SecurityAgent", "Antigravity", "Cursor", "powershell.exe")
            if IS_MAC
            else ("powershell.exe", "cmd.exe", "Antigravity.exe", "1Password.exe", "powershell", "wsl", "cmd")
        )
        for blocked_proc in blocked_procs:
            blk = client.call_tool("cu_grant_app", {"process_name": blocked_proc})
            assert blk["isError"] is True, f"Expected {blocked_proc} to be blocked"
            assert "policy_blocked" in blk["content"][0]["text"]

        if IS_MAC:
            assert server_mod.check_blocked_system_window("System Settings", "Privacy & Security") is not None
            assert server_mod.check_blocked_system_window("System Settings", "Screen & System Audio Recording") is not None
            assert server_mod.check_blocked_system_window("System Settings", "Appearance") is None
            for raw_blk, cmd_blk, args_blk in (
                ("/usr/bin/open", ["/usr/bin/open"], ["-b", "com.apple.Terminal"]),
                ("/usr/bin/python3.9", ["/usr/bin/python3.9"], []),
                ("/usr/bin/perl", ["/usr/bin/perl"], []),
            ):
                assert server_mod._is_blocked_launch_target(raw_blk, cmd_blk, args_blk) is not None, (raw_blk, args_blk)

        browser_name = "Safari" if IS_MAC else "chrome.exe"
        browser_grant = client.call_tool("cu_grant_app", {"process_name": browser_name})
        assert not browser_grant["isError"], browser_grant
        assert browser_grant["structuredContent"]["mode"] == "look_only"

        grant_res = client.call_tool("cu_grant_app", {"hwnd": target_hwnd})
        assert not grant_res["isError"], grant_res
        assert grant_res["structuredContent"]["mode"] == "interactive"

        blocked_chords = (
            ("cmd+space", "cmd+opt+esc", "cmd+shift+opt+esc", ["cmd", " "], "cmd+ctrl+q", "ctrl+shift+esc")
            if IS_MAC
            else ("win+r", "ctrl+shift+esc", "ctrl+alt+esc", "ctrl+esc")
        )
        for blocked_chord in blocked_chords:
            blk_chord_res = client.call_tool("cu_key", {"hwnd": target_hwnd, "keys": blocked_chord})
            assert blk_chord_res["isError"] is True, f"Expected {blocked_chord} to be blocked"
            assert "policy_blocked" in blk_chord_res["content"][0]["text"]

        empty_key_res = client.call_tool("cu_key", {"hwnd": target_hwnd, "keys": ["ctrl", ""]})
        assert empty_key_res["isError"] is True, empty_key_res

        assert server_mod._is_unc_or_device_path(r"\??\C:\foo.json") is True
        assert server_mod._is_unc_or_device_path("CON.json") is True
        assert server_mod._is_unc_or_device_path(r"C:\tmp\nul") is True
        assert server_mod._is_unc_or_device_path("console.json") is False

        if IS_WIN:
            explorer_cmd_res = client.call_tool("cu_launch_app", {"app": "explorer.exe", "args": ["cmd.exe"]})
            assert explorer_cmd_res["isError"] is True
            assert "policy_blocked" in explorer_cmd_res["content"][0]["text"]
            assert server_mod._is_blocked_launch_target(
                "Terminal",
                ["explorer.exe", "shell:AppsFolder\\Microsoft.WindowsTerminal_8wekyb3d8bbwe!App"],
            ) is not None

        # Sensitive editor title guard (unit + live end-to-end over MCP)
        assert server_mod.check_sensitive_editor_title("notepad.exe", "mcp_config.json - Notepad") is not None
        assert server_mod.check_sensitive_editor_title("notepad.exe", "*mcp_config.json - Notepad") is not None
        assert server_mod.check_sensitive_editor_title("notepad.exe", "•mcp_config.json - Notepad") is not None
        assert server_mod.check_sensitive_editor_title("notepad.exe", "notes.txt - Notepad") is None
        assert server_mod.check_sensitive_editor_title("TextEdit", ".zshrc — Edited") is not None
        assert server_mod.check_sensitive_editor_title("TextEdit", "●.zshrc") is not None
        assert server_mod.check_sensitive_editor_title("TextEdit", ".zprofile") is not None
        assert server_mod.check_sensitive_editor_title("TextEdit", "deploy.sh - TextEdit") is not None
        assert server_mod.check_sensitive_editor_title("TextEdit", "deploy.bash - TextEdit") is not None
        assert server_mod.check_sensitive_editor_title("coteditor", "profile.psm1 - CotEditor") is not None
        assert server_mod.check_sensitive_editor_title("TextEdit", "app.py - TextEdit") is None
        assert server_mod.check_sensitive_editor_title("code.exe", "index.js - Visual Studio Code") is None

        harness.set_title("mcp_config.json - TextEdit" if IS_MAC else "mcp_config.json - Notepad")
        try:
            sens_live = client.call_tool("cu_type", {"hwnd": target_hwnd, "text": "injected"})
            assert sens_live["isError"] is True
            assert "sensitive_file_guard" in sens_live["content"][0]["text"]
        finally:
            harness.set_title(harness.WINDOW_TITLE)
        results_summary["securityGuardsVerified"] = True

        # 4. cu_ui_tree + cu_set_value + cu_click_element
        tree_res = client.call_tool("cu_ui_tree", {"hwnd": target_hwnd})
        assert not tree_res["isError"], tree_res
        tree_data = tree_res["structuredContent"]
        tree_obs_id = tree_data["observationId"]
        elements = tree_data["elements"]
        assert len(elements) >= 3, f"Expected >=3 child elements in harness window, got {elements}"

        if IS_WIN:
            entry_aid = f"hwnd:{snap0['entry_hwnd']}"
            apply_aid = f"hwnd:{snap0['apply_btn_hwnd']}"
            entry_el = next((e for e in elements if e.get("automationId") == entry_aid), None)
            apply_el = next(
                (e for e in elements if e.get("automationId") == apply_aid or e.get("name") == "Apply Action"),
                None,
            )
        else:
            entry_el = next(
                (e for e in elements if e.get("automationId") == "payloadEntry" or e.get("controlType") == "Edit"),
                None,
            )
            apply_el = next(
                (e for e in elements if e.get("automationId") == "applyActionButton" or e.get("name") == "Apply Action"),
                None,
            )
        assert entry_el is not None, f"Entry element not found in UI tree: {elements}"
        assert apply_el is not None, f"Apply Action button not found in UI tree: {elements}"

        unicode_name = "O\u2019Brien"
        set_uni_res = client.call_tool(
            "cu_set_value",
            {"observation_id": tree_obs_id, "element_index": entry_el["index"], "value": unicode_name},
        )
        assert not set_uni_res["isError"], set_uni_res
        deadline_u = time.monotonic() + 2.0
        while time.monotonic() < deadline_u:
            if harness.snapshot()["entry_value"] == unicode_name:
                break
            time.sleep(0.03)
        assert harness.snapshot()["entry_value"] == unicode_name, (
            f"Expected entry '{unicode_name}', got '{harness.snapshot()['entry_value']}'"
        )

        target_text = "Antigravity macOS CUA Verified!" if IS_MAC else "Antigravity Win32 CUA Verified!"
        set_res = client.call_tool(
            "cu_set_value",
            {"observation_id": tree_obs_id, "element_index": entry_el["index"], "value": target_text},
        )
        assert not set_res["isError"], set_res

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if harness.snapshot()["entry_value"] == target_text:
                break
            time.sleep(0.03)
        snap_after_set = harness.snapshot()
        assert snap_after_set["entry_value"] == target_text, (
            f"Expected entry '{target_text}', got '{snap_after_set['entry_value']}'"
        )

        click_res = client.call_tool(
            "cu_click_element",
            {"observation_id": tree_obs_id, "element_index": apply_el["index"]},
        )
        assert not click_res["isError"], click_res

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if harness.snapshot()["apply_clicks"] >= 1:
                break
            time.sleep(0.03)
        snap_after_click = harness.snapshot()
        assert snap_after_click["apply_clicks"] == 1, f"Expected 1 button click, got {snap_after_click}"
        assert "VERIFIED — Clicks: 1" in snap_after_click["status_text"]
        assert target_text in snap_after_click["status_text"]
        results_summary["liveUiAutomationVerified"] = {
            "entryValue": snap_after_click["entry_value"],
            "applyClicks": snap_after_click["apply_clicks"],
            "statusText": snap_after_click["status_text"],
        }

        time.sleep(0.08)

        # 5. cu_screenshot + cu_zoom
        shot_res = client.call_tool("cu_screenshot", {"hwnd": target_hwnd})
        assert not shot_res["isError"], shot_res
        shot_meta = shot_res["structuredContent"]
        expected_cap = "screencapture_window" if IS_MAC else "print_window_background"
        assert shot_meta["captureMethod"] == expected_cap, shot_meta
        img_blocks = [c for c in shot_res["content"] if c.get("type") == "image"]
        assert len(img_blocks) == 1, "Expected inline base64 PNG image block"
        raw_png = base64.b64decode(img_blocks[0]["data"])
        shot_img = Image.open(io.BytesIO(raw_png)).convert("RGB")
        stat = ImageStat.Stat(shot_img)
        assert max(hi for _, hi in stat.extrema) > 0, f"Screenshot is blank: {stat.extrema}"
        assert max(stat.stddev) > 8.0, f"Screenshot lacks visual contrast: stddev={stat.stddev}"
        saved_shot_path = Path(shot_meta["savedPath"])
        assert saved_shot_path.is_file()
        shutil.copy2(saved_shot_path, LATEST_VERIFIED_PNG)

        zoom_res = client.call_tool(
            "cu_zoom",
            {
                "observation_id": shot_meta["observationId"],
                "x": 24, "y": 24, "width": 280, "height": 160, "upscale": 2,
            },
        )
        assert not zoom_res["isError"], zoom_res
        zoom_meta = zoom_res["structuredContent"]
        assert zoom_meta["returnedWidth"] == 560 and zoom_meta["returnedHeight"] == 320, zoom_meta
        zoom_blocks = [c for c in zoom_res["content"] if c.get("type") == "image"]
        assert len(zoom_blocks) == 1
        zoom_img = Image.open(io.BytesIO(base64.b64decode(zoom_blocks[0]["data"]))).convert("RGB")
        assert max(hi for _, hi in ImageStat.Stat(zoom_img).extrema) > 0
        results_summary["screenshotVerified"] = {
            "captureMethod": shot_meta["captureMethod"],
            "size": [shot_img.width, shot_img.height],
            "scaleX": shot_meta["scaleX"],
            "scaleY": shot_meta["scaleY"],
            "savedPath": str(LATEST_VERIFIED_PNG),
            "zoomSize": [zoom_img.width, zoom_img.height],
        }

        # 6. cu_clipboard_write + cu_clipboard_read round-trip & guards
        assert server_mod._parse_bool("false") is False
        assert server_mod._parse_bool("0") is False
        assert server_mod._parse_bool("no") is False
        assert server_mod._parse_bool("") is False
        assert server_mod._parse_bool("true") is True
        assert server_mod._parse_bool(None, True) is True

        clip_guard = client.call_tool("cu_clipboard_read", {})
        assert clip_guard["isError"] is True
        assert "clipboard_permission_required" in clip_guard["content"][0]["text"]

        clip_str_false = client.call_tool("cu_clipboard_read", {"allow_clipboard": "false"})
        assert clip_str_false["isError"] is True
        assert "clipboard_permission_required" in clip_str_false["content"][0]["text"]

        clip_long = client.call_tool("cu_clipboard_write", {"text": "x" * 16001})
        assert clip_long["isError"] is True
        assert "text_too_long" in clip_long["content"][0]["text"]

        clip_payload = "Antigravity Clipboard Round-Trip ✓ — Cross-Platform Unicode (2026)"
        clip_write = client.call_tool("cu_clipboard_write", {"text": clip_payload})
        if clip_write["isError"] and server_mod._IS_LOW_INTEGRITY and "clipboard_set_failed" in clip_write["content"][0]["text"]:
            results_summary["clipboardRoundTripVerified"] = "low_integrity_sandbox_uipi_isolated"
        else:
            assert not clip_write["isError"], clip_write
            assert clip_write["structuredContent"]["characters"] == len(clip_payload)

            clip_read = client.call_tool("cu_clipboard_read", {"allow_clipboard": True})
            assert not clip_read["isError"], clip_read
            assert clip_read["structuredContent"]["hasText"] is True
            assert clip_read["structuredContent"]["text"] == clip_payload
            results_summary["clipboardRoundTripVerified"] = True

        # 6b. Screen-border glow HUD overlay state & native self-test
        ov_state_file = RUNTIME_ROOT / "overlay_state.json"
        assert ov_state_file.is_file(), "Expected overlay_state.json to be written during granted actions"
        ov_after_actions = json.loads(ov_state_file.read_text(encoding="utf-8"))
        assert ov_after_actions.get("active") is True, ov_after_actions
        assert isinstance(ov_after_actions.get("click"), dict), ov_after_actions

        ov_tool_res = client.call_tool(
            "cu_overlay",
            {"active": True, "label": "E2E Overlay Verification", "cursor_style": "sleek_dart", "click_x": 320, "click_y": 240, "ttl_sec": 12},
        )
        assert not ov_tool_res["isError"], ov_tool_res
        assert ov_tool_res["structuredContent"]["active"] is True
        assert ov_tool_res["structuredContent"]["cursorStyle"] == "sleek_dart"
        assert ov_tool_res["structuredContent"]["label"] == "E2E Overlay Verification"

        if IS_WIN:
            hud_proc = subprocess.run(
                [sys.executable, str(PLUGIN_DIR / "scripts" / "overlay_hud.py"), "--self-test"],
                capture_output=True, text=True, timeout=10, check=True,
            )
            hud_info = json.loads(hud_proc.stdout.strip())
            assert hud_info.get("ok") is True and int(hud_info.get("hwnd", 0)) > 0, hud_info
        else:
            import macos_backend as mac_be
            mac_be.configure(RUNTIME_ROOT, RUNTIME_ROOT / "overlay_pid.json")
            hud_bin = mac_be.ensure_overlay_hud_binary()
            assert hud_bin is not None and hud_bin.is_file(), "Failed to compile overlay_hud_mac.swift"
            hud_proc = subprocess.run([str(hud_bin), "--self-test"], capture_output=True, text=True, timeout=10, check=True)
            hud_info = json.loads(hud_proc.stdout.strip())
            assert hud_info.get("ok") is True, hud_info
            assert hud_info.get("sharingTypeNone") is True, hud_info
            assert hud_info.get("clickThrough") is True, hud_info
            assert hud_info.get("captionChecksPassed") is True, hud_info
            assert abs(float(hud_info.get("shimmerCycleSec", 0)) - 2.15) < 0.01, hud_info
            assert int(hud_info.get("shimmerFrames", 0)) == 28, hud_info
        results_summary["overlayHudVerified"] = hud_info

        # 6c. Warm cached UI tree latency, cu_observe (WebP + SoM), and cu_batch
        t_uia0 = time.perf_counter()
        fast_tree = client.call_tool("cu_ui_tree", {"hwnd": target_hwnd})
        warm_uia_ms = round((time.perf_counter() - t_uia0) * 1000.0, 1)
        assert not fast_tree["isError"], fast_tree

        t_obs0 = time.perf_counter()
        obs_res = client.call_tool("cu_observe", {"hwnd": target_hwnd})
        obs_ms = round((time.perf_counter() - t_obs0) * 1000.0, 1)
        assert not obs_res["isError"], obs_res
        obs_meta = obs_res["structuredContent"]
        assert obs_meta["mimeType"] == "image/webp", obs_meta
        assert "frameHash" in obs_meta and len(obs_meta.get("elements", [])) >= 3, obs_meta

        ping_el = next((e for e in obs_meta["elements"] if "Secondary Ping" in str(e.get("name") or "")), None)
        assert ping_el is not None, obs_meta["elements"]
        batch_res = client.call_tool(
            "cu_batch",
            {
                "hwnd": target_hwnd,
                "steps": [
                    {"action": "click_element", "observation_id": obs_meta["observationId"], "element_index": ping_el["index"]},
                    {"action": "wait", "ms": 35},
                    {"action": "screenshot", "format": "webp", "max_width": 960},
                ],
            },
        )
        assert not batch_res["isError"], batch_res
        assert harness.snapshot()["ping_clicks"] >= 1

        # 6d. Fast local element matcher (`cu_choose` + `cu_step`)
        choose_res = client.call_tool(
            "cu_choose",
            {"goal": "Click Secondary Ping", "observation_id": obs_meta["observationId"]},
        )
        assert not choose_res["isError"], choose_res
        choose_meta = choose_res["structuredContent"]
        assert choose_meta["act"] is True and choose_meta["elementIndex"] == ping_el["index"], choose_meta

        prev_pings = harness.snapshot()["ping_clicks"]
        step_res = client.call_tool("cu_step", {"hwnd": target_hwnd, "goal": "Click 'Secondary Ping'"})
        assert not step_res["isError"], step_res
        step_meta = step_res["structuredContent"]
        assert step_meta["status"] == "completed_steps", step_meta
        deadline_j = time.monotonic() + 1.5
        while time.monotonic() < deadline_j and harness.snapshot()["ping_clicks"] <= prev_pings:
            time.sleep(0.02)
        assert harness.snapshot()["ping_clicks"] >= prev_pings + 1

        # 6e. Role disambiguation + active_hwnd cu_batch chaining
        disambig_tab = client.call_tool(
            "cu_choose",
            {
                "goal": "Click 'Connections'",
                "elements": [
                    {"index": 0, "controlType": "ProgressBar", "name": "Connections", "enabled": True},
                    {"index": 1, "controlType": "TabItem", "name": "Connections", "enabled": True},
                    {"index": 2, "controlType": "Button", "name": "Settings", "enabled": True},
                ],
            },
        )
        assert not disambig_tab["isError"], disambig_tab
        assert disambig_tab["structuredContent"]["choice"] == "e1" and disambig_tab["structuredContent"]["act"] is True

        disambig_chk = client.call_tool(
            "cu_choose",
            {
                "goal": "Toggle Remember on Resume",
                "elements": [
                    {"index": 0, "controlType": "Text", "name": "Remember on Resume", "enabled": True},
                    {"index": 1, "controlType": "CheckBox", "name": "Remember on Resume", "enabled": True},
                    {"index": 2, "controlType": "CheckBox", "name": "Remember", "enabled": True},
                ],
            },
        )
        assert not disambig_chk["isError"], disambig_chk
        assert disambig_chk["structuredContent"]["choice"] == "e1" and disambig_chk["structuredContent"]["act"] is True

        done_chk = client.call_tool(
            "cu_choose",
            {
                "goal": "Click '=' until display shows 14",
                "elements": [
                    {"index": 0, "controlType": "Text", "name": "Display", "value": "14", "enabled": True},
                    {"index": 1, "controlType": "Button", "name": "=", "enabled": True},
                ],
            },
        )
        assert not done_chk["isError"], done_chk
        assert done_chk["structuredContent"]["choice"] == "done", done_chk

        prev_pings2 = harness.snapshot()["ping_clicks"]
        chain_res = client.call_tool(
            "cu_batch",
            {
                "steps": [
                    {"action": "wait_for_window", "title_contains": "Antigravity CUA Verification", "process_name": harness_proc, "timeout_sec": 3.0},
                    {"action": "step", "goal": "Click 'Secondary Ping' -> 'Apply Action'"},
                    {"action": "observe", "format": "webp"},
                ]
            },
        )
        assert not chain_res["isError"], chain_res
        chain_meta = chain_res["structuredContent"]
        assert chain_meta["completedSteps"] == 3, chain_meta
        assert all(int(s["hwnd"]) == target_hwnd for s in chain_meta["steps"]), chain_meta
        deadline_c = time.monotonic() + 1.5
        while time.monotonic() < deadline_c and harness.snapshot()["ping_clicks"] <= prev_pings2:
            time.sleep(0.02)
        assert harness.snapshot()["ping_clicks"] >= prev_pings2 + 1

        # 6f. Calculator softmax + multi-point smooth cu_drag + Paint matcher checks
        calc_elems = [
            {"index": i, "controlType": "Button", "name": nm, "automationId": aid, "enabled": True}
            for i, (nm, aid) in enumerate([
                ("Zero", "num0Button"), ("One", "num1Button"), ("Two", "num2Button"),
                ("Three", "num3Button"), ("Four", "num4Button"), ("Five", "num5Button"),
                ("Six", "num6Button"), ("Seven", "num7Button"), ("Eight", "num8Button"),
                ("Nine", "num9Button"), ("Multiply by", "multiplyButton"),
                ("Divide by", "divideButton"), ("Plus", "plusButton"), ("Minus", "minusButton"),
                ("Equals", "equalButton"), ("Clear", "clearButton"), ("Clear entry", "clearEntryButton"),
            ])
        ]
        mul_res = client.call_tool("cu_choose", {"goal": "Press '*'", "elements": calc_elems})
        assert not mul_res["isError"], mul_res
        assert mul_res["structuredContent"]["choice"] == "e10" and mul_res["structuredContent"]["confidence"] >= 0.95, mul_res

        poly_batch = client.call_tool(
            "cu_batch",
            {
                "hwnd": target_hwnd,
                "steps": [
                    {
                        "action": "drag",
                        "coordinate_space": "window",
                        "points": [[60, 310], [160, 295], [260, 315], [360, 300]] if IS_MAC else [[60, 220], [120, 190], [180, 220], [240, 195]],
                        "smooth": True,
                        "closed": True,
                        "duration": 0.04,
                    },
                    {"action": "observe", "format": "webp", "annotate": False, "max_width": 640},
                ],
            },
        )
        assert not poly_batch["isError"], poly_batch
        assert poly_batch["structuredContent"]["completedSteps"] == 2, poly_batch

        paint_elems = [{"index": i, "controlType": "Button", "name": f"Item {i}", "enabled": True} for i in range(89)]
        paint_elems[13] = {"index": 13, "controlType": "Slider", "name": "Size", "value": "3 pixel", "enabled": True}
        paint_elems[22] = {"index": 22, "controlType": "MenuItem", "name": "Edit", "enabled": True}
        paint_elems[25] = {"index": 25, "controlType": "RadioButton", "name": "Select", "enabled": True}
        paint_elems[32] = {"index": 32, "controlType": "Button", "name": "Fill", "enabled": True}
        paint_elems[35] = {"index": 35, "controlType": "Button", "name": "Colour picker", "enabled": True}
        paint_elems[40] = {"index": 40, "controlType": "Button", "name": "Size", "enabled": True}
        paint_elems[43] = {"index": 43, "controlType": "Button", "name": "Edit colours", "enabled": True}
        paint_elems[48] = {"index": 48, "controlType": "ListItem", "name": "Oval", "enabled": True}
        paint_elems[70] = {"index": 70, "controlType": "ListItem", "name": "Grey", "enabled": True}
        paint_elems[74] = {"index": 74, "controlType": "ListItem", "name": "Yellow", "enabled": True}
        paint_elems[86] = {"index": 86, "controlType": "ListItem", "name": "Light turquoise", "enabled": True}
        for p_goal, exp_idx in [
            ("Click Edit colors", 43), ("Click Select tool", 25), ("Click Gray", 70),
            ("Click Yellow swatch", 74), ("Click sky blue", 86),
            ("Click Size slider", 13), ("Click Size button", 40),
        ]:
            pr = client.call_tool("cu_choose", {"goal": p_goal, "elements": paint_elems})
            assert not pr["isError"] and pr["structuredContent"]["elementIndex"] == exp_idx and pr["structuredContent"]["act"] is True and pr["structuredContent"]["confidence"] >= 0.85, (p_goal, pr)

        import local_matcher as local_matcher_mod
        prev_matcher_env = os.environ.get("CUA_MATCHER_ENABLED")
        os.environ["CUA_MATCHER_ENABLED"] = "0"
        try:
            for uns_goal in ("Click Eyedropper", "Click Paint bucket", "Click Circle shape"):
                ur = local_matcher_mod.match_ui_element(uns_goal, paint_elems)
                assert ur["choice"] == "unsure" and ur["act"] is False, (uns_goal, ur)
        finally:
            if prev_matcher_env is None:
                os.environ.pop("CUA_MATCHER_ENABLED", None)
            else:
                os.environ["CUA_MATCHER_ENABLED"] = prev_matcher_env

        prev_ep_env = os.environ.get("CUA_MATCHER_ENDPOINT")
        prev_remote_env = os.environ.get("CUA_ALLOW_REMOTE_MATCHER")
        os.environ["CUA_MATCHER_ENDPOINT"] = "http://example.com:11434"
        os.environ.pop("CUA_ALLOW_REMOTE_MATCHER", None)
        try:
            assert local_matcher_mod._discover_local_server() is None
            assert local_matcher_mod._is_loopback_host("127.0.0.1") is True
            assert local_matcher_mod._is_loopback_host("127.0.0.2") is True
            assert local_matcher_mod._is_loopback_host("localhost") is True
            assert local_matcher_mod._is_loopback_host("::1") is True
            assert local_matcher_mod._is_loopback_host("example.com") is False
            assert local_matcher_mod._is_loopback_host("169.254.169.254") is False
            import urllib.error
            import urllib.request
            assert any(
                isinstance(h, local_matcher_mod._NoRedirectHandler)
                for h in local_matcher_mod._NO_PROXY_OPENER.handlers
            )
            redir_blocked = False
            try:
                local_matcher_mod._NoRedirectHandler().redirect_request(
                    urllib.request.Request("http://127.0.0.1:11434/test"),
                    None,
                    302,
                    "Found",
                    {},
                    "http://169.254.169.254/latest/meta-data/",
                )
            except urllib.error.URLError:
                redir_blocked = True
            assert redir_blocked is True, "Expected _NoRedirectHandler to reject HTTP redirects"
        finally:
            if prev_ep_env is None:
                os.environ.pop("CUA_MATCHER_ENDPOINT", None)
            else:
                os.environ["CUA_MATCHER_ENDPOINT"] = prev_ep_env
            if prev_remote_env is not None:
                os.environ["CUA_ALLOW_REMOTE_MATCHER"] = prev_remote_env

        # 6h. Streaming stage HUD + steps_file + modal non-deadlock (<1.5s)
        steps_file_path = RUNTIME_ROOT / "test_batch_chapter.json"
        steps_file_path.write_text(
            json.dumps([
                {
                    "action": "drag",
                    "coordinate_space": "window",
                    "points": [[70, 305], [160, 295], [250, 315]] if IS_MAC else [[70, 210], [140, 185], [210, 215]],
                    "smooth": True,
                    "duration": 0.03,
                },
                {"action": "observe", "format": "webp", "annotate": False, "max_width": 640},
            ]),
            encoding="utf-8",
        )
        sf_res = client.call_tool(
            "cu_batch",
            {"hwnd": target_hwnd, "stage": "3/3 Drawing curves", "steps_file": str(steps_file_path)},
        )
        assert not sf_res["isError"], sf_res
        assert sf_res["structuredContent"]["completedSteps"] == 2 and sf_res["structuredContent"].get("stage") == "3/3 Drawing curves", sf_res
        ov_now = json.loads((RUNTIME_ROOT / "overlay_state.json").read_text(encoding="utf-8"))
        assert ov_now.get("label") == "3/3 Drawing curves", ov_now
        assert ov_now.get("executing") is False, ov_now
        assert float(ov_now.get("thinkingWaitSec", 0)) == 90.0, ov_now
        assert float(ov_now.get("idleTimeoutSec", 0)) == 600.0, ov_now
        assert server_mod._THINKING_WAIT_SEC == 90.0
        assert server_mod._HUD_IDLE_TIMEOUT_SEC == 600.0
        assert server_mod._GRANT_IDLE_TTL_SEC == 600.0

        # Modal sheet / modal-disabled owner window must NOT hang observe
        if IS_WIN:
            u32_modal = ctypes.WinDLL("user32", use_last_error=True)
            u32_modal.EnableWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
            u32_modal.EnableWindow.restype = wintypes.BOOL
            u32_modal.EnableWindow(wintypes.HWND(target_hwnd), False)
            try:
                t_modal0 = time.perf_counter()
                modal_obs = client.call_tool("cu_observe", {"hwnd": target_hwnd, "format": "webp", "max_width": 640})
                modal_dt = time.perf_counter() - t_modal0
            finally:
                u32_modal.EnableWindow(wintypes.HWND(target_hwnd), True)
        else:
            harness.set_modal(True)
            try:
                t_modal0 = time.perf_counter()
                modal_obs = client.call_tool("cu_observe", {"hwnd": target_hwnd, "format": "webp", "max_width": 640})
                modal_dt = time.perf_counter() - t_modal0
            finally:
                harness.set_modal(False)
        assert not modal_obs["isError"], modal_obs
        assert modal_dt < 1.5, f"Modal window observe took {modal_dt:.2f}s (expected <1.5s)"

        # 6i. Production invariants: HUD caption/shimmer, Esc cancel, platform-specific checks, Unicode matcher, grant TTL
        if IS_WIN:
            assert server_mod._resolve_vk_token("+")[0] == 0xBB
            import overlay_hud as hud_mod
            assert hud_mod.DEFAULT_PILL_CAPTION == "Antigravity is using your Computer"
            assert hud_mod._format_pill_caption("Inspecting Discord.exe") == hud_mod.DEFAULT_PILL_CAPTION
            assert hud_mod._format_pill_caption("UI Tree • notepad.exe") == hud_mod.DEFAULT_PILL_CAPTION
            assert hud_mod._format_pill_caption("Invoke Button • HWND 12345") == hud_mod.DEFAULT_PILL_CAPTION
            assert hud_mod._format_pill_caption("Active") == hud_mod.DEFAULT_PILL_CAPTION
            assert hud_mod._format_pill_caption("3/5 Opening desktop app") == "Antigravity • 3/5 Opening desktop app"
            assert hud_mod.THINKING_PILL_CAPTION == "Antigravity • Thinking..."
            assert hud_mod.WAITING_PILL_CAPTION == "Antigravity • Waiting for model..."
            assert hud_mod.WAITING_DELAY_SEC == 90.0
            assert hud_mod.PILL_SHIMMER_CYCLE_SEC == 2.15
            for cap in (
                "Antigravity • 3/5 Opening desktop app",
                hud_mod.THINKING_PILL_CAPTION,
                hud_mod.WAITING_PILL_CAPTION,
            ):
                frames, _ = hud_mod.build_black_pill_frames(dpi_scale=1.0, num_frames=hud_mod.PILL_SHIMMER_FRAMES, caption=cap)
                assert len(frames) == 28, len(frames)

            windir_sys = Path(os.environ.get("WINDIR") or r"C:\Windows") / "System32"
            if (windir_sys / "notepad.exe").is_file():
                assert server_mod._get_pe_subsystem(windir_sys / "notepad.exe") == 2
            if (windir_sys / "hostname.exe").is_file():
                assert server_mod._get_pe_subsystem(windir_sys / "hostname.exe") == 3
                cli_launch = client.call_tool(
                    "cu_launch_app",
                    {"app": str(windir_sys / "hostname.exe"), "auto_grant": False, "reuse_existing": False},
                )
                assert not cli_launch["isError"], cli_launch
                post_cli_status = client.call_tool("cu_status")
                assert not post_cli_status["isError"], post_cli_status
                assert post_cli_status["structuredContent"]["serverVersion"] == "3.2.0"

            launch_exe = PLUGIN_DIR / "scripts" / "launch_mcp.exe"
            if launch_exe.is_file():
                exe_proc = subprocess.run(
                    ["./scripts/launch_mcp", "--self-test"],
                    executable=str(launch_exe),
                    cwd=str(PLUGIN_DIR),
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=True,
                )
                exe_info = json.loads(exe_proc.stdout.strip())
                assert exe_info.get("server") == "computer-use@3.2.0", exe_info

        # Esc dismissal > 30s ago notifies model with user_closed_computer_use once, then allows clean restart
        test_pid_file = RUNTIME_ROOT / "test_esc_pid.json"
        old_pid_file = server_mod.OVERLAY_PID_FILE
        old_esc_handled = server_mod._LAST_HANDLED_ESC_TS
        old_esc_pending = server_mod._PENDING_USER_CANCEL_TS
        old_grants_esc_bak = dict(server_mod.STATE.get("grants") or {})
        try:
            server_mod.OVERLAY_PID_FILE = test_pid_file
            esc_mock_ts = time.time() - 30.0
            server_mod._LAST_HANDLED_ESC_TS = 0.0
            server_mod._PENDING_USER_CANCEL_TS = 0.0
            test_pid_file.write_text(
                json.dumps({"pid": 999999, "status": "exiting", "reason": "esc", "stoppedAt": esc_mock_ts}),
                encoding="utf-8",
            )
            esc_raised = ""
            try:
                server_mod.check_emergency_stop(check_corner=False)
            except RuntimeError as exc:
                esc_raised = str(exc)
            assert "user_closed_computer_use" in esc_raised, f"Expected user_closed_computer_use, got {esc_raised!r}"
            server_mod.check_emergency_stop(check_corner=False)
        finally:
            server_mod.OVERLAY_PID_FILE = old_pid_file
            server_mod._LAST_HANDLED_ESC_TS = old_esc_handled
            server_mod._PENDING_USER_CANCEL_TS = old_esc_pending
            server_mod.STATE["grants"] = old_grants_esc_bak
            test_pid_file.unlink(missing_ok=True)

        # Unicode matching, strict numeric word boundary, and ISO date preservation
        jp_res = local_matcher_mod.match_ui_element(
            "Click 保存",
            [
                {"index": 0, "controlType": "Button", "name": "キャンセル", "enabled": True},
                {"index": 1, "controlType": "Button", "name": "保存", "enabled": True},
            ],
        )
        assert jp_res["choice"] == "e1" and jp_res["act"] is True, jp_res
        bound_res = local_matcher_mod.match_ui_element(
            "Click '=' until display shows 1",
            [
                {"index": 0, "controlType": "Text", "name": "Display", "value": "14", "enabled": True},
                {"index": 1, "controlType": "Button", "name": "=", "enabled": True},
            ],
        )
        assert bound_res["choice"] == "e1", bound_res
        assert local_matcher_mod.decompose_goal_steps("Select 2026-11-25")[0] == ["Select 2026-11-25"]

        # Grant TTL auto-expiry unit check
        old_grants_backup = dict(server_mod.STATE.get("grants") or {})
        try:
            server_mod.STATE["grants"] = {
                "stale_demo.exe": {
                    "processName": "stale_demo.exe",
                    "mode": "interactive",
                    "grantedEpoch": time.time() - 500.0,
                    "lastUsedEpoch": time.time() - 500.0,
                    "ttlSec": 30.0,
                }
            }
            assert server_mod.prune_expired_grants(save_if_changed=False) is True
            assert "stale_demo.exe" not in server_mod.STATE["grants"]
        finally:
            server_mod.STATE["grants"] = old_grants_backup

        # Static privacy & dependency hygiene: zero hardcoded user paths (Win & macOS), zero unexpected script imports
        mcp_cfg = json.loads((PLUGIN_DIR / "mcp_config.json").read_text(encoding="utf-8"))
        cu_srv_cfg = mcp_cfg["mcpServers"]["computer-use"]
        assert "${PLUGIN_ROOT}" not in cu_srv_cfg["command"] and "${PLUGIN_DATA}" not in cu_srv_cfg["command"], cu_srv_cfg
        assert cu_srv_cfg["command"] == "./scripts/launch_mcp", cu_srv_cfg
        assert cu_srv_cfg["cwd"] == "${PLUGIN_ROOT}", cu_srv_cfg

        win_user_path_re = re.compile(r"[a-zA-Z]:\\{1,2}Users\\{1,2}[A-Za-z0-9_ ]+", re.I)
        mac_user_path_re = re.compile(r"/Users/(?!Shared\b)[A-Za-z0-9._-]+")
        scan_exts = {".py", ".cs", ".ps1", ".cmd", ".swift", ".sh", ".json", ".md", ".txt", ".svg"}
        for rel_target in ("scripts", "skills", "assets", "mcp_config.json", "plugin.json", "README.md", "ARCHITECTURE.md", "SECURITY.md", "tests"):
            p_t = PLUGIN_DIR / rel_target
            if not p_t.exists():
                continue
            files = [p_t] if p_t.is_file() else [
                x for x in p_t.rglob("*")
                if x.is_file()
                and (x.suffix.lower() in scan_exts or x.name == "launch_mcp")
                and ".runtime" not in x.parts
                and "__pycache__" not in x.parts
                and "bin" not in x.parts
            ]
            for fp in files:
                body = fp.read_text(encoding="utf-8", errors="ignore")
                m_win = win_user_path_re.search(body)
                assert m_win is None, f"Hardcoded local Windows user path {m_win.group(0)!r} found in {fp}"
                m_mac = mac_user_path_re.search(body)
                assert m_mac is None, f"Hardcoded local macOS user path {m_mac.group(0)!r} found in {fp}"

        allowed_script_imports = _get_stdlib_module_names() | {
            "PIL",
            "numpy",
            "antigravity_brand",
            "local_matcher",
            "overlay_hud",
            "computer_use_server",
            "win32_backend",
            "macos_backend",
        }
        for py_fp in sorted((PLUGIN_DIR / "scripts").glob("*.py")):
            tree = ast.parse(py_fp.read_text(encoding="utf-8"), filename=str(py_fp))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top_mod = alias.name.split(".")[0]
                        assert top_mod in allowed_script_imports, f"Unexpected import {top_mod!r} in {py_fp}"
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    top_mod = node.module.split(".")[0]
                    assert top_mod in allowed_script_imports, f"Unexpected import {top_mod!r} in {py_fp}"

        results_summary["v3ModalDeadlockSec"] = round(modal_dt, 3)
        results_summary["v3ZeroHardcodeVerified"] = True
        results_summary["v32CrossPlatformInvariantsVerified"] = True

        results_summary["v2SpeedBenchmarks"] = {
            "warmUiTreeMs": warm_uia_ms,
            "hybridObserveWebpSomMs": obs_ms,
            "localMatcherChooseMs": choose_meta["elapsedMs"],
            "localMatcherStepMs": step_meta["elapsedMs"],
            "multiWindowChainBatchMs": chain_meta["elapsedMs"],
            "webpBytes": obs_meta["bytes"],
            "pngBytes": len(raw_png),
            "batchElapsedMs": batch_res["structuredContent"]["elapsedMs"],
        }

    finally:
        try:
            revoke_res = client.call_tool("cu_revoke_app", {"all": True})
            assert not revoke_res["isError"], revoke_res
        finally:
            client.close()
            harness.stop()

    assert STATE_FILE.is_file(), f"Expected state file at {STATE_FILE}"
    final_state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    assert final_state.get("grants") == {}, f"Expected zero leftover grants, got: {final_state.get('grants')}"
    final_ov = json.loads((RUNTIME_ROOT / "overlay_state.json").read_text(encoding="utf-8"))
    assert final_ov.get("active") is False, f"Expected overlay to deactivate after revoke all, got: {final_ov}"
    results_summary["cleanSessionGrantsVerified"] = True

    print(json.dumps({"status": "ALL_TESTS_PASSED", "summary": results_summary}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
