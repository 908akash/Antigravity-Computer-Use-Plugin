import Foundation
import AppKit
import CoreGraphics
import Darwin

// Native macOS AppKit Click-Through Top Pill, Screen-Border Glow & Click Ripple HUD (v3.2.0).
// Matches Windows overlay_hud.py IPC contracts (overlay_state.json / overlay_pid.json)
// and UX rules:
//   - sharingType = .none (excluded from screenshots unless allowCapture == true)
//   - Pitch-black top pill + 4-color Antigravity 'A' mark + frosted N/M step ratio chip
//   - Constant 2.15s shimmer cycle (28 logical frames)
//   - 3-stage idle lifecycle: Active/Stage -> Thinking... (1.6s) -> Waiting for model... (90s) -> exit (600s)
//   - Dual physical Esc detection: NSEvent global/local keyDown monitor + 60Hz CGEventSource.keyState(53)

private let HUD_BRAND: String = {
    let raw = (ProcessInfo.processInfo.environment["CUA_HUD_BRAND"] ?? "Antigravity").trimmingCharacters(in: .whitespacesAndNewlines)
    return raw.isEmpty ? "Antigravity" : raw
}()

private let DEFAULT_PILL_CAPTION = "\(HUD_BRAND) is using your computer"
private let THINKING_PILL_CAPTION = "\(HUD_BRAND) • Thinking..."
private let WAITING_PILL_CAPTION = "\(HUD_BRAND) • Waiting for model..."
private let THINKING_DELAY_SEC: Double = 1.6
private let PILL_SHIMMER_FRAMES: Int = 28
private let PILL_SHIMMER_CYCLE_SEC: Double = 2.15

private let WAITING_DELAY_SEC: Double = {
    let raw = ProcessInfo.processInfo.environment["CUA_THINKING_WAIT_SEC"] ?? "90"
    let val = Double(raw) ?? 90.0
    return max(10.0, min(val.isFinite ? val : 90.0, 300.0))
}()

private let RAW_TELEMETRY_RE: NSRegularExpression = {
    let pat = "(?:\\.(?:exe|app|dll|ps1|py|cmd|bat|sh|swift)\\b|\\bhwnd\\b|\\bcgwindow\\b|0x[0-9a-f]+|[\\\\]|[a-z]:/|(?:[a-z_][a-z0-9_.-]*/[a-z_][a-z0-9_.-]*)|^(?:inspecting|ui tree|focused|invoke|click|set value|set text|typing in|typed|key)\\b)"
    return try! NSRegularExpression(pattern: pat, options: [.caseInsensitive])
}()

private let STAGE_STEP_RE: NSRegularExpression = {
    return try! NSRegularExpression(pattern: "^(\\d{1,2}/\\d{1,2})\\s+(.+)$", options: [])
}()

private func formatPillCaption(_ rawLabel: String?) -> String {
    let s = (rawLabel ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
    if s.isEmpty || s.lowercased() == "active" || s.lowercased() == "idle" {
        return DEFAULT_PILL_CAPTION
    }
    let nsRange = NSRange(s.startIndex..<s.endIndex, in: s)
    if RAW_TELEMETRY_RE.firstMatch(in: s, options: [], range: nsRange) != nil {
        return DEFAULT_PILL_CAPTION
    }
    return "\(HUD_BRAND) • \(String(s.prefix(46)))"
}

private func parseStageBadge(_ caption: String) -> (brandLead: String?, stepBadge: String?, mainText: String) {
    let prefix = "\(HUD_BRAND) • "
    if caption.hasPrefix(prefix) {
        let rest = String(caption.dropFirst(prefix.count)).trimmingCharacters(in: .whitespacesAndNewlines)
        let nsRange = NSRange(rest.startIndex..<rest.endIndex, in: rest)
        if let m = STAGE_STEP_RE.firstMatch(in: rest, options: [], range: nsRange),
           let r1 = Range(m.range(at: 1), in: rest),
           let r2 = Range(m.range(at: 2), in: rest) {
            return (HUD_BRAND, String(rest[r1]), String(rest[r2]).trimmingCharacters(in: .whitespacesAndNewlines))
        }
    }
    return (nil, nil, caption)
}

private func resolveRuntimeDir() -> URL {
    let env = ProcessInfo.processInfo.environment
    var candidates: [URL] = []
    for key in ["CUA_RUNTIME_DIR", "PLUGIN_DATA"] {
        if let v = env[key]?.trimmingCharacters(in: CharacterSet(charactersIn: " \"'\t\n\r")),
           !v.isEmpty, !v.hasPrefix("${") {
            candidates.append(URL(fileURLWithPath: v, isDirectory: true))
        }
    }
    let home = FileManager.default.homeDirectoryForCurrentUser
    candidates.append(home.appendingPathComponent("Library/Application Support/antigravity-computer-use", isDirectory: true))
    candidates.append(URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true).appendingPathComponent("antigravity-computer-use", isDirectory: true))
    for cand in candidates {
        do {
            try FileManager.default.createDirectory(at: cand, withIntermediateDirectories: true)
            let probe = cand.appendingPathComponent(".hud_probe_\(getpid())")
            try Data().write(to: probe)
            try? FileManager.default.removeItem(at: probe)
            return cand
        } catch {
            continue
        }
    }
    return candidates[0]
}

