# Antigravity Computer Use Plugin (`v3.1.0`)

Antigravity Computer Use Plugin is an open source Windows 10 (build 19041 or newer) and Windows 11 desktop automation plugin and MCP server (`computer-use` and `computer-use-win32`).

It talks straight to Win32 and Windows UI Automation through Python `ctypes` with no hardcoded app lists and no external pointer wrappers. Read [ARCHITECTURE.md](./ARCHITECTURE.md) for the internals and [`skills/computer-use/SKILL.md`](./skills/computer-use/SKILL.md) for the agent workflow runbook along with [SECURITY.md](./SECURITY.md) for the security model.

---

## Install in Antigravity

### One prompt install

Copy and paste this prompt straight into Antigravity to install and verify the plugin automatically:

```text
Download and install the Windows Computer Use plugin from https://github.com/908akash/Antigravity-Computer-Use-Plugin/releases/latest/download/computer-use-v3.1.0.zip into %USERPROFILE%\.gemini\config\plugins\computer-use then run scripts\launch_mcp.cmd --self-test so its Python packages are ready and share the customization link so I can enable it.
```

After you send that prompt Antigravity handles the setup in three quick steps:

1. Antigravity downloads `computer-use-v3.1.0.zip` and extracts the `computer-use` folder into `%USERPROFILE%\.gemini\config\plugins\computer-use`.
2. Next it runs `scripts\launch_mcp.cmd --self-test` to find Python 3.10 or newer and install `pillow` and `numpy` into the local plugin runtime folder if those packages are missing.
3. Then Antigravity gives you the `[Windows Computer Use](customization://computer-use)` toggle link so you can turn the plugin on and refresh MCP servers.

### One line PowerShell install

You can also download and verify the release archive straight from PowerShell:

```powershell
$zip = "$env:TEMP\computer-use-v3.1.0.zip"; Invoke-WebRequest "https://github.com/908akash/Antigravity-Computer-Use-Plugin/releases/latest/download/computer-use-v3.1.0.zip" -OutFile $zip; Expand-Archive -Path $zip -DestinationPath "$env:USERPROFILE\.gemini\config\plugins" -Force; & "$env:USERPROFILE\.gemini\config\plugins\computer-use\scripts\launch_mcp.cmd" --self-test
```

### Install with git clone

If you prefer git you can clone the repository into your plugins folder and run the self test script:

```powershell
git clone https://github.com/908akash/Antigravity-Computer-Use-Plugin.git "$env:USERPROFILE\.gemini\config\plugins\computer-use"
& "$env:USERPROFILE\.gemini\config\plugins\computer-use\scripts\launch_mcp.cmd" --self-test
```

### Standalone MCP clients

Claude Desktop and Cursor and VS Code can run the server directly by pointing at `scripts\launch_mcp.cmd` with an explicit folder path in `cwd`:

