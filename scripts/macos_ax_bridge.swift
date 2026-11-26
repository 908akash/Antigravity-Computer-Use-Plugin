import Foundation
import AppKit
import ApplicationServices
import CoreGraphics
import Darwin

// Native macOS Accessibility (AXUIElement), Window Inventory (CGWindowList),
// and 120Hz Hardware CGEvent Input Bridge for Antigravity Computer Use v3.2.0.

// Optional dynamic lookup of _AXUIElementGetWindow for exact multi-window CGWindowID matching.
private typealias AXUIElementGetWindowFn = @convention(c) (AXUIElement, UnsafeMutablePointer<CGWindowID>) -> AXError

private let _axGetWindowSym: AXUIElementGetWindowFn? = {
    let rtldDefault = UnsafeMutableRawPointer(bitPattern: -2)
    guard let sym = dlsym(rtldDefault, "_AXUIElementGetWindow") else { return nil }
    return unsafeBitCast(sym, to: AXUIElementGetWindowFn.self)
}()

private func axWindowID(_ element: AXUIElement) -> CGWindowID? {
    guard let fn = _axGetWindowSym else { return nil }
    var wid: CGWindowID = 0
    if fn(element, &wid) == .success && wid != 0 {
        return wid
    }
    return nil
}

private func isPidAlive(_ pid: pid_t) -> Bool {
    if pid <= 0 { return false }
    if kill(pid, 0) == 0 { return true }
    return errno == EPERM
}

private func startParentWatcher(_ parentPid: pid_t) {
    guard parentPid > 1 else { return }
    DispatchQueue.global(qos: .utility).async {
        while true {
            Thread.sleep(forTimeInterval: 1.0)
            if getppid() == 1 || !isPidAlive(parentPid) {
                exit(0)
            }
        }
    }
}

private func writeJSON(_ obj: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: obj, options: []),
          let line = String(data: data, encoding: .utf8) else {
        fputs("{\"error\":\"json_encode_failed\"}\n", stdout)
        fflush(stdout)
        return
    }
    fputs(line + "\n", stdout)
    fflush(stdout)
}

private func procPathForPid(_ pid: pid_t) -> String {
    var buf = [CChar](repeating: 0, count: 4096)
    let len = proc_pidpath(pid, &buf, UInt32(buf.count))
    if len > 0 {
        return String(cString: buf)
    }
    return ""
}

private func isScreenLocked() -> Bool {
    guard let dict = CGSessionCopyCurrentDictionary() as? [String: Any] else {
        return false
    }
    if let locked = dict["CGSSessionScreenIsLocked"] as? Bool, locked {
        return true
    }
    if let lockedNum = dict["CGSSessionScreenIsLocked"] as? NSNumber, lockedNum.boolValue {
        return true
    }
    if let onConsole = dict[kCGSessionOnConsoleKey as String] as? Bool, !onConsole {
        return true
    }
    if let onConsoleNum = dict[kCGSessionOnConsoleKey as String] as? NSNumber, !onConsoleNum.boolValue {
        return true
    }
    return false
}

private func isEscapeKeyPressed() -> Bool {
    return CGEventSource.keyState(.combinedSessionState, key: 53)
}

private func currentCursorPos() -> CGPoint {
    if let ev = CGEvent(source: nil) {
        return ev.location
    }
    return CGPoint(x: 0, y: 0)
}

private func displayRectsAndBounds() -> ([[Double]], [String: Int]) {
    var displayCount: UInt32 = 0
    var displays = [CGDirectDisplayID](repeating: 0, count: 16)
    let err = CGGetActiveDisplayList(UInt32(displays.count), &displays, &displayCount)
    var rects: [[Double]] = []
    var minX = Double.greatestFiniteMagnitude
    var minY = Double.greatestFiniteMagnitude
    var maxX = -Double.greatestFiniteMagnitude
    var maxY = -Double.greatestFiniteMagnitude

    if err == .success && displayCount > 0 {
        for i in 0..<Int(displayCount) {
            let did = displays[i]
            let b = CGDisplayBounds(did)
            var scale: Double = 1.0
            if let mode = CGDisplayCopyDisplayMode(did) {
                let pw = Double(mode.pixelWidth)
                let lw = Double(mode.width)
                if lw > 0 && pw > 0 {
                    scale = max(1.0, pw / lw)
                }
            }
            let l = Double(b.origin.x)
            let t = Double(b.origin.y)
            let r = l + Double(b.size.width)
            let bt = t + Double(b.size.height)
            rects.append([l, t, r, bt, scale])
            minX = min(minX, l)
            minY = min(minY, t)
            maxX = max(maxX, r)
            maxY = max(maxY, bt)
        }
    }

    if rects.isEmpty {
        let b = CGDisplayBounds(CGMainDisplayID())
        let w = max(1.0, Double(b.size.width))
        let h = max(1.0, Double(b.size.height))
        rects.append([Double(b.origin.x), Double(b.origin.y), Double(b.origin.x) + w, Double(b.origin.y) + h, 1.0])
        minX = Double(b.origin.x)
        minY = Double(b.origin.y)
        maxX = minX + w
        maxY = minY + h
    }

    let vb: [String: Int] = [
        "left": Int(minX.rounded()),
        "top": Int(minY.rounded()),
        "width": max(1, Int((maxX - minX).rounded())),
        "height": max(1, Int((maxY - minY).rounded())),
    ]
    return (rects, vb)
}

private func backingScaleForPoint(_ x: Double, _ y: Double, monitors: [[Double]]) -> Double {
    for m in monitors {
        if m.count >= 5 && x >= m[0] && x < m[2] && y >= m[1] && y < m[3] {
            return m[4]
        }
    }
    return monitors.first.flatMap { $0.count >= 5 ? $0[4] : nil } ?? 1.0
}

private let _chromeOwners: Set<String> = [
    "window server",
    "windowmanager",
    "dock",
    "control center",
    "control centre",
    "notification center",
    "wallpaper",
    "spotlight",
    "systemuiserver",
    "screencapture",
]

private func axCopyAttr(_ el: AXUIElement, _ attr: String) -> AnyObject? {
    var value: CFTypeRef?
    let err = AXUIElementCopyAttributeValue(el, attr as CFString, &value)
    guard err == .success, let v = value else { return nil }
    return v
}

private func asAXElement(_ ref: AnyObject?) -> AXUIElement? {
    guard let obj = ref, CFGetTypeID(obj) == AXUIElementGetTypeID() else {
        return nil
    }
    return (obj as! AXUIElement)
}

private func axString(_ el: AXUIElement, _ attr: String) -> String {
    guard let v = axCopyAttr(el, attr) else { return "" }
    if let s = v as? String { return s }
    if let n = v as? NSNumber { return n.stringValue }
    return ""
}

private func axBool(_ el: AXUIElement, _ attr: String, defaultVal: Bool = false) -> Bool {
    guard let v = axCopyAttr(el, attr) else { return defaultVal }
    if let b = v as? Bool { return b }
    if let n = v as? NSNumber { return n.boolValue }
    return defaultVal
}

private func axFrame(_ el: AXUIElement) -> CGRect? {
    guard let posVal = axCopyAttr(el, kAXPositionAttribute as String),
          let sizeVal = axCopyAttr(el, kAXSizeAttribute as String) else {
        return nil
    }
    var pt = CGPoint.zero
    var sz = CGSize.zero
    guard CFGetTypeID(posVal) == AXValueGetTypeID(),
          CFGetTypeID(sizeVal) == AXValueGetTypeID() else {
        return nil
    }
    let ok1 = AXValueGetValue(posVal as! AXValue, .cgPoint, &pt)
    let ok2 = AXValueGetValue(sizeVal as! AXValue, .cgSize, &sz)
    guard ok1 && ok2 else { return nil }
    if pt.x.isNaN || pt.y.isNaN || sz.width.isNaN || sz.height.isNaN { return nil }
    return CGRect(origin: pt, size: sz)
}

