import Foundation
import AppKit
import Darwin

// Native macOS AppKit NSWindow Verification Harness for tests/test_computer_use.py (v3.2.0).
// Provides real AXUIElement nodes (NSTextField, NSButton, modal sheet) on macOS 15 Sequoia
// without touching deprecated Apple CLT Tk 8.5.

final class HarnessController: NSObject, NSApplicationDelegate, NSTextFieldDelegate {
    var window: NSWindow!
    var entryField: NSTextField!
    var applyButton: NSButton!
    var pingButton: NSButton!
    var statusLabel: NSTextField!
    var sheetWindow: NSWindow?

    var applyClicks: Int = 0
    var pingClicks: Int = 0
    var statusText: String = "PENDING — Clicks: 0 | Value: initial_placeholder"

    private let writeLock = NSLock()

    private func sendJSON(_ obj: [String: Any]) {
        writeLock.lock()
        defer { writeLock.unlock() }
        guard let data = try? JSONSerialization.data(withJSONObject: obj, options: []),
              let line = String(data: data, encoding: .utf8) else { return }
        fputs(line + "\n", stdout)
        fflush(stdout)
    }

    private func buildSnapshot(type: String) -> [String: Any] {
        return [
            "type": type,
            "hwnd": window.windowNumber,
            "pid": Int(getpid()),
            "title": window.title,
            "entry_value": entryField.stringValue,
            "apply_clicks": applyClicks,
            "ping_clicks": pingClicks,
            "status_text": statusText,
        ]
    }

    @objc private func onApplyClicked(_ sender: Any?) {
        let val = entryField.stringValue
        applyClicks += 1
        statusText = "VERIFIED — Clicks: \(applyClicks) | Value: \(val)"
        statusLabel.stringValue = statusText
        statusLabel.backgroundColor = NSColor(calibratedRed: 0.024, green: 0.306, blue: 0.231, alpha: 1.0)
        statusLabel.textColor = NSColor(calibratedRed: 0.290, green: 0.871, blue: 0.502, alpha: 1.0)
    }