```json
{
  "mcpServers": {
    "computer-use-win32": {
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

Replace `C:\\path\\to\\computer-use` with the real folder path on your PC. Do not copy `${PLUGIN_ROOT}` or `${PLUGIN_DATA}` placeholders from Antigravity's `mcp_config.json` into standalone clients because standalone MCP hosts do not expand Antigravity plugin variables.

---

## How it works on your PC

### Standard user privileges and Win32 capture

The server runs only at your standard user privilege level (medium integrity). It never raises a UAC prompt and refuses elevated Administrator windows through Windows UIPI checks.

Screen capture uses pure Win32 `ctypes` GDI `PrintWindow` in the background and falls back to clipped GDI `BitBlt` desktop capture when needed. Pointer paths use 120Hz hardware `SendInput` curves across the virtual desktop with Per Monitor DPI v2 awareness on mixed DPI multi monitor displays. Open context menus and dropdown popups are detected automatically so screenshots include the popup and clicks inside the menu do not dismiss it.

On first run the server compiles `scripts\uia_fast_bridge.cs` locally using the built in Windows `.NET Framework 4.x` `csc.exe` compiler and checks the binary with a `SHA-256` hash before running it. If compilation is unavailable it falls back automatically to `scripts\uia_daemon.ps1` over PowerShell UI Automation.

Installed desktop apps are resolved dynamically through Windows Registry `App Paths` and `Uninstall` entries along with `WindowsApps` and Start Menu `Get-StartApps` and `PATH` with a PE header subsystem check so GUI desktop apps beat CLI tools of the same name.

### Three stage status pill and hardware cursor

While any app grant is active `scripts\overlay_hud.py` shows a click through top pill and a frosty screen border glow along with a native hardware cursor through `SetSystemCursor`. Every overlay surface uses `WDA_EXCLUDEFROMCAPTURE` so captured screenshots stay clean while you always see when automation is active.

- During tool execution and for the first `1.6` seconds after a tool finishes the top pill displays `Antigravity is using your computer` or clean stage steps like `3/5 Opening desktop app` with raw `.exe` names and window handles stripped out.
- After `1.6` seconds of idle time the pill switches to `Antigravity • Thinking...` for up to `90` seconds while the model plans its next turn.
- After `90` seconds of idle time the pill switches to `Antigravity • Waiting for model...` up to the `600` second safety timeout so slow reasoning or script generation never makes the overlay disappear early.
- Pressing `Esc` at any moment closes the overlay right away and restores your normal Windows cursor through `SPI_SETCURSORS` and revokes every active app grant. On the model's next tool call the server returns `user_closed_computer_use` so the agent stops cleanly and can restart later when authorized.
- Moving your pointer to the top left `(0, 0)` corner of any monitor also triggers an immediate emergency stop.

### Built in fast local UI matcher

The plugin ships with a built in local UI grounding engine at `scripts\local_matcher.py` that powers `cu_choose` and `cu_step` out of the box with zero external model downloads required.

- Tier 1 runs in under one millisecond using pure Python standard library code across up to 120 UI Automation controls with zero extra screenshot overhead. It combines Unicode NFKD folding with CJK and multilingual script tokenization along with `camelCase` and `snake_case` identifier splitting and suffix stemming and Jaro Winkler similarity and W3C CSS3 perceptual color matching and UI control role weighting to return a calibrated confidence choice (`e<index>` or `done` or `unsure`).
- `cu_step` also splits compound instructions like `"Click Settings -> Connections -> Enable Sync"` or calculator expressions into sequential actions and checks expected final UI text in a single MCP turn.
- Tier 2 is an optional local LLM fallback for ambiguous labels. If you run Ollama or LM Studio or Jan or `llama-server` on your own PC (`127.0.0.1` ports `11434` and `1234` and `1337`) you can set `CUA_MATCHER_AUTO_DISCOVER=1` or `CUA_MATCHER_ENDPOINT` to let `local_matcher.py` resolve low confidence controls through grammar constrained single token inference on `localhost`.

### Six layers of safety

- Every inspected or controlled window needs an explicit expiring session grant from `cu_grant_app` or `cu_launch_app`.
- Mandatory deny lists block terminals and shells and password managers and UAC or admin tools and AI agent hosts along with `Win` key chords and `ctrl+shift+esc` and `ctrl+esc` and dangerous launch arguments like passing `cmd.exe` to `explorer.exe`.
- Web browsers start in `look_only` read only mode unless you explicitly approve interactive input with `allow_browser_input`.
- Sensitive editor protection blocks typing into editors open to shell profiles or SSH keys or `.git/hooks` or MCP configs or executable script extensions such as `.ps1` and `.bat` and `.cmd` but allows ordinary source files like `.js` or `.py`.
- Input calls verify that the desktop is unlocked and block elevated UIPI targets and reject occluded points and abort typing if foreground focus changes.
- Physical `Esc` or moving the pointer to `(0, 0)` on any display stops automation immediately and revokes all grants.

---

## Privacy and zero telemetry

This plugin contains zero telemetry and zero analytics and zero tracking beacons and zero background data harvesting.

Screenshots and UI observations stay inside the local runtime folder and are wiped automatically when `cu_revoke_app({"all": true})` runs or when the MCP server exits. Password inputs (`IsPasswordProperty`) are redacted inside both UI Automation bridges before any data reaches Python.

The only network traffic the plugin can ever make is downloading `pillow` and `numpy` from PyPI on first run when those packages are missing from Python or talking to a local inference server (`127.0.0.1` ports `11434` and `1234` and `1337`) only if you opt in via `CUA_MATCHER_ENDPOINT` or `CUA_MATCHER_AUTO_DISCOVER=1`. Non loopback endpoints are rejected unless you explicitly set `CUA_ALLOW_REMOTE_MATCHER=1`.

---

## Environment variables

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `CUA_THINKING_WAIT_SEC` | `90` | Idle seconds before `Thinking...` switches to `Waiting for model...` (clamped `10` to `300`) |
| `CUA_HUD_IDLE_TIMEOUT_SEC` | `600` | Idle seconds before the status pill closes automatically (clamped `30` to `1800`) |
| `CUA_GRANT_TTL_SEC` | `600` | Inactivity seconds before unused app grants expire (clamped `30` to `1800`) |
| `CUA_HUD_BRAND` | `Antigravity` | Brand prefix displayed inside the status pill |
| `CUA_HUD_HUMAN_TAKEOVER` | `0` | Set to `1` to close the overlay on physical mouse motion or keypress while idle |
| `CUA_RUNTIME_DIR` | `.runtime` | Overrides temporary runtime folder path (falls back to `%LOCALAPPDATA%\computer-use-win32` if read only) |
| `CUA_PYTHON` | empty | Explicit path to a Python 3.10 or newer `python.exe` interpreter |
| `CUA_MATCHER_ENABLED` | `1` | Set to `0` to keep `cu_choose` and `cu_step` on Tier 1 local scoring only |
| `CUA_MATCHER_ENDPOINT` | empty | Optional inference URL for Tier 2 fallback (non loopback URLs require `CUA_ALLOW_REMOTE_MATCHER=1`) |
| `CUA_MATCHER_AUTO_DISCOVER` | `0` | Set to `1` to probe local inference ports `11434` and `1234` and `1337` |
| `CUA_MATCHER_MODEL` | `local-matcher` | Model name sent to an explicit Tier 2 endpoint |
| `CUA_MATCHER_API_KEY` | empty | Optional bearer token for an explicit Tier 2 endpoint |
| `CUA_MATCHER_API_KIND` | `openai` | Tier 2 API format (`openai` or `completion`) |
| `CUA_ALLOW_REMOTE_MATCHER` | `0` | Set to `1` to allow non loopback URLs in `CUA_MATCHER_ENDPOINT` |

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
| Keyboard and clipboard and HUD | `cu_type` `cu_key` `cu_clipboard_read` `cu_clipboard_write` `cu_overlay` |
| Fast local matching | `cu_choose` `cu_step` |

---

## Running tests

Verify the launcher and full end to end MCP suite against an isolated runtime folder:

```powershell
$env:CUA_RUNTIME_DIR = "$env:TEMP\cua-test-rt"
scripts\launch_mcp.cmd --self-test
python tests\test_computer_use.py
```

---

## Feedback and support

If something breaks on your PC or feels rough or you have ideas and concerns while using the plugin send a direct message to [@908Akash on X](https://x.com/908Akash).

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
│   ├── launch_mcp.cmd
│   ├── computer_use_server.py
│   ├── local_matcher.py
│   ├── overlay_hud.py
│   ├── antigravity_brand.py
│   ├── uia_fast_bridge.cs
│   └── uia_daemon.ps1
└── tests/
    └── test_computer_use.py
```