private func writeJSONAtomic(_ url: URL, payload: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: payload, options: []) else { return }
    let tmp = url.deletingLastPathComponent().appendingPathComponent(".\(url.lastPathComponent).\(getpid()).\(UUID().uuidString.prefix(6)).tmp")
    do {
        try data.write(to: tmp, options: .atomic)
        let _ = try? FileManager.default.replaceItemAt(url, withItemAt: tmp)
        if FileManager.default.fileExists(atPath: tmp.path) {
            try? FileManager.default.removeItem(at: tmp)
            try? data.write(to: url, options: .atomic)
        }
    } catch {
        try? FileManager.default.removeItem(at: tmp)
    }
}

private func isPidAlive(_ pid: Int) -> Bool {
    if pid <= 0 { return false }
    if kill(pid_t(pid), 0) == 0 { return true }
    return errno == EPERM
}

private func primaryScreenHeight() -> CGFloat {
    if let primary = NSScreen.screens.first(where: { $0.frame.origin == .zero }) {
        return primary.frame.height
    }
    return NSScreen.main?.frame.height ?? 900.0
}

private func screenForQuartzPoint(_ qx: CGFloat, _ qy: CGFloat) -> NSScreen {
    let ph = primaryScreenHeight()
    let ay = ph - qy
    let pt = NSPoint(x: qx, y: ay)
    for scr in NSScreen.screens {
        if scr.frame.contains(pt) {
            return scr
        }
    }
    return NSScreen.main ?? NSScreen.screens.first!
}

private func quartzRectToAppKit(_ qx: CGFloat, _ qy: CGFloat, _ w: CGFloat, _ h: CGFloat) -> NSRect {
    let ph = primaryScreenHeight()
    return NSRect(x: qx, y: ph - (qy + h), width: w, height: h)
}

// MARK: - Views

final class BorderGlowView: NSView {
    var breathPhase: CGFloat = 0.0

    override var isOpaque: Bool { false }

    override func draw(_ dirtyRect: NSRect) {
        guard let ctx = NSGraphicsContext.current?.cgContext else { return }
        ctx.clear(bounds)

        let w = bounds.width
        let h = bounds.height
        let breath = 0.5 + 0.5 * sin(2.0 * .pi * breathPhase)
        let intensity = 0.72 + 0.28 * breath
        let glowDepth = max(72.0, min(w, h) * 0.095)

        let cs = CGColorSpaceCreateDeviceRGB()
        let edgeColors: [CGColor] = [
            CGColor(red: 0.72, green: 0.89, blue: 1.0, alpha: 0.58 * intensity),
            CGColor(red: 0.58, green: 0.81, blue: 1.0, alpha: 0.24 * intensity),
            CGColor(red: 0.58, green: 0.81, blue: 1.0, alpha: 0.0),
        ]
        let locs: [CGFloat] = [0.0, 0.35, 1.0]
        guard let grad = CGGradient(colorsSpace: cs, colors: edgeColors as CFArray, locations: locs) else { return }

        // Top edge
        ctx.saveGState()
        ctx.clip(to: CGRect(x: 0, y: h - glowDepth, width: w, height: glowDepth))
        ctx.drawLinearGradient(grad, start: CGPoint(x: 0, y: h), end: CGPoint(x: 0, y: h - glowDepth), options: [])
        ctx.restoreGState()

        // Bottom edge
        ctx.saveGState()
        ctx.clip(to: CGRect(x: 0, y: 0, width: w, height: glowDepth))
        ctx.drawLinearGradient(grad, start: CGPoint(x: 0, y: 0), end: CGPoint(x: 0, y: glowDepth), options: [])
        ctx.restoreGState()

        // Left edge
        ctx.saveGState()
        ctx.clip(to: CGRect(x: 0, y: 0, width: glowDepth, height: h))
        ctx.drawLinearGradient(grad, start: CGPoint(x: 0, y: 0), end: CGPoint(x: glowDepth, y: 0), options: [])
        ctx.restoreGState()

        // Right edge
        ctx.saveGState()
        ctx.clip(to: CGRect(x: w - glowDepth, y: 0, width: glowDepth, height: h))
        ctx.drawLinearGradient(grad, start: CGPoint(x: w, y: 0), end: CGPoint(x: w - glowDepth, y: 0), options: [])
        ctx.restoreGState()

        // Crisp outer and inner frost rims
        ctx.setStrokeColor(CGColor(red: 0.95, green: 0.98, blue: 1.0, alpha: 0.90 * intensity))
        ctx.setLineWidth(2.0)
        ctx.stroke(bounds.insetBy(dx: 1.0, dy: 1.0))

        ctx.setStrokeColor(CGColor(red: 0.77, green: 0.90, blue: 1.0, alpha: 0.68 * intensity))
        ctx.setLineWidth(1.0)
        ctx.stroke(bounds.insetBy(dx: 2.5, dy: 2.5))
    }
}

