# Security policy

Antigravity Computer Use Plugin v3.1.0 automates native Windows 10 and 11 desktop apps locally through the Model Context Protocol over standard input and output. Desktop input and screen capture carry real risk so the plugin limits access by default and stops immediately when you cancel.

## Supported versions

Security fixes apply to the current release line.

- Version 3.1.0 is supported
- Older versions before 3.1.0 are not supported

## Privilege model and Windows UIPI boundary

The MCP server runs under your normal signed in Windows user account and never requests Administrator elevation. Windows User Interface Privilege Isolation blocks standard user processes from sending mouse or keyboard events to elevated Administrator windows. When a target window runs elevated the server stops and returns an `elevated_window_uipi` error because Windows blocks non elevated input to elevated windows.

## Session grants and blocked applications

Every action requires an active per app session grant stored locally in `session_state.json`. Grants expire automatically after an idle timeout controlled by `CUA_GRANT_TTL_SEC` with a default of 600 seconds and are cleared when the owning MCP server process exits.

Browsers such as Chrome and Edge along with Firefox and Brave default to `look_only` grants so the agent can inspect tabs without mouse or keyboard input unless you explicitly allow browser input.

A built in deny list blocks access to high risk process categories and Start menu app identifiers even if a grant is requested:

| Category | Blocked processes |
| --- | --- |
| Terminals and shells | `cmd.exe` `powershell.exe` `pwsh.exe` `windowsterminal.exe` `wt.exe` `conhost.exe` `openconsole.exe` `mintty.exe` `alacritty.exe` `wezterm-gui.exe` `powershell_ise.exe` `wsl.exe` `git-bash.exe` `putty.exe` `tabby.exe` `hyper.exe` `conemu64.exe` |
| Password managers and auth brokers | `1password.exe` `bitwarden.exe` `keepass.exe` `keepassxc.exe` `dashlane.exe` `credentialuibroker.exe` |
| Security and admin tools | `consent.exe` `lockapp.exe` `logonui.exe` `sechealthui.exe` `regedit.exe` `taskmgr.exe` `mmc.exe` |
| Agent host UIs | `antigravity.exe` `codex.exe` `claude.exe` |

## Content and input safeguards

Before an action inspects or edits a supported text editor window the server checks the window title against sensitive file patterns. That guard blocks automation when an editor title matches shell startup scripts or SSH keys or credential files or git configuration or agent config files such as `mcp_config.json` and `claude_desktop_config.json` or `.git/hooks` or Windows script extensions like `.bat` and `.cmd` and `.ps1` and `.reg`.

When Windows UI Automation walks controls in a granted app it checks password field metadata (`IsPassword`) and redacts password values before it returns control trees to the model.

## Local privacy and screenshot cleanup

This plugin sends zero telemetry and opens zero analytics connections. Screenshots and runtime state stay on your local machine inside the resolved runtime directory (`CUA_RUNTIME_DIR` or `PLUGIN_DATA` or `.runtime`). When app access is revoked or the MCP server exits it deletes captured screenshots and clears active grants.

## Physical emergency stops

Whenever Computer Use is active an excluded screen border glow and top pill indicate control. You can halt automation at any moment:

- Press physical `Esc` to dismiss the overlay HUD and restore system cursors and revoke all active app grants
- Move the pointer into the upper left corner of any monitor to trigger `emergency_stop` and revoke all active grants
- Leave the session idle past the idle timeout (`600` seconds by default) so the HUD shuts down automatically and restores normal cursors

## Reporting a vulnerability

Please report security vulnerabilities privately by direct message to [@908Akash on X](https://x.com/908Akash) or through GitHub Security Advisories on `https://github.com/908akash/Antigravity-Computer-Use-Plugin`. Include steps to reproduce the bug and affected Windows version details along with non sensitive logs so we can verify and fix the issue.
