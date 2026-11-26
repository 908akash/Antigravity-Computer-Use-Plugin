"""End-to-end verification suite for Windows Computer Use MCP server (v3.1.0).

Starts a dark-themed multi-control Tkinter Win32 window on a dedicated GUI thread,
spawns `computer_use_server.py` over stdio MCP JSON-RPC 2.0, and verifies:
  1. initialize + notifications/initialized + ping + tools/list (all 25 tools)
  2. cu_status + cu_list_windows + cu_wait_for_window
  3. cu_grant_app + defense-in-depth blocks (powershell/cmd/wsl deny-list with &
     without .exe, Terminal AUMID block, Win-key chord block, browser look_only,
     sensitive editor guard blocking mcp_config.json while allowing .js files)
  4. cu_ui_tree + cu_set_value + cu_click_element (live Tkinter state updates)
  5. cu_screenshot (Win32 PrintWindow background capture -> non-blank PNG) + cu_zoom
  6. cu_clipboard_write + cu_clipboard_read CF_UNICODETEXT round-trip & guards
  7. HUD pill caption sanitization, constant 2.15s shimmer, execution/idle IPC,
     Unicode/boundary local matcher checks, CLI stdout pipe isolation, grant TTL
     auto-expiry, zero-user-path/AST-import static scan, and clean grant cleanup.
"""

from __future__ import annotations

import ast
import base64
import ctypes
from ctypes import wintypes
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
import tkinter as tk
from typing import Any

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
for _rt_base in (RUNTIME_ROOT, PLUGIN_DIR / ".runtime"):
    if _rt_base.is_dir():
        _exact = _rt_base / _py_tag
        if _exact.is_dir():
            _pylibs_candidates.append(_exact)
        for _cand in sorted(_rt_base.glob("pylibs_cp*")):
            if _cand.is_dir() and _cand not in _pylibs_candidates:
                _pylibs_candidates.append(_cand)
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
            text="Antigravity Windows 11 Computer Use",
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

        # Expose descriptive Win32 window captions on child HWNDs for WM_GETTEXT inspection
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

        # Destroy all Tk widgets on the GUI thread to avoid Tcl_AsyncDelete warnings
        del status_var, status_banner, apply_btn, ping_btn, entry, btn_row, body, header
        root.destroy()
        del root


class McpStdioClient:
    """Minimal JSON-RPC 2.0 stdio client for computer_use_server.py."""

    def __init__(self, python_exe: str, server_script: Path) -> None:
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
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