final class TopPillView: NSView {
    var caption: String = DEFAULT_PILL_CAPTION
    var shimmerPhase: CGFloat = 0.0
    var brandImage: NSImage? = nil

    private let pillH: CGFloat = 38.0
    private let margin: CGFloat = 20.0
    private let mainFont = NSFont.systemFont(ofSize: 13.5, weight: .semibold)
    private let keyFont = NSFont.systemFont(ofSize: 10.5, weight: .semibold)

    override var isOpaque: Bool { false }

    func preferredSize() -> NSSize {
        let parsed = parseStageBadge(caption)
        let mainAttrs: [NSAttributedString.Key: Any] = [.font: mainFont]
        let keyAttrs: [NSAttributedString.Key: Any] = [.font: keyFont]

        let mainW = ceil((parsed.mainText as NSString).size(withAttributes: mainAttrs).width)
        let keyW = ceil(("Esc" as NSString).size(withAttributes: keyAttrs).width) + 14.0

        var extraLead: CGFloat = 0.0
        if let lead = parsed.brandLead, let badge = parsed.stepBadge {
            let lw = ceil((lead as NSString).size(withAttributes: mainAttrs).width)
            let bw = ceil((badge as NSString).size(withAttributes: keyAttrs).width) + 12.0
            extraLead = lw + 8.0 + bw + 8.0
        }

        let iconSz: CGFloat = 17.0
        let pillW = 15.0 + iconSz + 10.0 + extraLead + mainW + 12.0 + 1.0 + 12.0 + keyW + 13.0
        return NSSize(width: pillW + margin * 2.0, height: pillH + margin * 2.0)
    }

    private func drawFallbackBrandMark(in ctx: CGContext, rect: CGRect) {
        ctx.saveGState()
        let path = CGMutablePath()
        let x = rect.origin.x
        let y = rect.origin.y
        let w = rect.width
        let h = rect.height
        path.move(to: CGPoint(x: x + w * 0.50, y: y + h * 0.92))
        path.addLine(to: CGPoint(x: x + w * 0.88, y: y + h * 0.12))
        path.addLine(to: CGPoint(x: x + w * 0.72, y: y + h * 0.08))
        path.addLine(to: CGPoint(x: x + w * 0.50, y: y + h * 0.48))
        path.addLine(to: CGPoint(x: x + w * 0.28, y: y + h * 0.08))
        path.addLine(to: CGPoint(x: x + w * 0.12, y: y + h * 0.12))
        path.closeSubpath()
        ctx.addPath(path)
        ctx.clip()
        let cs = CGColorSpaceCreateDeviceRGB()
        let colors: [CGColor] = [
            CGColor(red: 0.98, green: 0.43, blue: 0.24, alpha: 1.0),
            CGColor(red: 0.20, green: 0.72, blue: 1.0, alpha: 1.0),
        ]
        if let g = CGGradient(colorsSpace: cs, colors: colors as CFArray, locations: [0.0, 1.0]) {
            ctx.drawLinearGradient(g, start: CGPoint(x: x, y: y + h), end: CGPoint(x: x + w, y: y), options: [])
        }
        ctx.restoreGState()
    }