private func axWindowTitleFallback(pid: pid_t, wid: CGWindowID, rect: CGRect) -> String {
    guard AXIsProcessTrusted() else { return "" }
    let appEl = AXUIElementCreateApplication(pid)
    AXUIElementSetMessagingTimeout(appEl, 0.15)
    guard let wins = axCopyAttr(appEl, kAXWindowsAttribute as String) as? [AXUIElement] else {
        return ""
    }
    for w in wins {
        if let awid = axWindowID(w), awid == wid {
            let t = axString(w, kAXTitleAttribute as String).trimmingCharacters(in: .whitespacesAndNewlines)
            if !t.isEmpty { return t }
        }
    }
    for w in wins {
        if let f = axFrame(w) {
            if abs(f.origin.x - rect.origin.x) <= 6 && abs(f.origin.y - rect.origin.y) <= 6 &&
               abs(f.size.width - rect.size.width) <= 8 && abs(f.size.height - rect.size.height) <= 8 {
                let t = axString(w, kAXTitleAttribute as String).trimmingCharacters(in: .whitespacesAndNewlines)
                if !t.isEmpty { return t }
            }
        }
    }
    return ""
}

private func resolveAppInfo(_ pid: pid_t, ownerFallback: String) -> (name: String, exePath: String, bundleId: String) {
    var name = ownerFallback.trimmingCharacters(in: .whitespacesAndNewlines)
    var exePath = ""
    var bundleId = ""
    if let app = NSRunningApplication(processIdentifier: pid) {
        if let loc = app.localizedName, !loc.isEmpty {
            name = loc
        }
        if let bUrl = app.bundleURL {
            exePath = bUrl.path
        } else if let eUrl = app.executableURL {
            exePath = eUrl.path
        }
        if let bid = app.bundleIdentifier {
            bundleId = bid
        }
    }
    if exePath.isEmpty {
        exePath = procPathForPid(pid)
    }
    if name.isEmpty && !exePath.isEmpty {
        name = URL(fileURLWithPath: exePath).deletingPathExtension().lastPathComponent
    }
    if name.isEmpty {
        name = "unknown"
    }
    return (name, exePath, bundleId)
}

private func frontmostAppPid() -> pid_t {
    return NSWorkspace.shared.frontmostApplication?.processIdentifier ?? 0
}

private func parseCGRect(_ dict: [String: Any]) -> CGRect? {
    guard let boundsDict = dict[kCGWindowBounds as String] as? NSDictionary else { return nil }
    var rect = CGRect.zero
    guard CGRectMakeWithDictionaryRepresentation(boundsDict, &rect) else { return nil }
    return rect
}

private func isHudWindow(_ dict: [String: Any], hudPid: Int) -> Bool {
    let pid = (dict[kCGWindowOwnerPID as String] as? NSNumber)?.intValue ?? 0
    if hudPid > 0 && pid == hudPid { return true }
    let owner = ((dict[kCGWindowOwnerName as String] as? String) ?? "").lowercased()
    if owner.contains("overlay_hud") { return true }
    return false
}

