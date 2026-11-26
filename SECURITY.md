# Security Policy

Antigravity Computer Use Plugin `v3.2.0` automates native **macOS** and **Windows 10/11** desktop apps locally through the Model Context Protocol over standard input and output. Desktop input and screen capture carry real risk, so the plugin limits access by default and stops immediately when you cancel.

## Supported versions

Security fixes apply to the current release line.

- Version `3.2.0` is supported
- Older versions before `3.2.0` are not supported

## Standard-user privilege boundary

The MCP server always runs under your normal signed-in user account and never elevates privileges:
- **macOS**: Relies on explicit manual TCC grants (**Accessibility** and **Screen & System Audio Recording**) configured by the user in **System Settings > Privacy & Security**. Never clicks or types into `SecurityAgent`, `CoreServicesUIAgent`, Touch ID / admin password prompts, TCC permission dialogs, or `System Settings` security/privacy panes. Refuses root-owned (`uid == 0`) processes.
- **Windows**: Never requests Administrator elevation and stops with `elevated_window_uipi` when targeting elevated Administrator windows blocked by Windows UIPI.

## Session grants and blocked applications

Every inspected or controlled app requires an active session grant stored locally in `session_state.json`. Grants expire automatically after `CUA_GRANT_TTL_SEC` (`600` seconds by default) and are cleared when the MCP server exits.

Web browsers (`Safari`, `Arc`, `Google Chrome`, `Brave Browser`, `Firefox`, `Microsoft Edge`, `Opera`, `Vivaldi`) default to `look_only` read-only grants unless `allow_browser_input=true` is explicitly authorized.

Built-in deny lists hard-block high-risk targets across both operating systems:

| Category | Blocked processes / apps |
| :--- | :--- |
| Terminals and shells (macOS) | `Terminal` `iTerm2` `Warp` `Ghostty` `Alacritty` `kitty` `WezTerm` `Hyper` `Tabby` `bash` `sh` `zsh` `fish` `osascript` `sudo` `su` |
| Terminals and shells (Windows) | `cmd.exe` `powershell.exe` `pwsh.exe` `windowsterminal.exe` `wt.exe` `conhost.exe` `openconsole.exe` `mintty.exe` `alacritty.exe` `wezterm-gui.exe` `wsl.exe` `git-bash.exe` `putty.exe` `tabby.exe` `hyper.exe` `conemu64.exe` |
| Password managers & auth prompts | `Keychain Access` `Passwords` `1Password` `Bitwarden` `KeePassXC` `Dashlane` `SecurityAgent` `CoreServicesUIAgent` `credentialuibroker.exe` |
| OS security & system monitors | `Activity Monitor` `System Settings` security/privacy panes `consent.exe` `lockapp.exe` `logonui.exe` `sechealthui.exe` `regedit.exe` `taskmgr.exe` `mmc.exe` |
| Agent host UIs | `Antigravity` `Cursor` `Claude` `Codex` |
| Dangerous OS key chords | `cmd+space` `cmd+opt+esc` `cmd+ctrl+q` (macOS); `Win` chords `ctrl+shift+esc` `ctrl+esc` (Windows) |

## Content, editor, and password safeguards

Before typing or sending key chords into an editor window (`TextEdit`, `Visual Studio Code`, `Notepad`, `Sublime Text`, `Zed`, `Xcode`, JetBrains IDEs, etc.), the server checks the window title against sensitive file patterns (`.env`, SSH keys, `.bashrc`, `.zshrc`, `.zprofile`, `.git/hooks`, `mcp_config.json`, `claude_desktop_config.json`, `.sh`, `.zsh`, `.ps1`, `.bat`, `.cmd`, `.reg`, `.pem`, `.key`) and blocks input with `sensitive_file_guard` while allowing regular source files (`.py`, `.js`, `.ts`).

Both native accessibility bridges redact password field values (`AXSecureTextField` on macOS, `IsPasswordProperty` on Windows) before any control tree data reaches Python.

## Local privacy and screenshot cleanup

Zero telemetry and zero analytics. Screenshots, observations, and compiled helper binaries remain strictly inside the local runtime folder (`CUA_RUNTIME_DIR` / `PLUGIN_DATA` / `.runtime` / `~/Library/Application Support/antigravity-computer-use`). Screenshots and observations are purged automatically on `cu_revoke_app({"all": true})` and on MCP server exit.

## Physical emergency stops

While Computer Use is active, an excluded screen-border glow and top status pill indicate control:
- Press physical `Esc` at any time to dismiss the HUD and revoke all active app grants (`user_closed_computer_use`).
- Move the pointer to the upper-left `(0, 0)` corner of any display to trigger `emergency_stop`.
- Leave the session idle past `CUA_HUD_IDLE_TIMEOUT_SEC` (`600` seconds by default) for automatic HUD dismissal and grant expiry.

## Reporting a vulnerability

Please report security vulnerabilities privately by direct message to [@908Akash on X](https://x.com/908Akash) or through GitHub Security Advisories on `https://github.com/908akash/Antigravity-Computer-Use-Plugin`. Include steps to reproduce and affected macOS or Windows version details along with non-sensitive logs.