    override func draw(_ dirtyRect: NSRect) {
        guard let ctx = NSGraphicsContext.current?.cgContext else { return }
        ctx.clear(bounds)

        let pillRect = CGRect(x: margin, y: margin, width: bounds.width - margin * 2.0, height: pillH)
        let radius = pillH / 2.0
        let breath = 0.5 + 0.5 * sin(2.0 * .pi * shimmerPhase)

        // 1. Soft drop shadow + breathing ice-blue outer halo
        ctx.saveGState()
        let haloPath = CGPath(roundedRect: pillRect, cornerWidth: radius, cornerHeight: radius, transform: nil)
        ctx.setShadow(offset: CGSize(width: 0, height: -3), blur: 11.0, color: CGColor(red: 0, green: 0, blue: 0, alpha: 0.65))
        ctx.addPath(haloPath)
        ctx.setFillColor(CGColor(red: 0.02, green: 0.025, blue: 0.04, alpha: 0.98))
        ctx.fillPath()
        ctx.restoreGState()

        ctx.saveGState()
        ctx.setShadow(offset: .zero, blur: 10.0 + 4.0 * breath, color: CGColor(red: 0.50, green: 0.80, blue: 1.0, alpha: 0.25 + 0.28 * breath))
        ctx.addPath(haloPath)
        ctx.setStrokeColor(CGColor(red: 0.55, green: 0.84, blue: 1.0, alpha: 0.24 + 0.22 * breath))
        ctx.setLineWidth(1.5)
        ctx.strokePath()
        ctx.restoreGState()

        // 2. Pitch-black capsule gradient body (#08090c -> #000000)
        ctx.saveGState()
        ctx.addPath(haloPath)
        ctx.clip()
        let cs = CGColorSpaceCreateDeviceRGB()
        let bodyColors: [CGColor] = [
            CGColor(red: 8.0 / 255.0, green: 9.0 / 255.0, blue: 13.0 / 255.0, alpha: 1.0),
            CGColor(red: 0.0, green: 0.0, blue: 0.0, alpha: 1.0),
        ]
        if let bg = CGGradient(colorsSpace: cs, colors: bodyColors as CFArray, locations: [0.0, 1.0]) {
            ctx.drawLinearGradient(bg, start: CGPoint(x: 0, y: pillRect.maxY), end: CGPoint(x: 0, y: pillRect.minY), options: [])
        }
        ctx.restoreGState()

        // 3. Synchronized rim shimmer
        ctx.saveGState()
        let rimPath = CGPath(roundedRect: pillRect.insetBy(dx: 0.75, dy: 0.75), cornerWidth: radius - 0.75, cornerHeight: radius - 0.75, transform: nil)
        ctx.addPath(rimPath)
        ctx.replacePathWithStrokedPath()
        ctx.clip()
        let waveX = pillRect.minX + (-0.28 + 1.56 * shimmerPhase) * pillRect.width
        let rimGradColors: [CGColor] = [
            CGColor(red: 0.82, green: 0.91, blue: 1.0, alpha: 0.16 + 0.10 * breath),
            CGColor(red: 0.94, green: 0.98, blue: 1.0, alpha: 0.62),
            CGColor(red: 0.82, green: 0.91, blue: 1.0, alpha: 0.16 + 0.10 * breath),
        ]
        if let rg = CGGradient(colorsSpace: cs, colors: rimGradColors as CFArray, locations: [0.0, 0.5, 1.0]) {
            ctx.drawLinearGradient(rg, start: CGPoint(x: waveX - 85.0, y: pillRect.maxY), end: CGPoint(x: waveX + 85.0, y: pillRect.maxY), options: [.drawsBeforeStartLocation, .drawsAfterEndLocation])
        }
        ctx.restoreGState()

        // 4. Official 4-color Antigravity 'A' mark
        let iconSz: CGFloat = 17.0
        let iconRect = CGRect(x: pillRect.minX + 15.0, y: pillRect.midY - iconSz / 2.0, width: iconSz, height: iconSz)
        if let img = brandImage {
            img.draw(in: iconRect, from: .zero, operation: .sourceOver, fraction: 1.0)
        } else {
            drawFallbackBrandMark(in: ctx, rect: iconRect)
        }

        // 5. Optional Brand + frosted N/M step ratio badge + main shimmering text
        let parsed = parseStageBadge(caption)
        var cursorX = iconRect.maxX + 10.0

        if let lead = parsed.brandLead, let badge = parsed.stepBadge {
            let leadAttrs: [NSAttributedString.Key: Any] = [
                .font: mainFont,
                .foregroundColor: NSColor(calibratedRed: 214.0/255.0, green: 224.0/255.0, blue: 238.0/255.0, alpha: 0.94),
            ]
            let leadSz = (lead as NSString).size(withAttributes: leadAttrs)
            (lead as NSString).draw(at: NSPoint(x: cursorX, y: pillRect.midY - leadSz.height / 2.0 - 0.5), withAttributes: leadAttrs)
            cursorX += ceil(leadSz.width) + 8.0

            let badgeAttrs: [NSAttributedString.Key: Any] = [
                .font: keyFont,
                .foregroundColor: NSColor(calibratedRed: 196.0/255.0, green: 234.0/255.0, blue: 1.0, alpha: 1.0),
            ]
            let badgeTextSz = (badge as NSString).size(withAttributes: badgeAttrs)
            let bw = ceil(badgeTextSz.width) + 12.0
            let bh: CGFloat = 20.0
            let bRect = CGRect(x: cursorX, y: pillRect.midY - bh / 2.0, width: bw, height: bh)
            let bPath = CGPath(roundedRect: bRect, cornerWidth: 6.0, cornerHeight: 6.0, transform: nil)
            ctx.addPath(bPath)
            ctx.setFillColor(CGColor(red: 116.0/255.0, green: 198.0/255.0, blue: 1.0, alpha: 0.16))
            ctx.fillPath()
            ctx.addPath(bPath)
            ctx.setStrokeColor(CGColor(red: 152.0/255.0, green: 218.0/255.0, blue: 1.0, alpha: 0.38))
            ctx.setLineWidth(1.0)
            ctx.strokePath()
            (badge as NSString).draw(at: NSPoint(x: bRect.minX + 6.0, y: bRect.midY - badgeTextSz.height / 2.0), withAttributes: badgeAttrs)
            cursorX += bw + 8.0
        }

        // Main caption with traveling pearl/ice-blue glimmer sweep
        let mainStr = parsed.mainText
        let mainMeasureAttrs: [NSAttributedString.Key: Any] = [.font: mainFont]
        let mainSz = (mainStr as NSString).size(withAttributes: mainMeasureAttrs)
        let mainW = ceil(mainSz.width)
        let textY = pillRect.midY - mainSz.height / 2.0 - 0.5

        let attrStr = NSMutableAttributedString(string: mainStr)
        let chars = Array(mainStr)
        let nChars = max(1, chars.count)
        let waveCenter = -0.28 + 1.56 * shimmerPhase
        var utf16Offset = 0
        for (idx, ch) in chars.enumerated() {
            let uLen = String(ch).utf16.count
            let nx = CGFloat(idx) / CGFloat(max(1, nChars - 1))
            let dist = (nx - waveCenter) / 0.21
            let glint = exp(-(dist * dist))
            let leadDist = (nx - (waveCenter + 0.07)) / 0.14
            let leadTint = exp(-(leadDist * leadDist))
            let baseLum = 196.0 + 18.0 * breath
            let r = min(255.0, max(0.0, baseLum + 59.0 * glint - 12.0 * leadTint)) / 255.0
            let g = min(255.0, max(0.0, (baseLum + 7.0) + 52.0 * glint + 16.0 * leadTint)) / 255.0
            let b = min(255.0, max(0.0, (baseLum + 20.0) + 39.0 * glint + 30.0 * leadTint)) / 255.0
            let col = NSColor(calibratedRed: r, green: g, blue: b, alpha: 1.0)
            attrStr.addAttributes([.font: mainFont, .foregroundColor: col], range: NSRange(location: utf16Offset, length: uLen))
            utf16Offset += uLen
        }
        attrStr.draw(at: NSPoint(x: cursorX, y: textY))
        cursorX += mainW + 12.0

        // 6. Divider hairline
        ctx.setStrokeColor(CGColor(red: 1.0, green: 1.0, blue: 1.0, alpha: 0.16))
        ctx.setLineWidth(1.0)
        ctx.move(to: CGPoint(x: cursorX + 0.5, y: pillRect.minY + 10.0))
        ctx.addLine(to: CGPoint(x: cursorX + 0.5, y: pillRect.maxY - 10.0))
        ctx.strokePath()
        cursorX += 13.0

        // 7. Esc keycap badge
        let keyAttrs: [NSAttributedString.Key: Any] = [
            .font: keyFont,
            .foregroundColor: NSColor(calibratedRed: 192.0/255.0, green: 200.0/255.0, blue: 214.0/255.0, alpha: 1.0),
        ]
        let keySz = ("Esc" as NSString).size(withAttributes: keyAttrs)
        let kw = ceil(keySz.width) + 14.0
        let kh: CGFloat = 20.0
        let kRect = CGRect(x: cursorX, y: pillRect.midY - kh / 2.0, width: kw, height: kh)
        let kPath = CGPath(roundedRect: kRect, cornerWidth: 6.0, cornerHeight: 6.0, transform: nil)
        ctx.addPath(kPath)
        ctx.setFillColor(CGColor(red: 22.0/255.0, green: 25.0/255.0, blue: 32.0/255.0, alpha: 1.0))
        ctx.fillPath()
        ctx.addPath(kPath)
        ctx.setStrokeColor(CGColor(red: 1.0, green: 1.0, blue: 1.0, alpha: 0.20))
        ctx.setLineWidth(1.0)
        ctx.strokePath()
        ("Esc" as NSString).draw(at: NSPoint(x: kRect.minX + 7.0, y: kRect.midY - keySz.height / 2.0), withAttributes: keyAttrs)
    }
}