private func findForegroundHwnd(hudPid: Int = 0) -> Int {
    let fgPid = frontmostAppPid()
    guard fgPid > 0 else { return 0 }
    guard let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] else {
        return 0
    }
    var firstPopup: Int = 0
    for info in list {
        if isHudWindow(info, hudPid: hudPid) { continue }
        let pid = (info[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value ?? 0
        if pid != fgPid { continue }
        let alpha = (info[kCGWindowAlpha as String] as? NSNumber)?.doubleValue ?? 1.0
        if alpha <= 0.01 { continue }
        guard let rect = parseCGRect(info), rect.width >= 20, rect.height >= 20 else { continue }
        let layer = (info[kCGWindowLayer as String] as? NSNumber)?.intValue ?? 0
        let wid = (info[kCGWindowNumber as String] as? NSNumber)?.intValue ?? 0
        if wid <= 0 { continue }
        if layer == 0 {
            return wid
        }
        if firstPopup == 0 && layer > 0 && layer < 25 {
            firstPopup = wid
        }
    }
    return firstPopup
}

private func listWindows(includeMinimized: Bool, requireTitle: Bool, hudPid: Int) -> [[String: Any]] {
    let (monitors, _) = displayRectsAndBounds()
    let fgPid = frontmostAppPid()
    let fgHwnd = findForegroundHwnd(hudPid: hudPid)

    let opts: CGWindowListOption = includeMinimized
        ? [.excludeDesktopElements]
        : [.optionOnScreenOnly, .excludeDesktopElements]
    guard let list = CGWindowListCopyWindowInfo(opts, kCGNullWindowID) as? [[String: Any]] else {
        return []
    }

    var onScreenIds = Set<Int>()
    if let onScreen = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] {
        for item in onScreen {
            if let wid = (item[kCGWindowNumber as String] as? NSNumber)?.intValue {
                onScreenIds.insert(wid)
            }
        }
    }

    var out: [[String: Any]] = []
    var seen = Set<Int>()
    for info in list {
        if isHudWindow(info, hudPid: hudPid) { continue }
        let wid = (info[kCGWindowNumber as String] as? NSNumber)?.intValue ?? 0
        if wid <= 0 || seen.contains(wid) { continue }
        let layer = (info[kCGWindowLayer as String] as? NSNumber)?.intValue ?? 0
        if layer != 0 { continue }
        let alpha = (info[kCGWindowAlpha as String] as? NSNumber)?.doubleValue ?? 1.0
        if alpha <= 0.01 { continue }
        guard let rect = parseCGRect(info) else { continue }
        let isOnScreen = onScreenIds.contains(wid)
        if !isOnScreen && !includeMinimized { continue }
        if rect.width < 20 || rect.height < 20 { continue }

        let pid = (info[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value ?? 0
        if pid <= 0 { continue }
        let ownerRaw = (info[kCGWindowOwnerName as String] as? String) ?? ""
        if _chromeOwners.contains(ownerRaw.lowercased()) { continue }

        let appInfo = resolveAppInfo(pid, ownerFallback: ownerRaw)
        if _chromeOwners.contains(appInfo.name.lowercased()) { continue }

        // Skip off-screen macOS internal helper/accessory/menubar backing windows;
        // only include off-screen windows if genuinely minimized via AXUIElement.
        if !isOnScreen {
            if let app = NSRunningApplication(processIdentifier: pid), app.activationPolicy != .regular {
                continue
            }
            if (rect.origin.y <= 1 && rect.size.height <= 44) || rect.size.width < 100 || rect.size.height < 80 {
                continue
            }
            var trulyMinimized = false
            if AXIsProcessTrusted(), let axWin = findAXWindow(pid: pid, hwnd: wid, targetRect: rect) {
                trulyMinimized = axBool(axWin, kAXMinimizedAttribute as String)
            }
            if !trulyMinimized {
                continue
            }
        }

        var title = ((info[kCGWindowName as String] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if title.isEmpty {
            title = axWindowTitleFallback(pid: pid, wid: CGWindowID(wid), rect: rect)
        }
        if title.isEmpty && isOnScreen {
            // Fall back to app localized name for standard macOS windows without explicit CGWindowName
            // (e.g., Calculator or windows before Screen Recording is granted)
            title = appInfo.name
        }
        if requireTitle && title.isEmpty { continue }

        let cx = Double(rect.origin.x + rect.size.width / 2.0)
        let cy = Double(rect.origin.y + rect.size.height / 2.0)
        let scale = backingScaleForPoint(cx, cy, monitors: monitors)

        let left = Int(rect.origin.x.rounded())
        let top = Int(rect.origin.y.rounded())
        let width = max(0, Int(rect.size.width.rounded()))
        let height = max(0, Int(rect.size.height.rounded()))

        seen.insert(wid)
        out.append([
            "hwnd": wid,
            "pid": Int(pid),
            "processName": appInfo.name,
            "exePath": appInfo.exePath,
            "bundleId": appInfo.bundleId,
            "title": title,
            "bounds": [
                "left": left,
                "top": top,
                "right": left + width,
                "bottom": top + height,
                "width": width,
                "height": height,
            ],
            "foreground": (wid == fgHwnd) || (fgHwnd == 0 && pid == fgPid),
            "minimized": !isOnScreen,
            "visible": isOnScreen,
            "layer": layer,
            "backingScale": scale,
        ])
    }
    return out
}

private func inspectWindow(hwnd: Int, hudPid: Int) -> [String: Any]? {
    guard hwnd > 0 else { return nil }
    let (monitors, _) = displayRectsAndBounds()
    let fgHwnd = findForegroundHwnd(hudPid: hudPid)
    let fgPid = frontmostAppPid()

    var onScreenIds = Set<Int>()
    if let onScreen = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] {
        for item in onScreen {
            if let wid = (item[kCGWindowNumber as String] as? NSNumber)?.intValue {
                onScreenIds.insert(wid)
            }
        }
    }

    guard let list = CGWindowListCopyWindowInfo([.optionIncludingWindow], CGWindowID(hwnd)) as? [[String: Any]],
          let info = list.first else {
        return nil
    }
    guard let rect = parseCGRect(info) else { return nil }
    let pid = (info[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value ?? 0
    if pid <= 0 || !isPidAlive(pid) { return nil }
    let ownerRaw = (info[kCGWindowOwnerName as String] as? String) ?? ""
    let appInfo = resolveAppInfo(pid, ownerFallback: ownerRaw)
    var title = ((info[kCGWindowName as String] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
    if title.isEmpty {
        title = axWindowTitleFallback(pid: pid, wid: CGWindowID(hwnd), rect: rect)
    }
    if title.isEmpty {
        title = appInfo.name
    }
    let isOnScreen = onScreenIds.contains(hwnd)
    let layer = (info[kCGWindowLayer as String] as? NSNumber)?.intValue ?? 0
    let cx = Double(rect.origin.x + rect.size.width / 2.0)
    let cy = Double(rect.origin.y + rect.size.height / 2.0)
    let scale = backingScaleForPoint(cx, cy, monitors: monitors)
    let left = Int(rect.origin.x.rounded())
    let top = Int(rect.origin.y.rounded())
    let width = max(0, Int(rect.size.width.rounded()))
    let height = max(0, Int(rect.size.height.rounded()))

    return [
        "hwnd": hwnd,
        "pid": Int(pid),
        "processName": appInfo.name,
        "exePath": appInfo.exePath,
        "bundleId": appInfo.bundleId,
        "title": title,
        "bounds": [
            "left": left,
            "top": top,
            "right": left + width,
            "bottom": top + height,
            "width": width,
            "height": height,
        ],
        "foreground": (hwnd == fgHwnd) || (pid == fgPid && isOnScreen && layer >= 0),
        "minimized": !isOnScreen,
        "visible": isOnScreen,
        "layer": layer,
        "backingScale": scale,
    ]
}

private func detectPopup(ownerHwnd: Int, hudPid: Int) -> [String: Any] {
    guard let owner = inspectWindow(hwnd: ownerHwnd, hudPid: hudPid),
          let ownerPid = owner["pid"] as? Int, ownerPid > 0 else {
        return ["popupHwnd": 0]
    }
    guard let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] else {
        return ["popupHwnd": 0]
    }
    for info in list {
        if isHudWindow(info, hudPid: hudPid) { continue }
        let wid = (info[kCGWindowNumber as String] as? NSNumber)?.intValue ?? 0
        if wid == ownerHwnd {
            // Reached the owner window in front-to-back order without seeing a higher same-PID popup/sheet
            break
        }
        let pid = (info[kCGWindowOwnerPID as String] as? NSNumber)?.intValue ?? 0
        if pid != ownerPid { continue }
        let alpha = (info[kCGWindowAlpha as String] as? NSNumber)?.doubleValue ?? 1.0
        if alpha <= 0.01 { continue }
        guard let rect = parseCGRect(info), rect.width >= 20, rect.height >= 16 else { continue }
        let layer = (info[kCGWindowLayer as String] as? NSNumber)?.intValue ?? 0
        // Context menus / NSPopUpButton menus / popovers / sheets run at layer > 0
        if layer > 0 && layer <= 105 {
            return [
                "popupHwnd": wid,
                "layer": layer,
                "bounds": [
                    "left": Int(rect.origin.x.rounded()),
                    "top": Int(rect.origin.y.rounded()),
                    "width": Int(rect.size.width.rounded()),
                    "height": Int(rect.size.height.rounded()),
                ],
            ]
        }
        // Only classify a layer == 0 sibling as a popup if AX confirms it is an AXSheet/AXDialog (not a normal document window)
        if layer == 0 && AXIsProcessTrusted(), let axWin = findAXWindow(pid: pid_t(ownerPid), hwnd: wid, targetRect: rect) {
            let role = axString(axWin, kAXRoleAttribute as String)
            let subrole = axString(axWin, kAXSubroleAttribute as String)
            if role == "AXSheet" || subrole == "AXDialog" || subrole == "AXSystemDialog" || subrole == "AXFloatingWindow" {
                return [
                    "popupHwnd": wid,
                    "layer": layer,
                    "bounds": [
                        "left": Int(rect.origin.x.rounded()),
                        "top": Int(rect.origin.y.rounded()),
                        "width": Int(rect.size.width.rounded()),
                        "height": Int(rect.size.height.rounded()),
                    ],
                ]
            }
        }
    }
    return ["popupHwnd": 0]
}

private func pointInsideVisibleWorkArea(_ qx: Double, _ qy: Double) -> Bool {
    let ph = NSScreen.screens.first(where: { $0.frame.origin == .zero })?.frame.height ?? NSScreen.main?.frame.height ?? 900.0
    let akPt = NSPoint(x: qx, y: ph - qy)
    for scr in NSScreen.screens {
        if scr.frame.contains(akPt) {
            return scr.visibleFrame.contains(akPt)
        }
    }
    return false
}

private func hitTestPoint(x: Double, y: Double, hudPid: Int) -> [String: Any] {
    guard let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] else {
        return ["hwnd": 0, "pid": 0, "layer": 0, "ownerName": ""]
    }
    let pt = CGPoint(x: x, y: y)
    let inWorkArea = pointInsideVisibleWorkArea(x, y)
    for info in list {
        if isHudWindow(info, hudPid: hudPid) { continue }
        let alpha = (info[kCGWindowAlpha as String] as? NSNumber)?.doubleValue ?? 1.0
        if alpha <= 0.01 { continue }
        let owner = (info[kCGWindowOwnerName as String] as? String) ?? ""
        let ownerLow = owner.lowercased()
        let name = (info[kCGWindowName as String] as? String) ?? ""
        let layer = (info[kCGWindowLayer as String] as? NSNumber)?.intValue ?? 0
        // Only skip invisible compositor/backdrop layers and the full-screen transparent Dock
        // container when the point lies inside the screen's non-Dock visibleFrame.
        if ownerLow == "window server" || ownerLow == "wallpaper" { continue }
        guard let rect = parseCGRect(info), rect.width >= 4, rect.height >= 4 else { continue }
        if ownerLow == "dock" && layer == 20 && name == "Dock" && inWorkArea {
            continue
        }
        if rect.contains(pt) {
            let wid = (info[kCGWindowNumber as String] as? NSNumber)?.intValue ?? 0
            let pid = (info[kCGWindowOwnerPID as String] as? NSNumber)?.intValue ?? 0
            return ["hwnd": wid, "pid": pid, "layer": layer, "ownerName": owner]
        }
    }
    return ["hwnd": 0, "pid": 0, "layer": 0, "ownerName": ""]
}

private func findAXWindow(pid: pid_t, hwnd: Int, targetRect: CGRect?) -> AXUIElement? {
    let appEl = AXUIElementCreateApplication(pid)
    AXUIElementSetMessagingTimeout(appEl, 0.35)
    // Enable Chromium/Electron accessibility tree lazily if supported
    let _ = AXUIElementSetAttributeValue(appEl, "AXEnhancedUserInterface" as CFString, kCFBooleanTrue)

    if let wins = axCopyAttr(appEl, kAXWindowsAttribute as String) as? [AXUIElement], !wins.isEmpty {
        for w in wins {
            if let awid = axWindowID(w), Int(awid) == hwnd {
                return w
            }
        }
        if let r = targetRect {
            var best: AXUIElement?
            var bestDist = Double.greatestFiniteMagnitude
            for w in wins {
                if let f = axFrame(w) {
                    let d = abs(Double(f.origin.x - r.origin.x)) + abs(Double(f.origin.y - r.origin.y))
                        + abs(Double(f.size.width - r.size.width)) + abs(Double(f.size.height - r.size.height))
                    if d < bestDist {
                        bestDist = d
                        best = w
                    }
                }
            }
            if let b = best, bestDist <= 32.0 {
                return b
            }
        }
        if let focused = asAXElement(axCopyAttr(appEl, kAXFocusedWindowAttribute as String)) {
            return focused
        }
        if let mainW = asAXElement(axCopyAttr(appEl, kAXMainWindowAttribute as String)) {
            return mainW
        }
        return wins.first
    }
    if let focused = asAXElement(axCopyAttr(appEl, kAXFocusedWindowAttribute as String)) {
        return focused
    }
    return nil
}

private func activateWindow(hwnd: Int, hudPid: Int) -> [String: Any] {
    guard let info = inspectWindow(hwnd: hwnd, hudPid: hudPid),
          let pidInt = info["pid"] as? Int, pidInt > 0 else {
        return ["ok": false, "foregroundHwnd": findForegroundHwnd(hudPid: hudPid)]
    }
    let pid = pid_t(pidInt)
    var targetRect: CGRect?
    if let b = info["bounds"] as? [String: Int] {
        targetRect = CGRect(x: b["left"] ?? 0, y: b["top"] ?? 0, width: b["width"] ?? 0, height: b["height"] ?? 0)
    }
    let app = NSRunningApplication(processIdentifier: pid)
    if let running = app, running.isHidden {
        running.unhide()
    }
    if AXIsProcessTrusted() {
        let appEl = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(appEl, 0.35)
        if let axWin = findAXWindow(pid: pid, hwnd: hwnd, targetRect: targetRect) {
            if axBool(axWin, kAXMinimizedAttribute as String) {
                let _ = AXUIElementSetAttributeValue(axWin, kAXMinimizedAttribute as CFString, kCFBooleanFalse)
                Thread.sleep(forTimeInterval: 0.12)
            }
            let _ = AXUIElementSetAttributeValue(axWin, kAXMainAttribute as CFString, kCFBooleanTrue)
            let _ = AXUIElementPerformAction(axWin, kAXRaiseAction as CFString)
        }
        // kAXFrontmostAttribute works reliably from background daemons on macOS 14/15 Sequoia
        let _ = AXUIElementSetAttributeValue(appEl, kAXFrontmostAttribute as CFString, kCFBooleanTrue)
    }
    if let running = app {
        if #available(macOS 14.0, *) {
            let _ = running.activate()
        } else {
            let _ = running.activate(options: [.activateIgnoringOtherApps])
        }
    }
    for attempt in 0..<12 {
        if frontmostAppPid() == pid {
            if AXIsProcessTrusted(), let axWin = findAXWindow(pid: pid, hwnd: hwnd, targetRect: targetRect) {
                let _ = AXUIElementPerformAction(axWin, kAXRaiseAction as CFString)
            }
            return ["ok": true, "foregroundHwnd": findForegroundHwnd(hudPid: hudPid)]
        }
        if attempt == 4, let bundleURL = app?.bundleURL {
            // LaunchServices fallback for macOS 15 Stage Manager / cross-space activation
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: "/usr/bin/open")
            proc.arguments = ["-a", bundleURL.path]
            try? proc.run()
            proc.waitUntilExit()
        }
        Thread.sleep(forTimeInterval: 0.04)
    }
    let ok = frontmostAppPid() == pid
    return ["ok": ok, "foregroundHwnd": findForegroundHwnd(hudPid: hudPid)]
}

private func canonicalControlType(role: String, subrole: String) -> String {
    switch role {
    case "AXButton", "AXMenuButton":
        return "Button"
    case "AXPopUpButton", "AXComboBox":
        return "ComboBox"
    case "AXTextField", "AXTextArea", "AXSearchField", "AXSecureTextField":
        return "Edit"
    case "AXCheckBox":
        return "CheckBox"
    case "AXRadioButton":
        if subrole == "AXTabButton" { return "TabItem" }
        return "RadioButton"
    case "AXMenuItem", "AXMenuBarItem":
        return "MenuItem"
    case "AXTab":
        return "TabItem"
    case "AXLink":
        return "Hyperlink"
    case "AXSlider", "AXValueIndicator":
        return "Slider"
    case "AXStaticText":
        return "Text"
    case "AXRow", "AXOutlineRow":
        return "ListItem"
    case "AXCell":
        return "DataItem"
    case "AXDisclosureTriangle":
        return "TreeItem"
    case "AXIncrementor":
        return "Button"
    case "AXProgressIndicator":
        return "ProgressBar"
    case "AXToolbar", "AXGroup", "AXSplitGroup":
        return "Group"
    case "AXTabGroup":
        return "TabItem"
    case "AXWebArea":
        return "Document"
    case "AXSheet", "AXDialog":
        return "Window"
    default:
        if role.hasPrefix("AX") {
            return String(role.dropFirst(2))
        }
        return role.isEmpty ? "Custom" : role
    }
}

private let _keepCanonicalTypes: Set<String> = [
    "Button", "Edit", "Document", "ComboBox", "CheckBox", "RadioButton",
    "MenuItem", "TabItem", "ListItem", "Hyperlink", "TreeItem", "Slider",
    "SplitButton", "DataItem",
]

private func extractElementName(_ el: AXUIElement, role: String, isPassword: Bool) -> String {
    for attr in [kAXTitleAttribute as String, kAXDescriptionAttribute as String, kAXHelpAttribute as String, "AXPlaceholderValue"] {
        let s = axString(el, attr).trimmingCharacters(in: .whitespacesAndNewlines)
        if !s.isEmpty { return s }
    }
    if !isPassword && (role == "AXStaticText" || role == "AXButton" || role == "AXMenuItem" || role == "AXCheckBox" || role == "AXRadioButton" || role == "AXLink") {
        let v = axString(el, kAXValueAttribute as String).trimmingCharacters(in: .whitespacesAndNewlines)
        if !v.isEmpty { return String(v.prefix(120)) }
    }
    return ""
}

private func queryUITree(hwnd: Int, maxElements: Int, setValueIndex: Int, valueText: String, invokeIndex: Int, hudPid: Int) -> [String: Any] {
    if !AXIsProcessTrusted() {
        return [
            "error": "accessibility_permission_required: Grant Accessibility permission to Antigravity in System Settings > Privacy & Security > Accessibility, then retry."
        ]
    }
    guard let winInfo = inspectWindow(hwnd: hwnd, hudPid: hudPid),
          let pidInt = winInfo["pid"] as? Int, pidInt > 0 else {
        return ["error": "window_unavailable: Cannot inspect CGWindowID \(hwnd)"]
    }
    let pid = pid_t(pidInt)
    let layer = (winInfo["layer"] as? Int) ?? 0
    var targetRect: CGRect?
    if let b = winInfo["bounds"] as? [String: Int] {
        targetRect = CGRect(x: b["left"] ?? 0, y: b["top"] ?? 0, width: b["width"] ?? 0, height: b["height"] ?? 0)
    }

    let appEl = AXUIElementCreateApplication(pid)
    AXUIElementSetMessagingTimeout(appEl, 0.35)

    var rootEl: AXUIElement?
    if layer > 0 {
        // Active popup / menu window: try focused UI element's menu container first, then open menu bar / app AXMenu matching popup rect
        if let fEl = asAXElement(axCopyAttr(appEl, kAXFocusedUIElementAttribute as String)) {
            let fRole = axString(fEl, kAXRoleAttribute as String)
            if fRole == "AXMenu" || fRole == "AXMenuItem" {
                rootEl = fRole == "AXMenu" ? fEl : (asAXElement(axCopyAttr(fEl, kAXParentAttribute as String)) ?? fEl)
            }
        }
        if rootEl == nil, let rect = targetRect {
            // Walk top-level app children and menu bar children to locate an open AXMenu matching the popup frame
            var searchRoots: [AXUIElement] = []
            if let ch = axCopyAttr(appEl, kAXChildrenAttribute as String) as? [AXUIElement] {
                searchRoots.append(contentsOf: ch)
            }
            if let mb = asAXElement(axCopyAttr(appEl, kAXMenuBarAttribute as String)) {
                if let mbCh = axCopyAttr(mb, kAXChildrenAttribute as String) as? [AXUIElement] {
                    searchRoots.append(contentsOf: mbCh)
                }
            }
            for cand in searchRoots {
                let cr = axString(cand, kAXRoleAttribute as String)
                if cr == "AXMenu" {
                    if let f = axFrame(cand), abs(f.origin.x - rect.origin.x) <= 16 && abs(f.origin.y - rect.origin.y) <= 16 {
                        rootEl = cand
                        break
                    }
                }
                if let sub = axCopyAttr(cand, kAXChildrenAttribute as String) as? [AXUIElement] {
                    for s in sub where axString(s, kAXRoleAttribute as String) == "AXMenu" {
                        if let f = axFrame(s), abs(f.origin.x - rect.origin.x) <= 16 && abs(f.origin.y - rect.origin.y) <= 16 {
                            rootEl = s
                            break
                        }
                    }
                    if rootEl != nil { break }
                }
            }
            if rootEl == nil {
                var winEls: [AXUIElement] = []
                if let fw = asAXElement(axCopyAttr(appEl, kAXFocusedWindowAttribute as String)) {
                    winEls.append(fw)
                }
                if let mw = asAXElement(axCopyAttr(appEl, kAXMainWindowAttribute as String)),
                   !winEls.contains(where: { CFEqual($0, mw) }) {
                    winEls.append(mw)
                }
                if let wins = axCopyAttr(appEl, kAXWindowsAttribute as String) as? [AXUIElement] {
                    for w in wins where !winEls.contains(where: { CFEqual($0, w) }) {
                        winEls.append(w)
                    }
                }
                for w in winEls {
                    if let wCh = axCopyAttr(w, kAXChildrenAttribute as String) as? [AXUIElement] {
                        for s in wCh where axString(s, kAXRoleAttribute as String) == "AXMenu" {
                            if let f = axFrame(s), abs(f.origin.x - rect.origin.x) <= 16 && abs(f.origin.y - rect.origin.y) <= 16 {
                                rootEl = s
                                break
                            }
                        }
                    }
                    if rootEl != nil { break }
                }
            }
        }
    }
    if rootEl == nil {
        rootEl = findAXWindow(pid: pid, hwnd: hwnd, targetRect: targetRect)
    }
    guard let root = rootEl else {
        return ["focusedElement": NSNull(), "count": 0, "timedOut": false, "elements": []]
    }

    var focusedStr: String? = nil
    if let fEl = asAXElement(axCopyAttr(appEl, kAXFocusedUIElementAttribute as String)) {
        let fr = axString(fEl, kAXRoleAttribute as String)
        let fsr = axString(fEl, kAXSubroleAttribute as String)
        let fct = canonicalControlType(role: fr, subrole: fsr)
        let isPwd = (fr == "AXSecureTextField" || fsr == "AXSecureTextField")
        let fn = extractElementName(fEl, role: fr, isPassword: isPwd)
        focusedStr = "\(fct) '\(fn)'"
    }

    struct QNode {
        let el: AXUIElement
        let depth: Int
    }

    var queue: [QNode] = [QNode(el: root, depth: 0)]
    var qHead = 0
    var items: [[String: Any]] = []
    var visited = 0
    let maxVisited = 1500
    let maxDepth = 16
    let budgetSec = 1.1
    let t0 = CFAbsoluteTimeGetCurrent()

    while qHead < queue.count && items.count < maxElements && visited < maxVisited && (CFAbsoluteTimeGetCurrent() - t0) < budgetSec {
        let cur = queue[qHead]
        qHead += 1
        visited += 1

        if cur.depth > 0 {
            let role = axString(cur.el, kAXRoleAttribute as String)
            if !role.isEmpty {
                let subrole = axString(cur.el, kAXSubroleAttribute as String)
                let ct = canonicalControlType(role: role, subrole: subrole)
                let isPassword = (role == "AXSecureTextField" || subrole == "AXSecureTextField")
                let name = extractElementName(cur.el, role: role, isPassword: isPassword)
                if focusedStr == nil && axBool(cur.el, kAXFocusedAttribute as String) {
                    focusedStr = "\(ct) '\(name)'"
                }

                if let r = axFrame(cur.el), r.size.width > 2, r.size.height > 2 {
                    // Exclude elements completely outside huge/negative sentinel bounds
                    if abs(r.origin.x) < 100000 && abs(r.origin.y) < 100000 {
                        let aid = axString(cur.el, "AXIdentifier")
                        let enabled = axBool(cur.el, kAXEnabledAttribute as String, defaultVal: true)
                        if _keepCanonicalTypes.contains(ct) || (!name.isEmpty && cur.depth <= 5 && ct == "Text") {
                            var val: Any = NSNull()
                            let isEditable = (ct == "Edit" || ct == "ComboBox" || ct == "Document" || ct == "Slider")
                            if !isPassword && (isEditable || ct == "Text" || ct == "CheckBox" || ct == "RadioButton") {
                                let rawV = axString(cur.el, kAXValueAttribute as String)
                                if !rawV.isEmpty {
                                    val = rawV.count > 120 ? String(rawV.prefix(120)) + "..." : rawV
                                }
                            }

                            let idx = items.count
                            let cx = Int((r.origin.x + r.size.width / 2.0).rounded())
                            let cy = Int((r.origin.y + r.size.height / 2.0).rounded())

                            if setValueIndex == idx {
                                AXUIElementSetMessagingTimeout(cur.el, 0.35)
                                var isSettable: DarwinBoolean = false
                                let chkErr = AXUIElementIsAttributeSettable(cur.el, kAXValueAttribute as CFString, &isSettable)
                                if chkErr == .success && isSettable.boolValue {
                                    let _ = AXUIElementSetAttributeValue(cur.el, kAXFocusedAttribute as CFString, kCFBooleanTrue)
                                    let setErr = AXUIElementSetAttributeValue(cur.el, kAXValueAttribute as CFString, valueText as CFString)
                                    if setErr == .success {
                                        let readBack = isPassword ? valueText : axString(cur.el, kAXValueAttribute as String)
                                        if readBack == valueText || valueText.isEmpty {
                                            return [
                                                "action": "set_value",
                                                "method": "AXValue",
                                                "index": idx,
                                                "name": name,
                                            ]
                                        }
                                    }
                                }
                                return [
                                    "action": "set_value",
                                    "method": "fallback_needed",
                                    "index": idx,
                                    "cx": cx,
                                    "cy": cy,
                                ]
                            }

                            if invokeIndex == idx {
                                AXUIElementSetMessagingTimeout(cur.el, 0.35)
                                var actionsRef: CFArray?
                                let actErr = AXUIElementCopyActionNames(cur.el, &actionsRef)
                                let actions = (actErr == .success ? (actionsRef as? [String]) : nil) ?? []
                                for candidate in [kAXPressAction as String, kAXConfirmAction as String, "AXPick"] {
                                    if actions.contains(candidate) {
                                        if AXUIElementPerformAction(cur.el, candidate as CFString) == .success {
                                            return [
                                                "action": "invoke",
                                                "method": candidate,
                                                "index": idx,
                                                "name": name,
                                            ]
                                        }
                                    }
                                }
                                return [
                                    "action": "invoke",
                                    "method": "fallback_click",
                                    "index": idx,
                                    "cx": cx,
                                    "cy": cy,
                                ]
                            }

                            items.append([
                                "index": idx,
                                "depth": cur.depth,
                                "controlType": ct,
                                "axRole": role,
                                "name": name,
                                "automationId": aid,
                                "nativeHwnd": 0,
                                "enabled": enabled,
                                "value": val,
                                "bounds": [
                                    "left": Int(r.origin.x.rounded()),
                                    "top": Int(r.origin.y.rounded()),
                                    "width": Int(r.size.width.rounded()),
                                    "height": Int(r.size.height.rounded()),
                                    "cx": cx,
                                    "cy": cy,
                                ],
                            ])
                        }
                    }
                }
            }
        }

        if cur.depth < maxDepth && (CFAbsoluteTimeGetCurrent() - t0) < budgetSec && visited < maxVisited {
            var children = axCopyAttr(cur.el, kAXChildrenAttribute as String) as? [AXUIElement]
            if children == nil || children!.isEmpty {
                children = axCopyAttr(cur.el, kAXVisibleChildrenAttribute as String) as? [AXUIElement]
            }
            if let chList = children {
                for ch in chList {
                    queue.append(QNode(el: ch, depth: cur.depth + 1))
                }
            }
        }
    }

    return [
        "focusedElement": focusedStr ?? NSNull(),
        "count": items.count,
        "timedOut": (CFAbsoluteTimeGetCurrent() - t0) >= budgetSec || visited >= maxVisited,
        "elements": items,
    ]
}

// 120Hz Hardware CGEvent Mouse & Keyboard Engine

private func mouseTypes(for button: String) -> (down: CGEventType, up: CGEventType, drag: CGEventType, btn: CGMouseButton) {
    switch button.lowercased() {
    case "right":
        return (.rightMouseDown, .rightMouseUp, .rightMouseDragged, .right)
    case "middle":
        return (.otherMouseDown, .otherMouseUp, .otherMouseDragged, .center)
    default:
        return (.leftMouseDown, .leftMouseUp, .leftMouseDragged, .left)
    }
}

private func postMouseEvent(type: CGEventType, point: CGPoint, button: CGMouseButton, clickState: Int64 = 1) {
    let src = CGEventSource(stateID: .hidSystemState)
    if let ev = CGEvent(mouseEventSource: src, mouseType: type, mouseCursorPosition: point, mouseButton: button) {
        if type == .leftMouseDown || type == .leftMouseUp || type == .rightMouseDown || type == .rightMouseUp || type == .otherMouseDown || type == .otherMouseUp {
            ev.setIntegerValueField(.mouseEventClickState, value: clickState)
        }
        ev.post(tap: .cghidEventTap)
    }
}

private func performSmoothMove(tx: Double, ty: Double, duration: Double, checkAbort: Bool) -> [String: Any] {
    let start = currentCursorPos()
    let dx = tx - Double(start.x)
    let dy = ty - Double(start.y)
    let dist = hypot(dx, dy)
    let dest = CGPoint(x: tx, y: ty)
    if dist < 2.0 || duration <= 0.004 {
        postMouseEvent(type: .mouseMoved, point: dest, button: .left)
        return ["ok": true, "x": Int(tx.rounded()), "y": Int(ty.rounded())]
    }
    let dur = max(0.008, min(duration, 2.0))
    let steps = max(3, Int((dur * 120.0).rounded()))
    let dt = dur / Double(steps)
    let t0 = CFAbsoluteTimeGetCurrent()
    for i in 1...steps {
        if checkAbort && isEscapeKeyPressed() {
            return ["error": "emergency_stop: Physical Escape key detected during cursor glide."]
        }
        let t = Double(i) / Double(steps)
        let u = pow(t, 0.82)
        let s = u * u * u * (10.0 - 15.0 * u + 6.0 * u * u)
        let pt = CGPoint(x: Double(start.x) + dx * s, y: Double(start.y) + dy * s)
        postMouseEvent(type: .mouseMoved, point: pt, button: .left)
        let targetT = t0 + Double(i) * dt
        let rem = targetT - CFAbsoluteTimeGetCurrent()
        if rem > 0.0015 {
            Thread.sleep(forTimeInterval: rem - 0.0008)
        }
        while CFAbsoluteTimeGetCurrent() < targetT {}
    }
    postMouseEvent(type: .mouseMoved, point: dest, button: .left)
    return ["ok": true, "x": Int(tx.rounded()), "y": Int(ty.rounded())]
}

private func performMouseClick(x: Double, y: Double, button: String, clicks: Int, interval: Double) -> [String: Any] {
    let mt = mouseTypes(for: button)
    let pt = CGPoint(x: x, y: y)
    let cnt = max(1, min(clicks, 3))
    postMouseEvent(type: .mouseMoved, point: pt, button: mt.btn)
    for i in 1...cnt {
        postMouseEvent(type: mt.down, point: pt, button: mt.btn, clickState: Int64(i))
        Thread.sleep(forTimeInterval: 0.008)
        postMouseEvent(type: mt.up, point: pt, button: mt.btn, clickState: Int64(i))
        if i < cnt && interval > 0 {
            Thread.sleep(forTimeInterval: interval)
        }
    }
    return ["ok": true]
}

private func performMouseDrag(samples: [[Double]], button: String, duration: Double, holdMs: Int) -> [String: Any] {
    guard samples.count >= 2 else { return ["error": "drag_requires_2_samples"] }
    let mt = mouseTypes(for: button)
    let p0 = CGPoint(x: samples[0][0], y: samples[0][1])
    let pEnd = CGPoint(x: samples[samples.count - 1][0], y: samples[samples.count - 1][1])

    postMouseEvent(type: .mouseMoved, point: p0, button: mt.btn)
    postMouseEvent(type: mt.down, point: p0, button: mt.btn, clickState: 1)
    var released = false
    defer {
        if !released {
            postMouseEvent(type: mt.up, point: pEnd, button: mt.btn, clickState: 1)
        }
    }

    Thread.sleep(forTimeInterval: 0.004)
    let n = max(1, samples.count - 1)
    let dt = max(0.015, duration) / Double(n)
    let t0 = CFAbsoluteTimeGetCurrent()
    for i in 1..<samples.count {
        if isEscapeKeyPressed() {
            let curPt = CGPoint(x: samples[i][0], y: samples[i][1])
            postMouseEvent(type: mt.up, point: curPt, button: mt.btn, clickState: 1)
            released = true
            return ["error": "emergency_stop: Physical Escape key detected during drag path; grants revoked."]
        }
        let pt = CGPoint(x: samples[i][0], y: samples[i][1])
        postMouseEvent(type: mt.drag, point: pt, button: mt.btn)
        let targetT = t0 + Double(i) * dt
        let rem = targetT - CFAbsoluteTimeGetCurrent()
        if rem > 0.0015 {
            Thread.sleep(forTimeInterval: rem - 0.0008)
        }
        while CFAbsoluteTimeGetCurrent() < targetT {}
    }
    postMouseEvent(type: mt.drag, point: pEnd, button: mt.btn)
    let holdSec = max(0.004, Double(max(0, min(holdMs, 1000))) / 1000.0)
    Thread.sleep(forTimeInterval: holdSec)
    postMouseEvent(type: mt.up, point: pEnd, button: mt.btn, clickState: 1)
    released = true
    return ["ok": true]
}

private func performMouseScroll(x: Double, y: Double, clicks: Int, horizontal: Bool) -> [String: Any] {
    let pt = CGPoint(x: x, y: y)
    postMouseEvent(type: .mouseMoved, point: pt, button: .left)
    let steps = max(-50, min(clicks, 50))
    if steps == 0 { return ["ok": true] }
    let src = CGEventSource(stateID: .hidSystemState)
    let delta = Int32(steps * 3)
    if horizontal {
        if let ev = CGEvent(scrollWheelEvent2Source: src, units: .line, wheelCount: 2, wheel1: 0, wheel2: delta, wheel3: 0) {
            ev.post(tap: .cghidEventTap)
        }
    } else {
        if let ev = CGEvent(scrollWheelEvent2Source: src, units: .line, wheelCount: 1, wheel1: delta, wheel2: 0, wheel3: 0) {
            ev.post(tap: .cghidEventTap)
        }
    }
    return ["ok": true]
}

private let _macKeyCodes: [String: (code: CGKeyCode, shift: Bool)] = [
    "a": (0, false), "s": (1, false), "d": (2, false), "f": (3, false),
    "h": (4, false), "g": (5, false), "z": (6, false), "x": (7, false),
    "c": (8, false), "v": (9, false), "b": (11, false), "q": (12, false),
    "w": (13, false), "e": (14, false), "r": (15, false), "y": (16, false),
    "t": (17, false), "1": (18, false), "2": (19, false), "3": (20, false),
    "4": (21, false), "6": (22, false), "5": (23, false), "=": (24, false),
    "+": (24, true), "plus": (24, true), "add": (69, false),
    "9": (25, false), "7": (26, false), "-": (27, false), "minus": (27, false),
    "subtract": (78, false), "8": (28, false), "0": (29, false),
    "]": (30, false), "o": (31, false), "u": (32, false), "[": (33, false),
    "i": (34, false), "p": (35, false), "return": (36, false), "enter": (36, false),
    "l": (37, false), "j": (38, false), "'": (39, false), "k": (40, false),
    ";": (41, false), "\\": (42, false), ",": (43, false), "/": (44, false),
    "divide": (75, false), "multiply": (67, false), "decimal": (65, false),
    "n": (45, false), "m": (46, false), ".": (47, false),
    "tab": (48, false), "space": (49, false), "spacebar": (49, false), "`": (50, false),
    "backspace": (51, false), "back": (51, false), "esc": (53, false), "escape": (53, false),
    "delete": (117, false), "del": (117, false),
    "home": (115, false), "pageup": (116, false), "pgup": (116, false),
    "end": (119, false), "pagedown": (121, false), "pgdn": (121, false),
    "left": (123, false), "right": (124, false), "down": (125, false), "up": (126, false),
    "f1": (122, false), "f2": (120, false), "f3": (99, false), "f4": (118, false),
    "f5": (96, false), "f6": (97, false), "f7": (98, false), "f8": (100, false),
    "f9": (101, false), "f10": (109, false), "f11": (103, false), "f12": (111, false),
    "f13": (105, false), "f14": (107, false), "f15": (113, false), "f16": (106, false),
    "f17": (64, false), "f18": (79, false), "f19": (80, false), "f20": (90, false),
    "capslock": (57, false),
]

private let _macModifiers: [String: (flag: CGEventFlags, code: CGKeyCode)] = [
    "cmd": (.maskCommand, 55), "command": (.maskCommand, 55), "meta": (.maskCommand, 55), "super": (.maskCommand, 55),
    "shift": (.maskShift, 56), "shiftleft": (.maskShift, 56), "shiftright": (.maskShift, 60),
    "alt": (.maskAlternate, 58), "opt": (.maskAlternate, 58), "option": (.maskAlternate, 58),
    "altleft": (.maskAlternate, 58), "altright": (.maskAlternate, 61),
    "ctrl": (.maskControl, 59), "control": (.maskControl, 59), "ctrlleft": (.maskControl, 59), "ctrlright": (.maskControl, 62),
]

private func performKeyChord(keys: [String]) -> [String: Any] {
    enum ChordKey {
        case vkey(CGKeyCode)
        case unicode(String)
    }

    let src = CGEventSource(stateID: .hidSystemState)
    var modFlags: CGEventFlags = []
    var modCodes: [CGKeyCode] = []
    var mainItems: [ChordKey] = []

    for raw in keys {
        let low = raw.trimmingCharacters(in: .whitespaces).lowercased()
        if let mod = _macModifiers[low] {
            modFlags.insert(mod.flag)
            modCodes.append(mod.code)
        } else if let kc = _macKeyCodes[low] {
            mainItems.append(.vkey(kc.code))
            if kc.shift && !modFlags.contains(.maskShift) {
                modFlags.insert(.maskShift)
                modCodes.append(56)
            }
        } else if raw.count == 1 {
            mainItems.append(.unicode(raw))
        } else {
            return ["error": "Unsupported macOS key token: '\(raw)'"]
        }
    }

    for mc in modCodes {
        if let ev = CGEvent(keyboardEventSource: src, virtualKey: mc, keyDown: true) {
            ev.flags = modFlags
            ev.post(tap: .cghidEventTap)
        }
    }
    if mainItems.isEmpty && !modCodes.isEmpty {
        Thread.sleep(forTimeInterval: 0.015)
    }
    for item in mainItems {
        switch item {
        case .vkey(let code):
            if let down = CGEvent(keyboardEventSource: src, virtualKey: code, keyDown: true) {
                down.flags = modFlags
                down.post(tap: .cghidEventTap)
            }
            Thread.sleep(forTimeInterval: 0.008)
            if let up = CGEvent(keyboardEventSource: src, virtualKey: code, keyDown: false) {
                up.flags = modFlags
                up.post(tap: .cghidEventTap)
            }
        case .unicode(let ch):
            let utf16 = Array(ch.utf16)
            if let down = CGEvent(keyboardEventSource: src, virtualKey: 0, keyDown: true),
               let up = CGEvent(keyboardEventSource: src, virtualKey: 0, keyDown: false) {
                down.flags = modFlags
                up.flags = modFlags
                down.keyboardSetUnicodeString(stringLength: utf16.count, unicodeString: utf16)
                up.keyboardSetUnicodeString(stringLength: utf16.count, unicodeString: utf16)
                down.post(tap: .cghidEventTap)
                Thread.sleep(forTimeInterval: 0.008)
                up.post(tap: .cghidEventTap)
            }
        }
    }
    for mc in modCodes.reversed() {
        if let ev = CGEvent(keyboardEventSource: src, virtualKey: mc, keyDown: false) {
            ev.post(tap: .cghidEventTap)
        }
    }
    return ["ok": true]
}

private func performTypeText(text: String, interval: Double) -> [String: Any] {
    let src = CGEventSource(stateID: .hidSystemState)
    var count = 0
    for ch in text {
        if count % 16 == 0 && isEscapeKeyPressed() {
            return ["error": "emergency_stop: Physical Escape key detected during typing."]
        }
        if ch == "\r" { continue }
        if ch == "\n" {
            let _ = performKeyChord(keys: ["return"])
        } else if ch == "\t" {
            let _ = performKeyChord(keys: ["tab"])
        } else {
            let utf16 = Array(String(ch).utf16)
            if let down = CGEvent(keyboardEventSource: src, virtualKey: 0, keyDown: true),
               let up = CGEvent(keyboardEventSource: src, virtualKey: 0, keyDown: false) {
                down.keyboardSetUnicodeString(stringLength: utf16.count, unicodeString: utf16)
                up.keyboardSetUnicodeString(stringLength: utf16.count, unicodeString: utf16)
                down.post(tap: .cghidEventTap)
                up.post(tap: .cghidEventTap)
            }
        }
        count += 1
        if interval > 0 {
            Thread.sleep(forTimeInterval: min(interval, 0.25))
        } else {
            Thread.sleep(forTimeInterval: 0.001)
        }
    }
    return ["ok": true, "characters": count]
}

private func handleRequest(_ req: [String: Any]) -> [String: Any] {
    if (req["ping"] as? Bool) == true {
        return ["pong": true]
    }
    let cmd = (req["cmd"] as? String) ?? ""
    let hudPid = (req["hudPid"] as? NSNumber)?.intValue ?? 0

    switch cmd {
    case "status":
        let (monitors, vb) = displayRectsAndBounds()
        let cur = currentCursorPos()
        let locked = isScreenLocked()
        return [
            "accessibilityTrusted": AXIsProcessTrusted(),
            "screenRecordingGranted": CGPreflightScreenCaptureAccess(),
            "desktopName": locked ? "Locked" : "Default",
            "desktopLocked": locked,
            "cursor": ["x": Int(cur.x.rounded()), "y": Int(cur.y.rounded())],
            "virtualDesktop": vb,
            "monitors": monitors,
            "escapePressed": isEscapeKeyPressed(),
            "foregroundHwnd": findForegroundHwnd(hudPid: hudPid),
            "frontmostPid": Int(frontmostAppPid()),
        ]
    case "escape_pressed":
        return ["pressed": isEscapeKeyPressed()]
    case "cursor_pos":
        let cur = currentCursorPos()
        return ["x": Int(cur.x.rounded()), "y": Int(cur.y.rounded())]
    case "list_windows":
        let incMin = (req["includeMinimized"] as? Bool) ?? true
        let reqTitle = (req["requireTitle"] as? Bool) ?? true
        let wins = listWindows(includeMinimized: incMin, requireTitle: reqTitle, hudPid: hudPid)
        return ["windows": wins, "foregroundHwnd": findForegroundHwnd(hudPid: hudPid)]
    case "inspect_window":
        let hwnd = (req["hwnd"] as? NSNumber)?.intValue ?? 0
        if let info = inspectWindow(hwnd: hwnd, hudPid: hudPid) {
            return ["found": true, "window": info]
        }
        return ["found": false]
    case "detect_popup":
        let hwnd = (req["hwnd"] as? NSNumber)?.intValue ?? 0
        return detectPopup(ownerHwnd: hwnd, hudPid: hudPid)
    case "hit_test":
        let x = (req["x"] as? NSNumber)?.doubleValue ?? 0
        let y = (req["y"] as? NSNumber)?.doubleValue ?? 0
        return hitTestPoint(x: x, y: y, hudPid: hudPid)
    case "activate_window":
        let hwnd = (req["hwnd"] as? NSNumber)?.intValue ?? 0
        return activateWindow(hwnd: hwnd, hudPid: hudPid)
    case "ui_tree":
        let hwnd = (req["hwnd"] as? NSNumber)?.intValue ?? 0
        let maxEl = (req["maxElements"] as? NSNumber)?.intValue ?? 80
        let setIdx = (req["setValueIndex"] as? NSNumber)?.intValue ?? -1
        let valText = (req["valueText"] as? String) ?? ""
        let invIdx = (req["invokeIndex"] as? NSNumber)?.intValue ?? -1
        return queryUITree(hwnd: hwnd, maxElements: maxEl, setValueIndex: setIdx, valueText: valText, invokeIndex: invIdx, hudPid: hudPid)
    case "mouse_move":
        let x = (req["x"] as? NSNumber)?.doubleValue ?? 0
        let y = (req["y"] as? NSNumber)?.doubleValue ?? 0
        let dur = (req["duration"] as? NSNumber)?.doubleValue ?? 0.0
        let checkAbort = (req["checkAbort"] as? Bool) ?? true
        return performSmoothMove(tx: x, ty: y, duration: dur, checkAbort: checkAbort)
    case "mouse_click":
        let x = (req["x"] as? NSNumber)?.doubleValue ?? 0
        let y = (req["y"] as? NSNumber)?.doubleValue ?? 0
        let button = (req["button"] as? String) ?? "left"
        let clicks = (req["clicks"] as? NSNumber)?.intValue ?? 1
        let interval = (req["interval"] as? NSNumber)?.doubleValue ?? 0.04
        return performMouseClick(x: x, y: y, button: button, clicks: clicks, interval: interval)
    case "mouse_button":
        let x = (req["x"] as? NSNumber)?.doubleValue ?? Double(currentCursorPos().x)
        let y = (req["y"] as? NSNumber)?.doubleValue ?? Double(currentCursorPos().y)
        let button = (req["button"] as? String) ?? "left"
        let down = (req["down"] as? Bool) ?? true
        let mt = mouseTypes(for: button)
        postMouseEvent(type: down ? mt.down : mt.up, point: CGPoint(x: x, y: y), button: mt.btn, clickState: 1)
        return ["ok": true]
    case "mouse_drag":
        let rawSamples = (req["samples"] as? [[NSNumber]]) ?? []
        let samples = rawSamples.compactMap { pair -> [Double]? in
            guard pair.count >= 2 else { return nil }
            return [pair[0].doubleValue, pair[1].doubleValue]
        }
        let button = (req["button"] as? String) ?? "left"
        let dur = (req["duration"] as? NSNumber)?.doubleValue ?? 0.1
        let holdMs = (req["holdMs"] as? NSNumber)?.intValue ?? 0
        return performMouseDrag(samples: samples, button: button, duration: dur, holdMs: holdMs)
    case "mouse_scroll":
        let x = (req["x"] as? NSNumber)?.doubleValue ?? 0
        let y = (req["y"] as? NSNumber)?.doubleValue ?? 0
        let clicks = (req["clicks"] as? NSNumber)?.intValue ?? -3
        let horiz = (req["horizontal"] as? Bool) ?? false
        return performMouseScroll(x: x, y: y, clicks: clicks, horizontal: horiz)
    case "key_chord":
        let keys = (req["keys"] as? [String]) ?? []
        return performKeyChord(keys: keys)
    case "type_text":
        let text = (req["text"] as? String) ?? ""
        let interval = (req["interval"] as? NSNumber)?.doubleValue ?? 0.002
        return performTypeText(text: text, interval: interval)
    default:
        return ["error": "unknown_cmd: \(cmd)"]
    }
}

// Entrypoint
var parentPid: pid_t = 0
var oneShotJSON: String? = nil
let args = CommandLine.arguments
var i = 1
while i < args.count {
    if args[i] == "--parent-pid", i + 1 < args.count {
        parentPid = pid_t(Int32(args[i + 1]) ?? 0)
        i += 2
    } else if args[i] == "--cmd", i + 1 < args.count {
        oneShotJSON = args[i + 1]
        i += 2
    } else {
        i += 1
    }
}

if let oneShot = oneShotJSON {
    if let data = oneShot.data(using: .utf8),
       let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
        writeJSON(handleRequest(obj))
    } else {
        writeJSON(["error": "invalid_json"])
    }
    exit(0)
}

if parentPid > 1 {
    startParentWatcher(parentPid)
}

// Daemon stdio JSON-lines loop
writeJSON(["pong": true])
while let line = readLine(strippingNewline: true) {
    let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
    if trimmed.isEmpty { continue }
    autoreleasepool {
        if let data = trimmed.data(using: .utf8),
           let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            writeJSON(handleRequest(obj))
        } else {
            writeJSON(["error": "invalid_json"])
        }
    }
}
exit(0)
