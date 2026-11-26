# Antigravity Computer Use Plugin (`v3.2.0`)

Antigravity Computer Use Plugin is an open-source **macOS and Windows 10/11** desktop automation plugin and MCP server (`computer-use`).

One single repository and one single release archive (`computer-use-v3.2.0.zip`) work natively on both **macOS** (compiled Swift `AXUIElement`, CoreGraphics `CGEvent`, Retina-calibrated `screencapture`, and click-through AppKit HUD) and **Windows 10/11** (pure Win32 `ctypes`, `PrintWindow`, `SendInput`, and Windows UI Automation) with zero third-party OS wrappers and no hardcoded app lists.

Read [ARCHITECTURE.md](./ARCHITECTURE.md) for the internals, [`skills/computer-use/SKILL.md`](./skills/computer-use/SKILL.md) for the agent workflow runbook, and [SECURITY.md](./SECURITY.md) for the security model.

---

## Install in Antigravity

### One-prompt install (auto-detects macOS or Windows)

Paste this single prompt into Antigravity on either a Mac or a Windows PC and your AI agent will detect your operating system and install and verify **Computer Use** automatically:

```text
Read https://github.com/908akash/Antigravity-Computer-Use-Plugin and install the Computer Use plugin for my current OS from https://github.com/908akash/Antigravity-Computer-Use-Plugin/releases/latest/download/computer-use-v3.2.0.zip (or by cloning the repository if the release zip is unavailable), run its OS self-test so all dependencies and native helpers are ready, tell me if any OS permissions need to be enabled, and give me the [Computer Use](customization://computer-use) link so I can turn it on.
```

