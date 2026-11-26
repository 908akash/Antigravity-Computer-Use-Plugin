---
name: computer-use
description: >-
  Automate native macOS and Windows desktop applications with the Computer Use
  MCP server (v3.2.0). Supports streaming 6 to 16 step cu_batch chapters with clean
  live stage labels and steps_file execution, 120Hz hardware CGEvent (macOS) and
  SendInput (Windows) mouse paths, Retina and per-monitor DPI aware WebP screenshots
  with numbered UI overlays, fast local element matching, and automatic idle dismissal
  under six safety layers. On Turn 1 read SKILL.md and ONLY cu_batch.json in parallel
  because cu_batch wraps all 24 other tools. Prefer the browser subagent for web pages
  and run_command for CLI work.
---

# Computer Use (`v3.2.0`)

Automate native **macOS** and **Windows 10/11** desktop applications safely across any machine with no hardcoded app lists.

- Use the built-in `browser` subagent for web pages and web apps.
- Terminal commands, git, and file edits run directly through `run_command` and file tools.
- Native desktop apps use `cu_batch`, which wraps all 24 other `cu_*` tools plus `wait` for 1 to 80 steps per turn.

---

## Sanctioned MCP transport & OS readiness

When the Computer Use plugin is enabled, the MCP host launches `computer-use` (`scripts/launch_mcp` on macOS, `scripts\launch_mcp.cmd` on Windows) inside your interactive desktop session (`cu_status.desktopName == "Default"`).

- **macOS TCC readiness**: Check `cu_status` if accessibility or screenshots report missing permissions. Both **Accessibility** (`accessibilityTrusted: true`) and **Screen & System Audio Recording** (`screenRecordingGranted: true`) must be enabled manually by the user for Antigravity in **System Settings > Privacy & Security** — never click OS TCC or password prompts programmatically.
- **Live visual indicator**: While apps are granted, the HUD (`overlay_hud_mac` on macOS with `sharingType = .none`, `overlay_hud.py` on Windows with `WDA_EXCLUDEFROMCAPTURE`) shows a click-through top pill (`Antigravity is using your computer` or your clean `stage` label, then `Thinking...` after `1.6s` and `Waiting for model...` after `90s` up to `600s`) and a frosty screen-border glow excluded from screenshots.

> [!IMPORTANT]
> Always call `cu_*` tools through MCP tool calls and never run `computer_use_server.py` through `run_command` to control live user windows. If `cu_*` tools fail to load, ask the user to refresh `computer-use` in MCP Servers or toggle the plugin.

---

## Streaming step-by-step workflow

1. **Read only `cu_batch.json` on Turn 1.**
   Read `<appDataDir>/mcp/computer-use_computer-use/cu_batch.json` in parallel with `SKILL.md` when not already loaded. Do not load every separate `cu_*.json` file unless you need a rare parameter because `cu_batch` executes every `cu_*` tool via `{"action": "<name_without_cu_>", ...}` with automatic `active_hwnd` chaining.
2. **Start fast with a two-step bootstrap.**
   Do not spend a minute computing coordinates before touching the screen. On Turn 1 run a short two-step `cu_batch`:
   ```json
   {"stage": "1/4 Opening app", "steps": [{"action": "launch_app", "app": "<app>"}, {"action": "observe", "format": "webp"}]}
   ```
   `cu_launch_app` discovers installed macOS `.app` bundles and Windows GUI apps dynamically and auto-grants access. `observe` returns numbered `[0..N]` Accessibility (`AXUIElement`) / UI Automation elements, Retina/DPI-calibrated window geometry, and automatically detects open context menus or dropdown popups.
3. **Stream compact visual chapters of 6 to 16 steps.**
   Break multi-step tasks into tight chapters of 6 to 16 steps with a short human-readable `stage` label such as `"2/4 Adjusting toolbar settings"`. Keep `stage` text clean and plain with no `.app`/`.exe` filenames, file paths, or raw window IDs. End each chapter with `{"action": "observe", "format": "webp"}` to verify real visual state.
4. **Use OS-native shortcuts.**
   - On **macOS (`darwin`)**: use `cmd` chords (`cmd+a`, `cmd+c`, `cmd+v`, `cmd+s`, `cmd+z`, `cmd+w`).
   - On **Windows (`win32`)**: use `ctrl` chords (`ctrl+a`, `ctrl+c`, `ctrl+v`, `ctrl+s`, `ctrl+z`, `alt+f4`).
5. **Use `steps_file` for dense calculated coordinates & smooth curves.**
   For many computed waypoints, write JSON step chunks into `<scratch>/chapter_N.json` (1 to 80 steps) and execute with `cu_batch(hwnd=..., stage="3/5 Drawing details", steps_file="<scratch>/chapter_3.json")`. Use `{"action": "drag", "points": [[x,y], ...], "smooth": true, "closed": true}` for smooth 120Hz curves. Clicking inside open context menus or dropdown popups preserves same-process popups without dismissing them.
6. **Always clean up when finished.**
   End every completed task with `{"action": "revoke_app", "all": true}` so grants clear, the HUD dismisses immediately, and session screenshots are purged.

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

- Hard-blocked categories return `policy_blocked`:
  - **macOS**: `Terminal`, `iTerm2`, `Warp`, `Ghostty`, `Alacritty`, `kitty`, `Keychain Access`, `Passwords`, `1Password`, `Bitwarden`, `Activity Monitor`, `SecurityAgent`, `CoreServicesUIAgent`, `System Settings` security/privacy panes, `Antigravity`, `Cursor`, `Claude`, `Codex`, plus `cmd+space`, `cmd+opt+esc`, and `cmd+ctrl+q`.
  - **Windows**: `powershell.exe`, `pwsh.exe`, `cmd.exe`, `wt.exe`, `wsl.exe`, `conhost.exe`, Windows Terminal AUMIDs, password managers, UAC/admin tools, agent hosts, blocked launch arguments (e.g., passing `cmd.exe` to `explorer.exe`), `Win` key chords, `ctrl+shift+esc`, and `ctrl+esc`.
- Browsers (`Safari`, `Arc`, `Google Chrome`, `Brave Browser`, `Firefox`, `Microsoft Edge`) default to `look_only` unless the user explicitly approves `allow_browser_input: true`.
- `cu_zoom` requires a granted `hwnd` (or window-bound `observation_id`) or explicit `allow_full_desktop: true`.
- Sensitive editor protection returns `sensitive_file_guard` when typing into editors open to `.env`, SSH keys, `.bashrc`, `.zshrc`, `.zprofile`, `.git/hooks`, MCP configs, or executable scripts (`.sh`, `.zsh`, `.bat`, `.cmd`, `.ps1`, `.reg`) while allowing regular source files like `.py`, `.js`, or `.ts`.
- If any call returns `user_closed_computer_use`, the user pressed `Esc` to close Computer Use and all active grants were revoked immediately. Stop automating right away and check with the user; call `cu_launch_app` or `cu_grant_app` to restart cleanly when authorized.
- Ask user confirmation before sending messages or emails, deleting files, or submitting irreversible forms or financial actions. Stop immediately on `emergency_stop` (pointer moved to monitor top-left corner), `desktop_locked`, or `elevated_window_uipi`.