final class ClickRippleView: NSView {
    var progress: CGFloat = 0.0

    override var isOpaque: Bool { false }

    override func draw(_ dirtyRect: NSRect) {
        guard let ctx = NSGraphicsContext.current?.cgContext else { return }
        ctx.clear(bounds)
        let p = max(0.0, min(1.0, progress))
        let cx = bounds.midX
        let cy = bounds.midY

        let rad1 = 5.0 + 30.0 * (1.0 - pow(1.0 - p, 2.0))
        let a1 = 0.85 * (1.0 - p)
        ctx.setStrokeColor(CGColor(red: 236.0/255.0, green: 248.0/255.0, blue: 1.0, alpha: a1))
        ctx.setLineWidth(2.0)
        ctx.strokeEllipse(in: CGRect(x: cx - rad1, y: cy - rad1, width: rad1 * 2.0, height: rad1 * 2.0))

        if p >= 0.12 {
            let p2 = min(1.0, (p - 0.12) / 0.75)
            let rad2 = 3.0 + 18.0 * (1.0 - pow(1.0 - p2, 2.0))
            let a2 = 0.68 * (1.0 - p2)
            ctx.setStrokeColor(CGColor(red: 135.0/255.0, green: 206.0/255.0, blue: 1.0, alpha: a2))
            ctx.setLineWidth(2.0)
            ctx.strokeEllipse(in: CGRect(x: cx - rad2, y: cy - rad2, width: rad2 * 2.0, height: rad2 * 2.0))
        }
    }
}

// MARK: - HUD Controller

final class OverlayHUDController {
    let runtimeDir: URL
    let stateFile: URL
    let pidFile: URL
    let sessionFile: URL
    let brandMarkFile: URL
    let selfTest: Bool