> **Agent auto-install instructions (when reading this repository):**
> 1. **Detect host OS** (`darwin` = macOS, `win32` = Windows 10/11).
> 2. **Install into the Antigravity plugins folder**:
>    - **macOS**: extract `computer-use-v3.2.0.zip` into `~/.gemini/config/plugins/` (creating `~/.gemini/config/plugins/computer-use` with `chmod +x ~/.gemini/config/plugins/computer-use/scripts/launch_mcp`), or `git clone` directly to `~/.gemini/config/plugins/computer-use`.
>    - **Windows**: extract `computer-use-v3.2.0.zip` into `%USERPROFILE%\.gemini\config\plugins\` (creating `%USERPROFILE%\.gemini\config\plugins\computer-use`), or `git clone` directly to `%USERPROFILE%\.gemini\config\plugins\computer-use`.
> 3. **Bootstrap & verify**:
>    - **macOS**: run `~/.gemini/config/plugins/computer-use/scripts/launch_mcp --self-test` (installs `pillow` + `numpy` into the local runtime cache and compiles the Swift `AXUIElement` & HUD helpers). If `accessibilityTrusted` or `screenRecordingGranted` is `false`, instruct the user to toggle **Antigravity** on under **System Settings > Privacy & Security > Accessibility** and **Screen & System Audio Recording** (and **Quit & Reopen** if macOS prompts).
>    - **Windows**: run `"%USERPROFILE%\.gemini\config\plugins\computer-use\scripts\launch_mcp.cmd" --self-test` (installs `pillow` + `numpy` into the local runtime cache and verifies Win32 Per-Monitor DPI v2 readiness).
> 4. **Hand back the toggle link**: share `[Computer Use](customization://computer-use)` so the user can enable the plugin and refresh MCP servers.

### macOS permissions (one-time setup)

On macOS, grant **Antigravity** two standard Privacy & Security permissions in **System Settings > Privacy & Security**:
1. **Accessibility** — enables native `AXUIElement` tree inspection and 120Hz `CGEvent` mouse/keyboard control.
2. **Screen & System Audio Recording** — enables shadow-free Retina window screenshots and `cu_observe`.

Run `cu_status` at any time to verify `accessibilityTrusted: true` and `screenRecordingGranted: true`. The plugin runs with stock macOS Apple Command Line Tools (`/usr/bin/python3` 3.9.6 + `xcrun swiftc`) and does **not** require Homebrew.

### One-line CLI install

**macOS (Terminal / zsh):**
```sh
curl -fsSL "https://github.com/908akash/Antigravity-Computer-Use-Plugin/releases/latest/download/computer-use-v3.2.0.zip" -o /tmp/computer-use-v3.2.0.zip && mkdir -p "$HOME/.gemini/config/plugins" && unzip -oq /tmp/computer-use-v3.2.0.zip -d "$HOME/.gemini/config/plugins" && chmod +x "$HOME/.gemini/config/plugins/computer-use/scripts/launch_mcp" && "$HOME/.gemini/config/plugins/computer-use/scripts/launch_mcp" --self-test
```

**Windows (PowerShell):**
```powershell
$zip = "$env:TEMP\computer-use-v3.2.0.zip"; Invoke-WebRequest "https://github.com/908akash/Antigravity-Computer-Use-Plugin/releases/latest/download/computer-use-v3.2.0.zip" -OutFile $zip; Expand-Archive -Path $zip -DestinationPath "$env:USERPROFILE\.gemini\config\plugins" -Force; & "$env:USERPROFILE\.gemini\config\plugins\computer-use\scripts\launch_mcp.cmd" --self-test
```

### Install with git clone

**macOS:**
```sh
git clone https://github.com/908akash/Antigravity-Computer-Use-Plugin.git "$HOME/.gemini/config/plugins/computer-use"
"$HOME/.gemini/config/plugins/computer-use/scripts/launch_mcp" --self-test
```

**Windows:**
```powershell
git clone https://github.com/908akash/Antigravity-Computer-Use-Plugin.git "$env:USERPROFILE\.gemini\config\plugins\computer-use"
& "$env:USERPROFILE\.gemini\config\plugins\computer-use\scripts\launch_mcp.cmd" --self-test
```

### Standalone MCP clients

Claude Desktop, Cursor, and VS Code can run the server directly using explicit folder paths in `cwd`:

**macOS:**
```json
{
  "mcpServers": {
    "computer-use": {
      "command": "/path/to/computer-use/scripts/launch_mcp",
      "args": [],
      "cwd": "/path/to/computer-use",
      "env": {
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8"
      }
    }
  }
}
```

**Windows:**
```json
{
  "mcpServers": {
    "computer-use": {
      "command": "cmd.exe",
      "args": ["/d", "/c", "scripts\\launch_mcp.cmd"],
      "cwd": "C:\\path\\to\\computer-use",
      "env": {
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8"
      }
    }
  }
}
```

Do not copy `${PLUGIN_ROOT}` or `${PLUGIN_DATA}` placeholders from Antigravity's `mcp_config.json` into standalone MCP clients.

---

## How it works on macOS and Windows

### Native OS backends with zero wrapper bloat

- **macOS (`scripts/macos_backend.py` + Swift helpers)**:
  - On first run compiles `scripts/macos_ax_bridge.swift` and `scripts/overlay_hud_mac.swift` locally via `xcrun swiftc -O` into `$CUA_RUNTIME_DIR/bin/` keyed by `SHA-256` (with `osascript` JXA fallback if Command Line Tools are missing).
  - Shadow-free Retina window capture uses `/usr/sbin/screencapture -x -o -l <CGWindowID>` while automatically scaling 2x Retina image pixels back to exact 1x Quartz logical points (`scaleX` / `scaleY`) for `cu_click`, `cu_drag`, `cu_zoom`, and Set-of-Mark overlays.
  - Pointer motion, Catmull-Rom curves, drags, scrolls, Unicode text, and `Cmd` shortcuts run at 120Hz via CoreGraphics `CGEvent` inside the persistent Swift bridge.
  - App discovery scans `/Applications`, `~/Applications`, `/System/Applications`, and Spotlight (`mdfind`) and launches detached via `open -a`.
  - Same-PID context menus, `NSPopUpButton` dropdowns, and sheets are preserved on clicks without re-activating the parent window.
- **Windows 10/11 (`scripts/win32_backend.py` + C# / PowerShell UIA)**:
  - Talks straight to Win32 `ctypes` with Per-Monitor DPI v2 awareness, background GDI `PrintWindow` capture, 120Hz `SendInput`, dynamic Registry/WindowsApps/StartApps discovery with PE subsystem classification, and SHA-256 verified `uia_fast_bridge.cs` (with `uia_daemon.ps1` fallback).

### Three-stage status pill and live HUD

While any app grant is active, the click-through HUD (`overlay_hud_mac` on macOS using `NSWindow.sharingType = .none`; `overlay_hud.py` on Windows using `WDA_EXCLUDEFROMCAPTURE`) displays a frosty screen-border glow, click ripples, and a pitch-black top status pill with the official 4-color Antigravity mark:

- During tool execution and for the first `1.6s` after a tool finishes, the pill shows `Antigravity is using your Computer` or clean stage steps like `3/5 Opening desktop app` with a frosted `N/M` step badge (never leaking `.app`/`.exe` filenames, paths, or window IDs).
- After `1.6s` idle it switches to `Antigravity • Thinking...` for up to `90s`.
- After `90s` idle it switches to `Antigravity • Waiting for model...` up to the `600s` safety timeout.
- The pill shimmer runs on a constant `2.15s` cycle (`28` logical frames) regardless of tool latency.
- Pressing physical `Esc` at any moment closes the HUD immediately and revokes all active grants (`user_closed_computer_use` on the model's next tool call). On macOS, `overlay_hud_mac.swift` pairs global `NSEvent` keyDown monitoring with 60Hz `CGEventSource.keyState(.combinedSessionState, key: 53)` polling so physical `Esc` never misses even during active drags or open menus.
- Moving the pointer to the upper-left `(0, 0)` corner of any display also triggers `emergency_stop`.

### Built-in fast local UI matcher

`scripts/local_matcher.py` is 100% OS-independent pure Python stdlib and powers `cu_choose` and `cu_step` in under `1ms` across both macOS `AXUIElement` (normalized to canonical `Button`, `Edit`, `ComboBox`, `CheckBox`, `RadioButton`, `MenuItem`, `TabItem`, `Hyperlink`, `Slider`, `Text` roles) and Windows UI Automation trees, with opt-in local LLM Tier 2 fallback (`CUA_MATCHER_ENDPOINT` / `CUA_MATCHER_AUTO_DISCOVER=1`, loopback-only by default).

### Six layers of safety

1. **Expiring session grants**: every controlled window needs an active grant (`CUA_GRANT_TTL_SEC=600`) and `cu_zoom` requires a granted window or `allow_full_desktop=true`.
2. **Mandatory deny lists**: blocks terminals/shells, password managers, OS security prompts (`SecurityAgent`, `CoreServicesUIAgent`, Touch ID/UAC, macOS `System Settings` security panes), admin monitors, and AI agent hosts (`Antigravity`, `Cursor`, `Claude`, `Codex`), plus dangerous OS chords (`cmd+space`, `cmd+opt+esc`, `cmd+ctrl+q`, `Win` chords, `ctrl+shift+esc`, `ctrl+esc`).
3. **Browser `look_only` default**: `Safari`, `Arc`, `Google Chrome`, `Brave Browser`, `Firefox`, and `Microsoft Edge` default to read-only inspection unless `allow_browser_input=true`.
4. **Sensitive editor guard + password redaction**: blocks typing into editors open to `.env`, SSH keys, `.bashrc`, `.zshrc`, `.zprofile`, `.git/hooks`, MCP configs, or executable scripts (`.sh`, `.zsh`, `.ps1`, `.bat`, `.cmd`) while allowing `.py`, `.js`, and `.ts`. Password fields (`AXSecureTextField` on macOS, `IsPasswordProperty` on Windows) are redacted inside the native bridges before data reaches Python.
5. **Desktop & focus guards**: verifies the desktop is unlocked, blocks elevated/root/admin targets, rejects occluded points, and aborts typing on foreground focus change.
6. **Physical emergency stops**: instant physical `Esc` cancel (`user_closed_computer_use`) and `(0, 0)` corner failsafe (`emergency_stop`).

---

## Privacy and zero telemetry

Zero telemetry, zero analytics, and zero background network beacons. Screenshots and observations stay inside the local runtime folder and are purged on `cu_revoke_app({"all": true})` and server exit. The only network traffic is installing `pillow` and `numpy` from PyPI on first run if missing, or contacting a local inference server (`127.0.0.1`) only when explicitly enabled.

---

## Environment variables

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `CUA_THINKING_WAIT_SEC` | `90` | Idle seconds before `Thinking...` switches to `Waiting for model...` (`10`..`300`) |
| `CUA_HUD_IDLE_TIMEOUT_SEC` | `600` | Idle seconds before the status pill closes automatically (`30`..`1800`) |
| `CUA_GRANT_TTL_SEC` | `600` | Inactivity seconds before unused app grants expire (`30`..`1800`) |
| `CUA_HUD_BRAND` | `Antigravity` | Brand prefix displayed inside the status pill |
| `CUA_HUD_HUMAN_TAKEOVER` | `0` | (Windows only) Set to `1` to close the overlay on physical mouse motion while idle (off by default) |
| `CUA_RUNTIME_DIR` | `.runtime` | Runtime folder override (falls back to `~/Library/Application Support/antigravity-computer-use` on macOS or `%LOCALAPPDATA%\computer-use-win32` on Windows) |
| `CUA_PYTHON` | empty | Explicit path to a Python 3.9+ (macOS) or 3.10+ (Windows) interpreter |
| `CUA_MATCHER_ENABLED` | `1` | Set to `0` to keep `cu_choose` and `cu_step` on Tier 1 local scoring only |
| `CUA_MATCHER_ENDPOINT` | empty | Optional inference URL for Tier 2 fallback (non-loopback requires `CUA_ALLOW_REMOTE_MATCHER=1`) |
| `CUA_MATCHER_AUTO_DISCOVER` | `0` | Set to `1` to probe local inference ports `11434`, `1234`, and `1337` |
| `CUA_MATCHER_MODEL` | `local-matcher` | Model name sent to an explicit Tier 2 endpoint |
| `CUA_MATCHER_API_KEY` | empty | Optional bearer token for an explicit Tier 2 endpoint |
| `CUA_MATCHER_API_KIND` | `openai` | Tier 2 API format (`openai` or `completion`) |
| `CUA_ALLOW_REMOTE_MATCHER` | `0` | Set to `1` to allow non-loopback URLs in `CUA_MATCHER_ENDPOINT` |

---

## MCP tools

All 25 tools below can be called directly or chained inside `cu_batch` (up to 80 steps per turn plus `wait`):

| Group | Tools |
| :--- | :--- |
| Discovery and readiness | `cu_status` `cu_list_windows` `cu_wait_for_window` |
| Session access and app launch | `cu_grant_app` `cu_revoke_app` `cu_launch_app` |
| Screen and UI inspection | `cu_observe` `cu_screenshot` `cu_zoom` `cu_ui_tree` |
| Focus and element actions | `cu_focus_window` `cu_click_element` `cu_set_value` `cu_batch` |
| Mouse and scroll gestures | `cu_click` `cu_move` `cu_drag` `cu_scroll` |
| Keyboard, clipboard, and HUD | `cu_type` `cu_key` `cu_clipboard_read` `cu_clipboard_write` `cu_overlay` |
| Fast local matching | `cu_choose` `cu_step` |

---

## Running tests

**macOS:**
```sh
export CUA_RUNTIME_DIR="/tmp/cua-test-rt"
./scripts/launch_mcp --self-test
CUA_PYLIBS_DIR="$CUA_RUNTIME_DIR/pylibs_cp$(python3 -c 'import sys;print(f"{sys.version_info[0]}{sys.version_info[1]}")')" python3 tests/test_computer_use.py
```

**Windows:**
```powershell
$env:CUA_RUNTIME_DIR = "$env:TEMP\cua-test-rt"
scripts\launch_mcp.cmd --self-test
python tests\test_computer_use.py
```

---

## Feedback and support

If something breaks on your Mac or PC or feels rough while using the plugin, send a direct message to [@908Akash on X](https://x.com/908Akash).

---

## Repository layout

```text
computer-use/
├── .gitattributes
├── .gitignore
├── LICENSE
├── README.md
├── ARCHITECTURE.md
├── SECURITY.md
├── plugin.json
├── mcp_config.json
├── requirements.txt
├── assets/
│   └── logo.svg
├── skills/
│   └── computer-use/
│       └── SKILL.md
├── scripts/
│   ├── launch_mcp
│   ├── launch_mcp.cmd
│   ├── launch_mcp.exe
│   ├── launch_mcp_win.cs
│   ├── computer_use_server.py
│   ├── macos_backend.py
│   ├── macos_ax_bridge.swift
│   ├── overlay_hud_mac.swift
│   ├── win32_backend.py
│   ├── overlay_hud.py
│   ├── uia_fast_bridge.cs
│   ├── uia_daemon.ps1
│   ├── local_matcher.py
│   └── antigravity_brand.py
└── tests/
    ├── test_computer_use.py
    └── macos_test_harness.swift
```