    @objc private func onPingClicked(_ sender: Any?) {
        pingClicks += 1
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let w: CGFloat = 580
        let h: CGFloat = 360
        let scrH = NSScreen.screens.first(where: { $0.frame.origin == .zero })?.frame.height ?? NSScreen.main?.frame.height ?? 900
        let frame = NSRect(x: 110, y: max(80, scrH - 110 - h), width: w, height: h)

        window = NSWindow(
            contentRect: frame,
            styleMask: [.titled, .closable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Antigravity CUA Verification Harness"
        window.backgroundColor = NSColor(calibratedRed: 0.059, green: 0.090, blue: 0.165, alpha: 1.0)
        window.isReleasedWhenClosed = false

        let content = NSView(frame: NSRect(x: 0, y: 0, width: w, height: h))
        content.wantsLayer = true
        content.layer?.backgroundColor = NSColor(calibratedRed: 0.059, green: 0.090, blue: 0.165, alpha: 1.0).cgColor
        window.contentView = content

        // Header panel
        let headerBox = NSView(frame: NSRect(x: 16, y: h - 92, width: w - 32, height: 76))
        headerBox.wantsLayer = true
        headerBox.layer?.backgroundColor = NSColor(calibratedRed: 0.118, green: 0.161, blue: 0.231, alpha: 1.0).cgColor
        headerBox.layer?.cornerRadius = 8
        content.addSubview(headerBox)

        let titleLbl = NSTextField(labelWithString: "Antigravity Computer Use")
        titleLbl.frame = NSRect(x: 16, y: 38, width: w - 64, height: 24)
        titleLbl.font = NSFont.systemFont(ofSize: 16, weight: .bold)
        titleLbl.textColor = NSColor(calibratedRed: 0.973, green: 0.980, blue: 0.988, alpha: 1.0)
        headerBox.addSubview(titleLbl)

        let subLbl = NSTextField(labelWithString: "Native macOS AppKit AXUIElement + Retina Capture Verification")
        subLbl.frame = NSRect(x: 16, y: 14, width: w - 64, height: 18)
        subLbl.font = NSFont.systemFont(ofSize: 11, weight: .regular)
        subLbl.textColor = NSColor(calibratedRed: 0.580, green: 0.639, blue: 0.722, alpha: 1.0)
        headerBox.addSubview(subLbl)

        // Field label
        let fieldLbl = NSTextField(labelWithString: "Automation Payload Input:")
        fieldLbl.frame = NSRect(x: 20, y: h - 132, width: w - 40, height: 20)
        fieldLbl.font = NSFont.systemFont(ofSize: 12, weight: .semibold)
        fieldLbl.textColor = NSColor(calibratedRed: 0.796, green: 0.835, blue: 0.882, alpha: 1.0)
        content.addSubview(fieldLbl)

        // Editable payload input (AXTextField -> Edit)
        entryField = NSTextField(frame: NSRect(x: 20, y: h - 170, width: w - 40, height: 30))
        entryField.stringValue = "initial_placeholder"
        entryField.identifier = NSUserInterfaceItemIdentifier("payloadEntry")
        entryField.setAccessibilityLabel("Automation Payload Input")
        entryField.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)
        entryField.textColor = NSColor(calibratedRed: 0.220, green: 0.741, blue: 0.973, alpha: 1.0)
        entryField.backgroundColor = NSColor(calibratedRed: 0.008, green: 0.024, blue: 0.090, alpha: 1.0)
        entryField.drawsBackground = true
        entryField.isBezeled = true
        entryField.bezelStyle = .squareBezel
        entryField.delegate = self
        content.addSubview(entryField)

        // Buttons (AXButton -> Button)
        applyButton = NSButton(title: "Apply Action", target: self, action: #selector(onApplyClicked(_:)))
        applyButton.frame = NSRect(x: 20, y: h - 220, width: 140, height: 34)
        applyButton.bezelStyle = .rounded
        applyButton.identifier = NSUserInterfaceItemIdentifier("applyActionButton")
        applyButton.setAccessibilityTitle("Apply Action")
        content.addSubview(applyButton)

        pingButton = NSButton(title: "Secondary Ping", target: self, action: #selector(onPingClicked(_:)))
        pingButton.frame = NSRect(x: 174, y: h - 220, width: 150, height: 34)
        pingButton.bezelStyle = .rounded
        pingButton.identifier = NSUserInterfaceItemIdentifier("secondaryPingButton")
        pingButton.setAccessibilityTitle("Secondary Ping")
        content.addSubview(pingButton)

        // Status banner
        statusLabel = NSTextField(labelWithString: statusText)
        statusLabel.frame = NSRect(x: 20, y: h - 278, width: w - 40, height: 40)
        statusLabel.font = NSFont.systemFont(ofSize: 12, weight: .bold)
        statusLabel.textColor = NSColor(calibratedRed: 0.886, green: 0.910, blue: 0.941, alpha: 1.0)
        statusLabel.backgroundColor = NSColor(calibratedRed: 0.118, green: 0.161, blue: 0.231, alpha: 1.0)
        statusLabel.drawsBackground = true
        statusLabel.identifier = NSUserInterfaceItemIdentifier("statusBanner")
        content.addSubview(statusLabel)

        // Canvas area for smooth drag test visibility
        let canvasBox = NSView(frame: NSRect(x: 20, y: 18, width: w - 40, height: 52))
        canvasBox.wantsLayer = true
        canvasBox.layer?.backgroundColor = NSColor(calibratedRed: 0.082, green: 0.125, blue: 0.210, alpha: 1.0).cgColor
        canvasBox.layer?.cornerRadius = 6
        content.addSubview(canvasBox)

        window.makeKeyAndOrderFront(nil)
        if #available(macOS 14.0, *) {
            NSApp.activate()
        } else {
            NSApp.activate(ignoringOtherApps: true)
        }

        DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) {
            self.sendJSON(self.buildSnapshot(type: "ready"))
            self.startStdinReader()
        }
    }

    private func startStdinReader() {
        DispatchQueue.global(qos: .userInitiated).async {
            while let line = readLine(strippingNewline: true) {
                let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
                if trimmed.isEmpty { continue }
                guard let data = trimmed.data(using: .utf8),
                      let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let cmd = obj["cmd"] as? String else {
                    continue
                }
                DispatchQueue.main.async {
                    self.handleCommand(cmd, obj)
                }
            }
            DispatchQueue.main.async {
                NSApp.terminate(nil)
            }
        }
    }

    private func handleCommand(_ cmd: String, _ obj: [String: Any]) {
        switch cmd {
        case "snapshot":
            sendJSON(buildSnapshot(type: "snapshot"))
        case "set_title":
            if let t = obj["title"] as? String {
                window.title = t
            }
            sendJSON(["type": "ack", "cmd": "set_title", "title": window.title])
        case "set_modal":
            let active = (obj["active"] as? Bool) ?? false
            if active && sheetWindow == nil {
                let sh = NSWindow(
                    contentRect: NSRect(x: 0, y: 0, width: 300, height: 110),
                    styleMask: [.titled],
                    backing: .buffered,
                    defer: false
                )
                let lbl = NSTextField(labelWithString: "Modal Sheet Active")
                lbl.frame = NSRect(x: 20, y: 45, width: 260, height: 24)
                sh.contentView?.addSubview(lbl)
                sheetWindow = sh
                window.beginSheet(sh, completionHandler: nil)
            } else if !active, let sh = sheetWindow {
                window.endSheet(sh)
                sh.orderOut(nil)
                sheetWindow = nil
            }
            sendJSON(["type": "ack", "cmd": "set_modal", "active": active])
        case "quit":
            if let sh = sheetWindow {
                window.endSheet(sh)
                sh.orderOut(nil)
                sheetWindow = nil
            }
            window.close()
            NSApp.terminate(nil)
            exit(0)
        default:
            sendJSON(["type": "error", "message": "unknown_cmd"])
        }
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)
let delegate = HarnessController()
app.delegate = delegate
app.run()
