---
name: computer-use
description: >-
  Automate native Windows 10 and 11 desktop applications with the Windows Computer Use
  MCP server (v3.1.0). Supports streaming 6 to 16 step cu_batch chapters with clean live
  stage labels and steps_file execution and 120Hz hardware SendInput mouse paths and popup
  aware WebP screenshots with numbered UI overlays and fast local element matching and
  automatic idle dismissal with six safety layers. On Turn 1 read SKILL.md and ONLY
  cu_batch.json in parallel because cu_batch wraps all 24 other tools. Prefer the browser
  subagent for web pages and run_command for CLI work.
---

# Windows Computer Use (`v3.1.0`)

Automate native Windows 10 and 11 desktop applications safely across any machine with no hardcoded app lists.

- Use the built in `browser` subagent for web pages and web apps.
- Terminal commands and git and file edits run directly through `run_command` and file tools.
- Native Windows desktop apps use `cu_batch` which wraps all 24 other `cu_*` tools plus `wait` for 1 to 80 steps per turn.

---

## Sanctioned MCP transport

When the Windows Computer Use plugin is enabled the MCP host launches `computer-use-win32` inside your interactive Windows desktop session (`cu_status.desktopName == "Default"`). While apps are granted `overlay_hud.py` shows a click through top pill (`Antigravity is using your computer` or your clean `stage` label then `Thinking...` after `1.6`s and `Waiting for model...` after `90`s up to `600`s) along with a screen border glow and hardware dart cursor excluded from screenshots via `WDA_EXCLUDEFROMCAPTURE`.

> [!IMPORTANT]
> Always call `cu_*` tools through MCP tool calls and never run `computer_use_server.py` through `run_command` to control live user windows. Shell commands run inside an isolated sandbox desktop that cannot reach user windows. If `cu_*` tools fail to load ask the user to refresh `computer-use-win32` in MCP Servers or toggle the plugin.

---

## Streaming step by step workflow

1. Read only `cu_batch.json` on Turn 1.
   Read `<appDataDir>/mcp/computer-use_computer-use-win32/cu_batch.json` in parallel with `SKILL.md` when not already loaded. Do not load every separate `cu_*.json` file unless you need a specific rare parameter because `cu_batch` executes every `cu_*` tool via `{"action": "<name_without_cu_>", ...}` with automatic `active_hwnd` chaining.
2. Start fast with a two step bootstrap.
   Do not spend a minute computing dozens of coordinates before touching the screen. On Turn 1 run a short two step `cu_batch`:
   ```json
   {"stage": "1/4 Opening app", "steps": [{"action": "launch_app", "app": "<app>"}, {"action": "observe", "format": "webp"}]}
   ```
   `cu_launch_app` discovers any installed GUI app dynamically and prefers Windows GUI binaries over same name CLI tools and auto grants access. `observe` returns numbered `[0..N]` UI Automation elements and window geometry and automatically detects open context menus or dropdown popups.
3. Stream compact visual chapters of 6 to 16 steps.
   Break multi step tasks like drawing or filling forms or configuring dialogs into tight chapters of 6 to 16 steps with a short human readable `stage` label such as `"2/4 Adjusting toolbar settings"`. Keep `stage` text clean and plain with no `.exe` filenames or raw window handles. End each chapter with `{"action": "observe", "format": "webp"}` so you see real visual state before deciding the next chapter.
4. Use `steps_file` for dense calculated coordinates.
   When a task needs many computed waypoints such as geometric art or repetitive grids generate JSON step chunks into `<scratch>/chapter_N.json` (1 to 80 steps per file) with Python and run each file cleanly with `cu_batch(hwnd=..., stage="3/5 Drawing details", steps_file="<scratch>/chapter_3.json")`.
5. Use smooth multi point paths for curves and popups carefully.
   Use `{"action": "drag", "points": [[x,y], ...], "smooth": true, "closed": true}` to trace smooth strokes or fillable closed shapes in one continuous 120Hz hardware `SendInput` motion. When a dropdown or right click context menu opens call `observe` or click directly inside the popup because `cu_click` preserves same process popups without dismissing them.
6. Always clean up when finished.
   End every completed task with `{"action": "revoke_app", "all": true}` so grants clear and the overlay dismisses right away and session screenshots are purged.

---

## Complete MCP tool reference

All 25 tools can be invoked inside `cu_batch`:

| Group | Tools and key arguments |
| :--- | :--- |
| Universal batch | `cu_batch`: `{ hwnd?, stage?, compact?, stop_on_error?, steps?, steps_file? }` runs 1 to 80 steps inline or from a JSON file with `active_hwnd` chaining and live HUD stage text |
| Discovery and access | `cu_status` `cu_list_windows` `cu_wait_for_window` `cu_grant_app` `cu_revoke_app({"all": true})` `cu_launch_app({app, auto_grant?, allow_browser_input?, reuse_existing?, args?})` |
| Screen and UI inspection | `cu_observe({hwnd, format?: "webp", max_width?: 1280, annotate?: true, max_elements?: 100})` `cu_screenshot` `cu_zoom({x, y, width?, height?, upscale?: 2, observation_id?, hwnd?, allow_full_desktop?})` `cu_ui_tree` |
| Elements and focus | `cu_focus_window` `cu_click_element({observation_id, element_index, prefer_uia_invoke?})` `cu_set_value({observation_id, element_index, value, prefer_background_msg?})` |
| Pointer and keyboard | `cu_click` `cu_drag({points?, from_x..to_y, closed?, smooth?, duration?, hold_ms?, coordinate_space?})` `cu_move` `cu_scroll({x, y, clicks?, horizontal?})` `cu_type` `cu_key` |
| Clipboard and HUD | `cu_clipboard_read({allow_clipboard: true})` `cu_clipboard_write({text})` `cu_overlay({active, label?, cursor_style?, ttl_sec?})` |
| Fast local matching | `cu_choose({goal, observation_id?, elements?, confidence_floor?: 0.80})` `cu_step({hwnd, goal?, goals?, verify_contains?, prefer_uia_invoke?})` |

---

## Safety and recovery rules

- Hard blocked categories return `policy_blocked`: terminals and shells (`powershell.exe` or `pwsh.exe` or `cmd.exe` or `wt.exe` or `wsl.exe` or `conhost.exe` or Windows Terminal AUMIDs) along with password managers and admin or UAC tools and agent hosts and blocked launch arguments (such as passing `cmd.exe` to `explorer.exe`) and `Win` key chords or `ctrl+shift+esc` or `ctrl+esc`.
- Browsers default to `look_only` unless the user explicitly approves `allow_browser_input: true`.
- `cu_zoom` requires a granted `hwnd` (or window bound `observation_id`) or explicit `allow_full_desktop: true`.
- Sensitive editor protection returns `sensitive_file_guard` when typing into editors open to shell profiles or SSH keys or `.git/hooks` or MCP configs or executable scripts (`.bat` or `.cmd` or `.reg` or `.vbs` or `.ps1`) but allows regular source files like `.js` or `.py`.
- If any call returns `user_closed_computer_use` the user pressed `Esc` to close Computer Use and every active grant was revoked immediately. Stop automating right away and check with the user if they interrupted mid task. When the user asks to resume or authorizes desktop control again call `cu_launch_app` or `cu_grant_app` to restart Computer Use cleanly.
- Ask user confirmation before sending messages or emails or deleting files or submitting irreversible forms or financial actions. Stop immediately on `emergency_stop` (pointer moved to monitor top left corner) or `desktop_locked` or `elevated_window_uipi`.