    var borderWin: NSWindow!
    var borderView: BorderGlowView!
    var pillWin: NSWindow!
    var pillView: TopPillView!
    var rippleWin: NSWindow!
    var rippleView: ClickRippleView!

    var globalKeyMonitor: Any?
    var localKeyMonitor: Any?
    var timer: Timer?

    var lastStateMtime: TimeInterval = -1
    var cachedState: [String: Any] = ["active": true, "label": DEFAULT_PILL_CAPTION]
    var lastClickTs: Double = 0.0
    var activeClickWall: Double = 0.0
    var startMono: Double = CFAbsoluteTimeGetCurrent()
    var exiting: Bool = false
    var allowCapture: Bool = false
    var currentScreenFrame: NSRect = .zero

    init(runtimeDir: URL, selfTest: Bool) {
        self.runtimeDir = runtimeDir
        self.stateFile = runtimeDir.appendingPathComponent("overlay_state.json")
        self.pidFile = runtimeDir.appendingPathComponent("overlay_pid.json")
        self.sessionFile = runtimeDir.appendingPathComponent("session_state.json")
        self.brandMarkFile = runtimeDir.appendingPathComponent("brand_mark.png")
        self.selfTest = selfTest
    }

    private func makeOverlayWindow(frame: NSRect, allowCapture: Bool) -> NSWindow {
        let win = NSWindow(
            contentRect: frame,
            styleMask: [.borderless],
            backing: .buffered,
            defer: false
        )
        win.isOpaque = false
        win.backgroundColor = .clear
        win.hasShadow = false
        win.ignoresMouseEvents = true
        win.level = NSWindow.Level(rawValue: Int(CGWindowLevelForKey(.screenSaverWindow)))
        win.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
        win.sharingType = allowCapture ? .readOnly : .none
        win.isReleasedWhenClosed = false
        return win
    }