def main() -> int:
    harness = TkinterHarness()
    harness.start()
    client = McpStdioClient(sys.executable, SERVER_SCRIPT)
    results_summary: dict[str, Any] = {}

    try:
        # 1. initialize + notifications/initialized + ping + tools/list (strict MCP lifecycle order)
        init_resp = client.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "cua-e2e-test", "version": "1.0.0"},
            },
        )
        server_info = init_resp["result"]["serverInfo"]
        assert server_info["name"] == "computer-use-win32"
        client.notify("notifications/initialized", {})
        ping_resp = client.request("ping")
        assert ping_resp["result"] == {}

        # Clean starting state after handshake
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
        results_summary["toolsVerified"] = len(tools)

        # 2. cu_status + cu_list_windows + cu_wait_for_window
        status_res = client.call_tool("cu_status")
        assert not status_res["isError"], status_res
        status_data = status_res["structuredContent"]
        assert status_data["dpiAware"] == "PerMonitorV2"
        assert status_data["desktopLocked"] is False

        snap0 = harness.snapshot()
        target_hwnd = snap0["hwnd"]
        assert target_hwnd > 0

        wait_res = client.call_tool(
            "cu_wait_for_window",
            {
                "title_contains": "Antigravity CUA Verification",
                "process_name": "python.exe",
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

        for blocked_proc in ("powershell.exe", "cmd.exe", "Antigravity.exe", "1Password.exe"):
            blk = client.call_tool("cu_grant_app", {"process_name": blocked_proc})
            assert blk["isError"] is True, f"Expected {blocked_proc} to be blocked"
            assert "policy_blocked" in blk["content"][0]["text"]

        browser_grant = client.call_tool("cu_grant_app", {"process_name": "chrome.exe"})
        assert not browser_grant["isError"], browser_grant
        assert browser_grant["structuredContent"]["mode"] == "look_only"

        grant_res = client.call_tool("cu_grant_app", {"hwnd": target_hwnd})
        assert not grant_res["isError"], grant_res
        assert grant_res["structuredContent"]["mode"] == "interactive"

        winkey_res = client.call_tool("cu_key", {"hwnd": target_hwnd, "keys": "win+r"})
        assert winkey_res["isError"] is True
        assert "policy_blocked" in winkey_res["content"][0]["text"]

        for blocked_chord in ("ctrl+shift+esc", "ctrl+esc"):
            blk_chord_res = client.call_tool("cu_key", {"hwnd": target_hwnd, "keys": blocked_chord})
            assert blk_chord_res["isError"] is True, f"Expected {blocked_chord} to be blocked"
            assert "policy_blocked" in blk_chord_res["content"][0]["text"]

        explorer_cmd_res = client.call_tool("cu_launch_app", {"app": "explorer.exe", "args": ["cmd.exe"]})
        assert explorer_cmd_res["isError"] is True
        assert "policy_blocked" in explorer_cmd_res["content"][0]["text"]

        # Sensitive editor title guard (unit + live end-to-end over MCP)
        unit_sens = server_mod.check_sensitive_editor_title("notepad.exe", "mcp_config.json - Notepad")
        assert unit_sens is not None and "sensitive_file_guard" in unit_sens
        assert server_mod.check_sensitive_editor_title("notepad.exe", "notes.txt - Notepad") is None

        harness.set_title("mcp_config.json - Notepad")
        try:
            sens_live = client.call_tool("cu_type", {"hwnd": target_hwnd, "text": "injected"})
            assert sens_live["isError"] is True
            assert "sensitive_file_guard" in sens_live["content"][0]["text"]
        finally:
            harness.set_title(TkinterHarness.WINDOW_TITLE)
        results_summary["securityGuardsVerified"] = True

        # 4. cu_ui_tree + cu_set_value + cu_click_element
        tree_res = client.call_tool("cu_ui_tree", {"hwnd": target_hwnd})
        assert not tree_res["isError"], tree_res
        tree_data = tree_res["structuredContent"]
        tree_obs_id = tree_data["observationId"]
        elements = tree_data["elements"]
        assert len(elements) >= 3, f"Expected >=3 child elements in Tkinter window, got {elements}"

        entry_aid = f"hwnd:{snap0['entry_hwnd']}"
        apply_aid = f"hwnd:{snap0['apply_btn_hwnd']}"
        entry_el = next((e for e in elements if e.get("automationId") == entry_aid), None)
        apply_el = next(
            (e for e in elements if e.get("automationId") == apply_aid or e.get("name") == "Apply Action"),
            None,
        )
        assert entry_el is not None, f"Entry {entry_aid} not found in UI tree: {elements}"
        assert apply_el is not None, f"Apply button {apply_aid} not found in UI tree: {elements}"

        unicode_name = "O\u2019Brien"
        set_uni_res = client.call_tool(
            "cu_set_value",
            {
                "observation_id": tree_obs_id,
                "element_index": entry_el["index"],
                "value": unicode_name,
            },
        )
        assert not set_uni_res["isError"], set_uni_res
        deadline_u = time.monotonic() + 2.0
        while time.monotonic() < deadline_u:
            if harness.snapshot()["entry_value"] == unicode_name:
                break
            time.sleep(0.03)
        assert harness.snapshot()["entry_value"] == unicode_name, (
            f"Expected Tkinter Entry '{unicode_name}', got '{harness.snapshot()['entry_value']}'"
        )

        target_text = "Antigravity Win32 CUA Verified!"
        set_res = client.call_tool(
            "cu_set_value",
            {
                "observation_id": tree_obs_id,
                "element_index": entry_el["index"],
                "value": target_text,
            },
        )
        assert not set_res["isError"], set_res

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if harness.snapshot()["entry_value"] == target_text:
                break
            time.sleep(0.03)
        snap_after_set = harness.snapshot()
        assert snap_after_set["entry_value"] == target_text, (
            f"Expected Tkinter Entry '{target_text}', got '{snap_after_set['entry_value']}'"
        )

        click_res = client.call_tool(
            "cu_click_element",
            {
                "observation_id": tree_obs_id,
                "element_index": apply_el["index"],
            },
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

        # Give Tkinter 60ms to repaint updated emerald banner before PrintWindow capture
        time.sleep(0.06)

        # 5. cu_screenshot (Win32 PrintWindow) + cu_zoom
        shot_res = client.call_tool("cu_screenshot", {"hwnd": target_hwnd})
        assert not shot_res["isError"], shot_res
        shot_meta = shot_res["structuredContent"]
        assert shot_meta["captureMethod"] == "print_window_background", shot_meta
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
                "x": 24,
                "y": 24,
                "width": 280,
                "height": 160,
                "upscale": 2,
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
            "savedPath": str(LATEST_VERIFIED_PNG),
            "zoomSize": [zoom_img.width, zoom_img.height],
        }

        # 6. cu_clipboard_write + cu_clipboard_read round-trip & guards
        clip_guard = client.call_tool("cu_clipboard_read", {})
        assert clip_guard["isError"] is True
        assert "clipboard_permission_required" in clip_guard["content"][0]["text"]

        clip_long = client.call_tool("cu_clipboard_write", {"text": "x" * 16001})
        assert clip_long["isError"] is True
        assert "text_too_long" in clip_long["content"][0]["text"]

        clip_payload = "Antigravity Clipboard Round-Trip ✓ — 64-bit CF_UNICODETEXT (2026)"
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

        # 6b. Screen-border glow HUD overlay state & UpdateLayeredWindow self-test
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

        hud_proc = subprocess.run(
            [sys.executable, str(PLUGIN_DIR / "scripts" / "overlay_hud.py"), "--self-test"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        hud_info = json.loads(hud_proc.stdout.strip())
        assert hud_info.get("ok") is True and int(hud_info.get("hwnd", 0)) > 0, hud_info
        results_summary["overlayHudVerified"] = hud_info

        # 6c. v2.0 Speed & Efficiency Verification: warm cached UIA latency, cu_observe (WebP + SoM), and cu_batch
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

        # 6d. v2.2 Fast local element matcher (`cu_choose` + `cu_step`) verification
        choose_res = client.call_tool(
            "cu_choose",
            {"goal": "Click Secondary Ping", "observation_id": obs_meta["observationId"]},
        )
        assert not choose_res["isError"], choose_res
        choose_meta = choose_res["structuredContent"]
        assert choose_meta["act"] is True and choose_meta["elementIndex"] == ping_el["index"], choose_meta

        prev_pings = harness.snapshot()["ping_clicks"]
        step_res = client.call_tool(
            "cu_step",
            {"hwnd": target_hwnd, "goal": "Click 'Secondary Ping'"},
        )
        assert not step_res["isError"], step_res
        step_meta = step_res["structuredContent"]
        assert step_meta["status"] == "completed_steps", step_meta
        deadline_j = time.monotonic() + 1.5
        while time.monotonic() < deadline_j and harness.snapshot()["ping_clicks"] <= prev_pings:
            time.sleep(0.02)
        assert harness.snapshot()["ping_clicks"] >= prev_pings + 1

        # 6e. v2.3.0 Role disambiguation + multi-window active_hwnd cu_batch chaining
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
                    {"action": "wait_for_window", "title_contains": "Antigravity CUA Verification", "process_name": "python.exe", "timeout_sec": 3.0},
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

        # 6f. v2.4.0 Multi-goal decomposition + 45-button Calculator softmax calibration check
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

        # 6g. v2.5.0 Multi-point polyline/Catmull-Rom cu_drag inside cu_batch + 89-element Win11 Paint matcher checks
        poly_batch = client.call_tool(
            "cu_batch",
            {
                "hwnd": target_hwnd,
                "steps": [
                    {
                        "action": "drag",
                        "coordinate_space": "window",
                        "points": [[60, 220], [120, 190], [180, 220], [240, 195]],
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
            ("Click Edit colors", 43),
            ("Click Select tool", 25),
            ("Click Gray", 70),
            ("Click Yellow swatch", 74),
            ("Click sky blue", 86),
            ("Click Size slider", 13),
            ("Click Size button", 40),
        ]:
            pr = client.call_tool("cu_choose", {"goal": p_goal, "elements": paint_elems})
            assert not pr["isError"] and pr["structuredContent"]["elementIndex"] == exp_idx and pr["structuredContent"]["act"] is True and pr["structuredContent"]["confidence"] >= 0.85, (p_goal, pr)

        # Tier-1 isolated zero-hardcode check: unrelated conceptual synonyms return unsure without LLM Tier-2
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

        # Non-loopback CUA_MATCHER_ENDPOINT is rejected unless CUA_ALLOW_REMOTE_MATCHER is set
        prev_ep_env = os.environ.get("CUA_MATCHER_ENDPOINT")
        prev_remote_env = os.environ.get("CUA_ALLOW_REMOTE_MATCHER")
        os.environ["CUA_MATCHER_ENDPOINT"] = "http://example.com:11434"
        os.environ.pop("CUA_ALLOW_REMOTE_MATCHER", None)
        try:
            assert local_matcher_mod._discover_local_server() is None
        finally:
            if prev_ep_env is None:
                os.environ.pop("CUA_MATCHER_ENDPOINT", None)
            else:
                os.environ["CUA_MATCHER_ENDPOINT"] = prev_ep_env
            if prev_remote_env is not None:
                os.environ["CUA_ALLOW_REMOTE_MATCHER"] = prev_remote_env

        # 6h. v3.0.0 streaming stage HUD + steps_file + modal-disabled deadlock (<1.5s) + zero-hardcode scan
        steps_file_path = RUNTIME_ROOT / "test_batch_chapter.json"
        steps_file_path.write_text(
            json.dumps([
                {"action": "drag", "coordinate_space": "window", "points": [[70, 210], [140, 185], [210, 215]], "smooth": True, "duration": 0.03},
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

        # Modal-disabled owner window must NOT hang PrintWindow or UIA
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
        assert not modal_obs["isError"], modal_obs
        assert modal_dt < 1.5, f"Modal-disabled window observe took {modal_dt:.2f}s (expected <1.5s)"

        assert server_mod._resolve_vk_token("+")[0] == 0xBB

        # 6i. v3.1.0 production invariants: HUD caption/shimmer, security hardening, Unicode/boundary matcher, CLI pipe isolation, grant TTL
        import overlay_hud as hud_mod

        assert hud_mod._format_pill_caption("Inspecting Discord.exe") == hud_mod.DEFAULT_PILL_CAPTION
        assert hud_mod._format_pill_caption("UI Tree • notepad.exe") == hud_mod.DEFAULT_PILL_CAPTION
        assert hud_mod._format_pill_caption("Invoke Button • HWND 12345") == hud_mod.DEFAULT_PILL_CAPTION
        assert hud_mod._format_pill_caption("Active") == hud_mod.DEFAULT_PILL_CAPTION
        assert hud_mod._format_pill_caption("3/5 Opening desktop app") == "Antigravity • 3/5 Opening desktop app"
        assert hud_mod.THINKING_PILL_CAPTION == "Antigravity • Thinking..."
        assert hud_mod.WAITING_PILL_CAPTION == "Antigravity • Waiting for model..."
        assert hud_mod.WAITING_DELAY_SEC == 90.0
        assert hud_mod.PILL_SHIMMER_CYCLE_SEC == 2.15
        stage_pill_frames, _ = hud_mod.build_black_pill_frames(
            dpi_scale=1.0,
            num_frames=hud_mod.PILL_SHIMMER_FRAMES,
            caption="Antigravity • 3/5 Opening desktop app",
        )
        assert len(stage_pill_frames) == 28, len(stage_pill_frames)
        thinking_pill_frames, _ = hud_mod.build_black_pill_frames(
            dpi_scale=1.0,
            num_frames=hud_mod.PILL_SHIMMER_FRAMES,
            caption=hud_mod.THINKING_PILL_CAPTION,
        )
        assert len(thinking_pill_frames) == 28, len(thinking_pill_frames)
        waiting_pill_frames, _ = hud_mod.build_black_pill_frames(
            dpi_scale=1.0,
            num_frames=hud_mod.PILL_SHIMMER_FRAMES,
            caption=hud_mod.WAITING_PILL_CAPTION,
        )
        assert len(waiting_pill_frames) == 28, len(waiting_pill_frames)

        # Esc dismissal > 30s ago notifies model with user_closed_computer_use once, then allows clean restart
        pid_file_bak = server_mod.OVERLAY_PID_FILE.read_text(encoding="utf-8") if server_mod.OVERLAY_PID_FILE.is_file() else None
        old_esc_handled = server_mod._LAST_HANDLED_ESC_TS
        old_esc_pending = server_mod._PENDING_USER_CANCEL_TS
        try:
            esc_mock_ts = time.time() - 30.0
            server_mod._LAST_HANDLED_ESC_TS = 0.0
            server_mod._PENDING_USER_CANCEL_TS = 0.0
            server_mod.OVERLAY_PID_FILE.write_text(
                json.dumps({"pid": 999999, "status": "exiting", "reason": "esc", "stoppedAt": esc_mock_ts}),
                encoding="utf-8",
            )
            esc_raised = ""
            try:
                server_mod.check_emergency_stop(check_corner=False)
            except RuntimeError as exc:
                esc_raised = str(exc)
            assert "user_closed_computer_use" in esc_raised, f"Expected user_closed_computer_use, got {esc_raised!r}"
            # Subsequent call (restart) must proceed cleanly without sticking
            server_mod.check_emergency_stop(check_corner=False)
        finally:
            server_mod._LAST_HANDLED_ESC_TS = old_esc_handled
            server_mod._PENDING_USER_CANCEL_TS = old_esc_pending
            if pid_file_bak is not None:
                server_mod.OVERLAY_PID_FILE.write_text(pid_file_bak, encoding="utf-8")
            else:
                server_mod.OVERLAY_PID_FILE.unlink(missing_ok=True)

        # Policy hardening: extensionless blocked names, UWP Terminal AUMID, regular .js editor allowed, schema updates
        for no_ext_blk in ("powershell", "wsl", "cmd"):
            blk_no_ext = client.call_tool("cu_grant_app", {"process_name": no_ext_blk})
            assert blk_no_ext["isError"] is True and "policy_blocked" in blk_no_ext["content"][0]["text"], blk_no_ext
        assert server_mod._is_blocked_launch_target(
            "Terminal",
            ["explorer.exe", "shell:AppsFolder\\Microsoft.WindowsTerminal_8wekyb3d8bbwe!App"],
        ) is not None
        assert server_mod.check_sensitive_editor_title("code.exe", "index.js - Visual Studio Code") is None
        assert by_name["cu_overlay"]["annotations"]["readOnlyHint"] is False
        assert "horizontal" in by_name["cu_scroll"]["inputSchema"]["properties"]

        # PE subsystem classifier & CLI stdout pipe isolation ('┌' crash regression guard)
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
            assert post_cli_status["structuredContent"]["serverVersion"] == "3.1.0"

        # Unicode matching, strict numeric word boundary, and ISO date preservation in local_matcher.py
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

        # Grant TTL auto-expiry unit verification (isolated without mutating active stdio server grants)
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

        # Static privacy & dependency hygiene: zero hardcoded user paths, zero unexpected script imports
        user_path_re = re.compile(r"[a-zA-Z]:\\{1,2}Users\\{1,2}[A-Za-z0-9_ ]+", re.I)
        scan_exts = {".py", ".cs", ".ps1", ".cmd", ".json", ".md", ".txt", ".svg"}
        for rel_target in ("scripts", "skills", "mcp_config.json", "plugin.json", "README.md", "ARCHITECTURE.md", "SECURITY.md", "tests"):
            p_t = PLUGIN_DIR / rel_target
            if not p_t.exists():
                continue
            files = [p_t] if p_t.is_file() else [
                x for x in p_t.rglob("*")
                if x.is_file()
                and x.suffix.lower() in scan_exts
                and ".runtime" not in x.parts
                and "__pycache__" not in x.parts
            ]
            for fp in files:
                body = fp.read_text(encoding="utf-8", errors="ignore")
                m_path = user_path_re.search(body)
                assert m_path is None, f"Hardcoded local Windows user path {m_path.group(0)!r} found in {fp}"

        allowed_script_imports = sys.stdlib_module_names | {
            "PIL",
            "numpy",
            "antigravity_brand",
            "local_matcher",
            "overlay_hud",
            "computer_use_server",
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
        results_summary["v31ProductionInvariantsVerified"] = True

        results_summary["v2SpeedBenchmarks"] = {
            "warmUiaTreeMs": warm_uia_ms,
            "hybridObserveWebpSomMs": obs_ms,
            "localMatcherChooseMs": choose_meta["elapsedMs"],
            "localMatcherStepMs": step_meta["elapsedMs"],
            "multiWindowChainBatchMs": chain_meta["elapsedMs"],
            "webpBytes": obs_meta["bytes"],
            "pngBytes": len(raw_png),
            "batchElapsedMs": batch_res["structuredContent"]["elapsedMs"],
        }

    finally:
        # 7. Always revoke all session grants so session_state.json is left clean
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