    private func readStateIfChanged() -> [String: Any] {
        if selfTest { return cachedState }
        do {
            let attrs = try FileManager.default.attributesOfItem(atPath: stateFile.path)
            let mtime = (attrs[.modificationDate] as? Date)?.timeIntervalSince1970 ?? 0
            if mtime != lastStateMtime {
                let data = try Data(contentsOf: stateFile)
                if let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    cachedState = obj
                    lastStateMtime = mtime
                }
            }
        } catch {
            if !FileManager.default.fileExists(atPath: stateFile.path) {
                cachedState = ["active": false]
            }
        }
        return cachedState
    }

    private func layoutOnScreen(_ scr: NSScreen) {
        currentScreenFrame = scr.frame
        borderWin.setFrame(scr.frame, display: false)
        let pSz = pillView.preferredSize()
        let px = scr.frame.midX - pSz.width / 2.0
        let topBarInset: CGFloat = (scr.frame.maxY - scr.visibleFrame.maxY)
        let py = scr.frame.maxY - max(16.0, topBarInset + 6.0) - pSz.height + 20.0
        pillWin.setFrame(NSRect(x: px, y: py, width: pSz.width, height: pSz.height), display: false)
    }

    private func updateCaptionIfNeeded(_ newCaption: String) {
        if pillView.caption == newCaption { return }
        pillView.caption = newCaption
        let pSz = pillView.preferredSize()
        let scr = NSScreen.screens.first(where: { $0.frame == currentScreenFrame }) ?? NSScreen.main ?? NSScreen.screens.first!
        let px = scr.frame.midX - pSz.width / 2.0
        let topBarInset: CGFloat = (scr.frame.maxY - scr.visibleFrame.maxY)
        let py = scr.frame.maxY - max(16.0, topBarInset + 6.0) - pSz.height + 20.0
        pillWin.setFrame(NSRect(x: px, y: py, width: pSz.width, height: pSz.height), display: true)
    }

    func start() {
        let initState = readStateIfChanged()
        if !selfTest && (initState["active"] as? Bool) == false {
            NSApp.terminate(nil)
            return
        }
        allowCapture = (initState["allowCapture"] as? Bool) ?? false

        var initScreen = NSScreen.main ?? NSScreen.screens.first!
        if let cur = initState["cursor"] as? [String: Any],
           let cx = (cur["x"] as? NSNumber)?.doubleValue,
           let cy = (cur["y"] as? NSNumber)?.doubleValue {
            initScreen = screenForQuartzPoint(CGFloat(cx), CGFloat(cy))
        }

        borderWin = makeOverlayWindow(frame: initScreen.frame, allowCapture: allowCapture)
        borderView = BorderGlowView(frame: NSRect(origin: .zero, size: initScreen.frame.size))
        borderWin.contentView = borderView

        pillView = TopPillView(frame: .zero)
        pillView.caption = formatPillCaption(initState["label"] as? String)
        if FileManager.default.fileExists(atPath: brandMarkFile.path) {
            pillView.brandImage = NSImage(contentsOf: brandMarkFile)
        }
        let pSz = pillView.preferredSize()
        pillWin = makeOverlayWindow(frame: NSRect(origin: .zero, size: pSz), allowCapture: allowCapture)
        pillWin.contentView = pillView

        rippleView = ClickRippleView(frame: NSRect(x: 0, y: 0, width: 96, height: 96))
        rippleWin = makeOverlayWindow(frame: NSRect(x: 0, y: 0, width: 96, height: 96), allowCapture: allowCapture)
        rippleWin.contentView = rippleView

        layoutOnScreen(initScreen)

        if selfTest {
            let capChecks =
                formatPillCaption("Inspecting Discord.exe") == DEFAULT_PILL_CAPTION
                && formatPillCaption("Inspecting Safari.app") == DEFAULT_PILL_CAPTION
                && formatPillCaption("UI Tree • TextEdit.app") == DEFAULT_PILL_CAPTION
                && formatPillCaption("Invoke Button • HWND 12345") == DEFAULT_PILL_CAPTION
                && formatPillCaption("Active") == DEFAULT_PILL_CAPTION
                && formatPillCaption("3/5 Opening desktop app") == "\(HUD_BRAND) • 3/5 Opening desktop app"

            let sharingOk = (borderWin.sharingType == .none) && (pillWin.sharingType == .none) && (rippleWin.sharingType == .none)
            let clickThroughOk = borderWin.ignoresMouseEvents && pillWin.ignoresMouseEvents && rippleWin.ignoresMouseEvents
            let payload: [String: Any] = [
                "ok": sharingOk && clickThroughOk && capChecks,
                "hwnd": borderWin.windowNumber,
                "pillHwnd": pillWin.windowNumber,
                "rippleHwnd": rippleWin.windowNumber,
                "sharingTypeNone": sharingOk,
                "clickThrough": clickThroughOk,
                "captionChecksPassed": capChecks,
                "shimmerCycleSec": PILL_SHIMMER_CYCLE_SEC,
                "shimmerFrames": PILL_SHIMMER_FRAMES,
                "thinkingCaption": THINKING_PILL_CAPTION,
                "waitingCaption": WAITING_PILL_CAPTION,
                "waitingDelaySec": WAITING_DELAY_SEC,
            ]
            if let data = try? JSONSerialization.data(withJSONObject: payload, options: []),
               let txt = String(data: data, encoding: .utf8) {
                print(txt)
                fflush(stdout)
            }
            borderWin.close()
            pillWin.close()
            rippleWin.close()
            exit(0)
        }

        borderWin.orderFrontRegardless()
        pillWin.orderFrontRegardless()

        writeJSONAtomic(pidFile, payload: [
            "pid": Int(getpid()),
            "status": "running",
            "startedAt": Date().timeIntervalSince1970,
            "hwnd": borderWin.windowNumber,
            "pillHwnd": pillWin.windowNumber,
            "rippleHwnd": rippleWin.windowNumber,
            "parentPid": (initState["parentPid"] as? NSNumber)?.intValue ?? 0,
        ])

        // Dual physical Esc monitoring: NSEvent global + local keyDown monitors paired with 60Hz CGEventSource polling
        globalKeyMonitor = NSEvent.addGlobalMonitorForEvents(matching: .keyDown) { [weak self] ev in
            if ev.keyCode == 53 {
                self?.triggerExit(reason: "esc")
            }
        }
        localKeyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] ev in
            if ev.keyCode == 53 {
                self?.triggerExit(reason: "esc")
                return nil
            }
            return ev
        }

        // 60Hz timer for shimmer, border breathing, click ripple, IPC state, parent-PID watch, and CGEventSource Esc polling
        timer = Timer.scheduledTimer(withTimeInterval: 1.0 / 60.0, repeats: true) { [weak self] _ in
            self?.tick()
        }
        if let t = timer {
            RunLoop.main.add(t, forMode: .common)
        }
    }

    private func triggerExit(reason: String) {
        if exiting { return }
        exiting = true
        let stoppedAt = Date().timeIntervalSince1970
        writeJSONAtomic(pidFile, payload: [
            "pid": Int(getpid()),
            "status": "exiting",
            "reason": reason,
            "stoppedAt": stoppedAt,
        ])
        timer?.invalidate()
        timer = nil
        if let gm = globalKeyMonitor { NSEvent.removeMonitor(gm); globalKeyMonitor = nil }
        if let lm = localKeyMonitor { NSEvent.removeMonitor(lm); localKeyMonitor = nil }

        // Smooth 0.16s fade-out before exit
        NSAnimationContext.runAnimationGroup({ ctx in
            ctx.duration = 0.16
            self.borderWin.animator().alphaValue = 0.0
            self.pillWin.animator().alphaValue = 0.0
            self.rippleWin.animator().alphaValue = 0.0
        }, completionHandler: {
            self.borderWin.close()
            self.pillWin.close()
            self.rippleWin.close()
            NSApp.terminate(nil)
            exit(0)
        })
    }

    private func tick() {
        if exiting { return }

        // 1. Hardware 60Hz physical Esc poll (never misses during drags or open menus)
        if CGEventSource.keyState(.combinedSessionState, key: 53) {
            triggerExit(reason: "esc")
            return
        }

        let st = readStateIfChanged()
        let nowWall = Date().timeIntervalSince1970

        // 2. Active & parent-PID liveness check
        if (st["active"] as? Bool) == false {
            triggerExit(reason: "inactive")
            return
        }
        if let ppid = (st["parentPid"] as? NSNumber)?.intValue, ppid > 0, !isPidAlive(ppid) {
            triggerExit(reason: "parent_exited")
            return
        }

        // 3. Screenshot exclusion toggle
        let newAllowCap = (st["allowCapture"] as? Bool) ?? false
        if newAllowCap != allowCapture {
            allowCapture = newAllowCap
            let sType: NSWindow.SharingType = allowCapture ? .readOnly : .none
            borderWin.sharingType = sType
            pillWin.sharingType = sType
            rippleWin.sharingType = sType
        }

        // 4. Three-stage idle caption lifecycle (Active/Stage -> Thinking... -> Waiting for model... -> exit at 600s)
        let executing = (st["executing"] as? Bool) ?? false
        let updatedAt = (st["updatedAt"] as? NSNumber)?.doubleValue ?? nowWall
        let lastDoneAt = (st["lastToolDoneAt"] as? NSNumber)?.doubleValue ?? updatedAt
        let idleSec = executing ? 0.0 : max(0.0, nowWall - max(lastDoneAt, updatedAt))
        let thinkWait = max(10.0, min((st["thinkingWaitSec"] as? NSNumber)?.doubleValue ?? WAITING_DELAY_SEC, 300.0))
        let idleTimeout = max(2.0, min((st["idleTimeoutSec"] as? NSNumber)?.doubleValue ?? (st["ttlSec"] as? NSNumber)?.doubleValue ?? 600.0, 1800.0))

        if !executing && idleSec >= idleTimeout {
            triggerExit(reason: "idle_timeout")
            return
        }

        let desiredCaption: String
        if executing || idleSec < THINKING_DELAY_SEC {
            desiredCaption = formatPillCaption(st["label"] as? String)
        } else if idleSec < thinkWait {
            desiredCaption = THINKING_PILL_CAPTION
        } else {
            desiredCaption = WAITING_PILL_CAPTION
        }
        updateCaptionIfNeeded(desiredCaption)

        // 5. Follow active display if cursor moved across screens
        if let cur = st["cursor"] as? [String: Any],
           let cx = (cur["x"] as? NSNumber)?.doubleValue,
           let cy = (cur["y"] as? NSNumber)?.doubleValue {
            let scr = screenForQuartzPoint(CGFloat(cx), CGFloat(cy))
            if scr.frame != currentScreenFrame {
                layoutOnScreen(scr)
            }
        }

        // 6. Constant 2.15s shimmer & border breathing animation (quantized to 28 logical steps or smooth 60fps cycle)
        let elapsed = CFAbsoluteTimeGetCurrent() - startMono
        let phase = CGFloat((elapsed.truncatingRemainder(dividingBy: PILL_SHIMMER_CYCLE_SEC)) / PILL_SHIMMER_CYCLE_SEC)
        pillView.shimmerPhase = phase
        pillView.needsDisplay = true

        let borderPhase = CGFloat((elapsed.truncatingRemainder(dividingBy: 3.4)) / 3.4)
        borderView.breathPhase = borderPhase
        borderView.needsDisplay = true

        // 7. Click ripple (0..0.38s)
        if let clk = st["click"] as? [String: Any],
           let ts = (clk["ts"] as? NSNumber)?.doubleValue,
           let cx = (clk["x"] as? NSNumber)?.doubleValue,
           let cy = (clk["y"] as? NSNumber)?.doubleValue {
            if ts > lastClickTs && (nowWall - ts) <= 0.45 {
                lastClickTs = ts
                activeClickWall = nowWall
                let rRect = quartzRectToAppKit(CGFloat(cx) - 48.0, CGFloat(cy) - 48.0, 96.0, 96.0)
                rippleWin.setFrame(rRect, display: false)
                rippleWin.orderFrontRegardless()
            }
        }
        if activeClickWall > 0 {
            let age = nowWall - activeClickWall
            if age <= 0.38 {
                rippleView.progress = CGFloat(age / 0.38)
                rippleView.needsDisplay = true
            } else {
                activeClickWall = 0
                rippleWin.orderOut(nil)
            }
        }
    }
}

// MARK: - Main Entrypoint

let isSelfTest = CommandLine.arguments.contains("--self-test")
let rtDir = resolveRuntimeDir()

var lockFd: Int32 = -1
if !isSelfTest {
    let lockPath = rtDir.appendingPathComponent("overlay_hud_mac.lock").path
    lockFd = open(lockPath, O_CREAT | O_RDWR, 0o600)
    if lockFd >= 0 {
        if flock(lockFd, LOCK_EX | LOCK_NB) != 0 {
            close(lockFd)
            exit(0) // Another HUD daemon instance is already active
        }
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory) // No Dock icon, no menu bar takeover, click-through overlay

let controller = OverlayHUDController(runtimeDir: rtDir, selfTest: isSelfTest)
DispatchQueue.main.async {
    controller.start()
}
app.run()
