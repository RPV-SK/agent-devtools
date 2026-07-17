// appdriver — native macOS window automation CLI for local development.
//
// Lets a dev session SEE and DRIVE a real native app window (not just
// its HTTP engine): list windows, screenshot one, click, type, bring an app
// frontmost, and report permission state.
//
// This is LOCAL automation of the user's own apps on the user's own machine.
// macOS gates the sensitive parts behind user-granted permissions:
//   * Screen Recording  — needed by `shot` (and full window titles in
//     `windows`); granted in System Settings → Privacy & Security →
//     Screen Recording for the app that LAUNCHES appdriver (the terminal /
//     agent host), then that app must be fully quit and reopened.
//   * Accessibility     — needed by `click` / `key` (synthetic input);
//     granted in System Settings → Privacy & Security → Accessibility,
//     same "enable the launching app" rule.
// appdriver never tries to bypass those gates: when a permission is missing
// it prints the exact grant path on stderr and exits with a distinct code.
//
// Exit codes:
//   0  success
//   1  usage error
//   2  target app/window not found
//   3  Screen Recording permission missing (shot)
//   4  Accessibility permission missing (click / key)
//   5  capture failed for another reason (e.g. window vanished mid-shot)
//
// All informational output is JSON on stdout; diagnostics go to stderr.

import AppKit
import ApplicationServices
import CoreGraphics
import Foundation
import ImageIO
import UniformTypeIdentifiers

// MARK: - Exit plumbing

enum ExitCode: Int32 {
    case ok = 0
    case usage = 1
    case notFound = 2
    case screenRecordingDenied = 3
    case axUntrusted = 4
    case captureFailed = 5
}

func stderrPrint(_ message: String) {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
}

func die(_ message: String, _ code: ExitCode) -> Never {
    stderrPrint("appdriver: " + message)
    exit(code.rawValue)
}

func emitJSON(_ object: Any) {
    guard
        JSONSerialization.isValidJSONObject(object),
        let data = try? JSONSerialization.data(
            withJSONObject: object, options: [.sortedKeys, .prettyPrinted])
    else {
        die("internal error: result was not JSON-encodable", .captureFailed)
    }
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write("\n".data(using: .utf8)!)
}

let screenRecordingGuidance = """
    appdriver: the capture came back empty — Screen Recording permission is not
    granted to the app that launched appdriver (Terminal / iTerm / the agent
    host process). To grant it:
      1. Open System Settings → Privacy & Security → Screen Recording
      2. Enable the launching app (add it with “+” if it is not listed)
      3. Fully quit and reopen that app, then re-run this command.
    Without the grant, macOS lets appdriver list windows but not read pixels.
    """

let axGuidance = """
    appdriver: this command synthesizes mouse/keyboard events, which macOS only
    allows for Accessibility-trusted processes. To grant it:
      1. Open System Settings → Privacy & Security → Accessibility
      2. Enable the app that launches appdriver (Terminal / iTerm / the agent
         host process); add it with “+” if it is not listed
      3. Re-run this command (no restart needed for Accessibility).
    """

// MARK: - Window listing

struct Win {
    let app: String
    let pid: Int
    let windowId: UInt32
    let title: String
    let x: Double, y: Double, w: Double, h: Double
    let onScreen: Bool
    let layer: Int

    var json: [String: Any] {
        [
            "app": app,
            "pid": pid,
            "windowId": Int(windowId),
            "title": title,
            "frame": ["x": x, "y": y, "w": w, "h": h],
            "onScreen": onScreen,
            "layer": layer,
        ]
    }
}

/// All windows known to the window server (front-to-back order), desktop
/// elements excluded. NOTE: `title` is only populated by macOS when the
/// caller has Screen Recording permission; without it titles are "".
func listWindows(appFilter: String?) -> [Win] {
    guard
        let raw = CGWindowListCopyWindowInfo(
            [.optionAll, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]]
    else { return [] }

    var out: [Win] = []
    for entry in raw {
        let owner = entry[kCGWindowOwnerName as String] as? String ?? ""
        if let filter = appFilter, !owner.localizedCaseInsensitiveContains(filter) {
            continue
        }
        let bounds = entry[kCGWindowBounds as String] as? [String: Any] ?? [:]
        func num(_ key: String) -> Double {
            (bounds[key] as? NSNumber)?.doubleValue ?? 0
        }
        out.append(
            Win(
                app: owner,
                pid: (entry[kCGWindowOwnerPID as String] as? NSNumber)?.intValue ?? 0,
                windowId: (entry[kCGWindowNumber as String] as? NSNumber)?.uint32Value ?? 0,
                title: entry[kCGWindowName as String] as? String ?? "",
                x: num("X"), y: num("Y"), w: num("Width"), h: num("Height"),
                onScreen: (entry[kCGWindowIsOnscreen as String] as? NSNumber)?.boolValue
                    ?? false,
                layer: (entry[kCGWindowLayer as String] as? NSNumber)?.intValue ?? 0
            ))
    }
    return out
}

func findWindow(byId id: UInt32) -> Win? {
    listWindows(appFilter: nil).first { $0.windowId == id }
}

/// Best main-window candidate for an app: prefer on-screen layer-0 windows,
/// largest area first.
func findWindow(byApp name: String) -> Win? {
    let candidates = listWindows(appFilter: name)
    let ranked = candidates.sorted { a, b in
        func score(_ w: Win) -> (Int, Int, Double) {
            (w.onScreen ? 1 : 0, w.layer == 0 ? 1 : 0, w.w * w.h)
        }
        return score(a) > score(b)
    }
    return ranked.first
}

// MARK: - Capture

/// True when the image is unusable: degenerate size or perfectly uniform
/// pixels (all-black / all-white / all-transparent), which is what a
/// permission-denied capture looks like.
func imageLooksEmpty(_ image: CGImage) -> Bool {
    if image.width <= 1 || image.height <= 1 { return true }
    let w = min(image.width, 32)
    let h = min(image.height, 32)
    guard
        let ctx = CGContext(
            data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: w * 4,
            space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)
    else { return true }
    ctx.interpolationQuality = .low
    ctx.draw(image, in: CGRect(x: 0, y: 0, width: w, height: h))
    guard let data = ctx.data else { return true }
    let buf = data.bindMemory(to: UInt8.self, capacity: w * h * 4)
    for i in stride(from: 4, to: w * h * 4, by: 4) {
        if buf[i] != buf[0] || buf[i + 1] != buf[1]
            || buf[i + 2] != buf[2] || buf[i + 3] != buf[3]
        {
            return false
        }
    }
    return true
}

func writePNG(_ image: CGImage, to path: String) -> Bool {
    let url = URL(fileURLWithPath: path) as CFURL
    guard
        let dest = CGImageDestinationCreateWithURL(
            url, UTType.png.identifier as CFString, 1, nil)
    else { return false }
    CGImageDestinationAddImage(dest, image, nil)
    return CGImageDestinationFinalize(dest)
}

func captureWindow(_ id: UInt32) -> CGImage? {
    CGWindowListCreateImage(
        .null, .optionIncludingWindow, id, [.boundsIgnoreFraming, .bestResolution])
}

/// Fallback path: /usr/sbin/screencapture -l<id>. Returns true when the file
/// exists afterwards and its content passes the non-empty check.
func screencaptureFallback(_ id: UInt32, to path: String) -> Bool {
    let proc = Process()
    proc.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
    proc.arguments = ["-x", "-o", "-l", String(id), path]
    do {
        try proc.run()
        proc.waitUntilExit()
    } catch {
        return false
    }
    guard proc.terminationStatus == 0,
        let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil),
        let img = CGImageSourceCreateImageAtIndex(src, 0, nil)
    else { return false }
    return !imageLooksEmpty(img)
}

// MARK: - Input synthesis

func requireAXTrusted() {
    if !AXIsProcessTrusted() {
        stderrPrint(axGuidance)
        exit(ExitCode.axUntrusted.rawValue)
    }
}

let eventSource = CGEventSource(stateID: .hidSystemState)

func postMouse(_ type: CGEventType, at point: CGPoint, clickState: Int64) {
    let event = CGEvent(
        mouseEventSource: eventSource, mouseType: type, mouseCursorPosition: point,
        mouseButton: .left)
    event?.setIntegerValueField(.mouseEventClickState, value: clickState)
    event?.post(tap: .cghidEventTap)
}

func clickAt(_ point: CGPoint, double: Bool) {
    postMouse(.mouseMoved, at: point, clickState: 0)
    usleep(30_000)
    postMouse(.leftMouseDown, at: point, clickState: 1)
    usleep(20_000)
    postMouse(.leftMouseUp, at: point, clickState: 1)
    if double {
        usleep(60_000)
        postMouse(.leftMouseDown, at: point, clickState: 2)
        usleep(20_000)
        postMouse(.leftMouseUp, at: point, clickState: 2)
    }
}

func typeText(_ text: String) -> Int {
    var typed = 0
    for ch in text {
        var units = Array(String(ch).utf16)
        let down = CGEvent(keyboardEventSource: eventSource, virtualKey: 0, keyDown: true)
        down?.keyboardSetUnicodeString(stringLength: units.count, unicodeString: &units)
        down?.post(tap: .cghidEventTap)
        let up = CGEvent(keyboardEventSource: eventSource, virtualKey: 0, keyDown: false)
        up?.keyboardSetUnicodeString(stringLength: units.count, unicodeString: &units)
        up?.post(tap: .cghidEventTap)
        usleep(8_000)
        typed += 1
    }
    return typed
}

func pressKey(code: CGKeyCode, flags: CGEventFlags) {
    let down = CGEvent(keyboardEventSource: eventSource, virtualKey: code, keyDown: true)
    down?.flags = flags
    down?.post(tap: .cghidEventTap)
    usleep(8_000)
    let up = CGEvent(keyboardEventSource: eventSource, virtualKey: code, keyDown: false)
    up?.flags = flags
    up?.post(tap: .cghidEventTap)
}

// MARK: - Tiny arg helper

struct Args {
    var positional: [String] = []
    var options: [String: String] = [:]
    var flags: Set<String> = []

    /// `valued`: option names that consume the next argument.
    init(_ raw: [String], valued: Set<String>) {
        var i = 0
        while i < raw.count {
            let a = raw[i]
            if a.hasPrefix("--") {
                let name = String(a.dropFirst(2))
                if valued.contains(name) {
                    guard i + 1 < raw.count else {
                        die("--\(name) needs a value", .usage)
                    }
                    options[name] = raw[i + 1]
                    i += 2
                } else {
                    flags.insert(name)
                    i += 1
                }
            } else {
                positional.append(a)
                i += 1
            }
        }
    }
}

// MARK: - Subcommands

func cmdWindows(_ args: Args) {
    let wins = listWindows(appFilter: args.options["app"])
    emitJSON(wins.map { $0.json })
}

func resolveTargetWindow(_ args: Args, context: String) -> Win {
    if let idStr = args.options["window"] {
        guard let id = UInt32(idStr) else { die("--window expects a numeric window id", .usage) }
        guard let win = findWindow(byId: id) else {
            die("no window with id \(id) (see `appdriver windows`)", .notFound)
        }
        return win
    }
    if let app = args.options["app"] {
        guard let win = findWindow(byApp: app) else {
            die("no window found for app matching \"\(app)\" (see `appdriver windows`)", .notFound)
        }
        return win
    }
    die("\(context) needs --window <id> or --app <name>", .usage)
}

func cmdShot(_ args: Args) {
    guard let outPath = args.positional.first else {
        die("shot needs an output path, e.g. appdriver shot --app YourApp out.png", .usage)
    }
    let win = resolveTargetWindow(args, context: "shot")

    if !CGPreflightScreenCaptureAccess() {
        stderrPrint(screenRecordingGuidance)
        exit(ExitCode.screenRecordingDenied.rawValue)
    }

    var via = "CGWindowListCreateImage"
    var finalImage: CGImage? = captureWindow(win.windowId)
    if let img = finalImage, imageLooksEmpty(img) { finalImage = nil }

    if let img = finalImage {
        guard writePNG(img, to: outPath) else {
            die("could not write PNG to \(outPath)", .captureFailed)
        }
        emitJSON([
            "ok": true, "path": outPath, "via": via,
            "width": img.width, "height": img.height,
            "windowId": Int(win.windowId), "app": win.app,
        ])
        return
    }

    // API path came back nil/empty despite preflight saying granted —
    // try the screencapture(1) fallback before concluding anything.
    via = "screencapture -l"
    if screencaptureFallback(win.windowId, to: outPath),
        let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: outPath) as CFURL, nil),
        let img = CGImageSourceCreateImageAtIndex(src, 0, nil)
    {
        emitJSON([
            "ok": true, "path": outPath, "via": via,
            "width": img.width, "height": img.height,
            "windowId": Int(win.windowId), "app": win.app,
        ])
        return
    }

    // Preflight said granted but both capture paths produced nothing usable.
    // Most likely the grant is stale (launching app not restarted) — say so.
    stderrPrint(screenRecordingGuidance)
    stderrPrint(
        "appdriver: note — macOS preflight reported Screen Recording as granted, "
            + "but both capture paths returned empty. If you just granted it, fully "
            + "quit and reopen the launching app. Otherwise the window may have "
            + "closed mid-capture.")
    exit(ExitCode.captureFailed.rawValue)
}

func cmdClick(_ args: Args) {
    guard args.positional.count >= 2,
        let x = Double(args.positional[0]), let y = Double(args.positional[1])
    else {
        die("click needs numeric coordinates: appdriver click <x> <y>", .usage)
    }
    if args.flags.contains("relative") && args.options["window"] == nil {
        die("--relative needs --window <id>", .usage)
    }
    requireAXTrusted()

    var point = CGPoint(x: x, y: y)
    var relativeTo: Int? = nil
    if let idStr = args.options["window"] {
        guard let id = UInt32(idStr) else { die("--window expects a numeric window id", .usage) }
        guard let win = findWindow(byId: id) else {
            die("no window with id \(id) (see `appdriver windows`)", .notFound)
        }
        // With --window the coordinates are window-relative (--relative is an
        // explicit alias for the same behaviour).
        point = CGPoint(x: win.x + x, y: win.y + y)
        relativeTo = Int(id)
    }
    let double = args.flags.contains("double")
    clickAt(point, double: double)
    var result: [String: Any] = [
        "ok": true, "x": point.x, "y": point.y, "double": double,
    ]
    if let id = relativeTo { result["relativeToWindow"] = id }
    emitJSON(result)
}

func cmdKey(_ args: Args) {
    let hasText = args.options["text"] != nil
    let hasCode = args.options["code"] != nil
    guard hasText != hasCode else {
        die("key needs exactly one of --text \"…\" or --code <CGKeyCode>", .usage)
    }
    requireAXTrusted()

    if let text = args.options["text"] {
        let n = typeText(text)
        emitJSON(["ok": true, "typed_chars": n])
        return
    }

    guard let code = UInt16(args.options["code"]!) else {
        die("--code expects a numeric CGKeyCode (e.g. 36 = Return)", .usage)
    }
    var flags: CGEventFlags = []
    var applied: [String] = []
    for (flag, mask) in
        [("cmd", CGEventFlags.maskCommand), ("shift", .maskShift),
         ("alt", .maskAlternate), ("ctrl", .maskControl)]
    {
        if args.flags.contains(flag) {
            flags.insert(mask)
            applied.append(flag)
        }
    }
    pressKey(code: CGKeyCode(code), flags: flags)
    emitJSON(["ok": true, "code": Int(code), "modifiers": applied])
}

func cmdFront(_ args: Args) {
    guard let name = args.options["app"] else {
        die("front needs --app <name>", .usage)
    }
    let matches = NSWorkspace.shared.runningApplications.filter {
        ($0.localizedName ?? "").localizedCaseInsensitiveContains(name)
            || ($0.bundleIdentifier ?? "").localizedCaseInsensitiveContains(name)
    }
    // Prefer real apps over similarly-named helper processes.
    guard
        let app = matches.first(where: { $0.activationPolicy == .regular })
            ?? matches.first
    else {
        die("no running app matching \"\(name)\"", .notFound)
    }
    let activated = app.activate(options: [])
    emitJSON([
        "ok": activated, "app": app.localizedName ?? name,
        "pid": Int(app.processIdentifier),
        "bundleId": app.bundleIdentifier ?? "",
    ])
}

func cmdStatus() {
    let ax = AXIsProcessTrusted()
    let screen = CGPreflightScreenCaptureAccess()

    // Secondary evidence: capture a small strip of the menubar and check it
    // is non-empty. Only meaningful when preflight says granted.
    var probe = "skipped (preflight says not granted)"
    if screen {
        let rect = CGRect(x: 0, y: 0, width: 8, height: 8)
        if let img = CGWindowListCreateImage(rect, .optionOnScreenOnly, kCGNullWindowID, []) {
            probe = imageLooksEmpty(img) ? "empty" : "ok"
        } else {
            probe = "failed"
        }
    }

    var out: [String: Any] = [
        "ax_trusted": ax,
        "screen_recording": screen,
        "screen_recording_probe": probe,
    ]
    if !ax {
        out["grant_accessibility"] =
            "System Settings → Privacy & Security → Accessibility → enable the launching app"
    }
    if !screen {
        out["grant_screen_recording"] =
            "System Settings → Privacy & Security → Screen Recording → enable the launching app, then quit + reopen it"
    }
    emitJSON(out)
}

// MARK: - Usage / dispatch

let usage = """
    appdriver — window list / screenshot / input CLI for local development.

    Local automation for the user's OWN apps on this machine. The sensitive
    capabilities are gated by macOS permissions that only the user can grant,
    in System Settings → Privacy & Security (grant them to the app that
    launches appdriver — the terminal or agent host — not to appdriver itself):
      Screen Recording  → shot (and window titles in `windows`)
      Accessibility     → click, key

    Usage:
      appdriver windows [--app <name>]            list windows as JSON
      appdriver shot (--window <id> | --app <name>) <out.png>
                                                  capture one window to PNG
      appdriver click <x> <y> [--window <id> [--relative]] [--double]
                                                  synthesize a left click
                                                  (with --window, x/y are
                                                  window-relative)
      appdriver key --text \"abc\"                  type text
      appdriver key --code <CGKeyCode> [--cmd --shift --alt --ctrl]
                                                  press one key with modifiers
      appdriver front --app <name>                bring an app frontmost
      appdriver status                            permission report (JSON)

    Exit codes: 0 ok · 1 usage · 2 not found · 3 screen-recording missing ·
                4 accessibility missing · 5 capture failed
    """

let argv = Array(CommandLine.arguments.dropFirst())
guard let command = argv.first, !["-h", "--help", "help"].contains(command) else {
    print(usage)
    exit(argv.first == nil ? ExitCode.usage.rawValue : ExitCode.ok.rawValue)
}
let rest = Array(argv.dropFirst())

switch command {
case "windows":
    cmdWindows(Args(rest, valued: ["app"]))
case "shot":
    cmdShot(Args(rest, valued: ["app", "window"]))
case "click":
    cmdClick(Args(rest, valued: ["window"]))
case "key":
    cmdKey(Args(rest, valued: ["text", "code"]))
case "front":
    cmdFront(Args(rest, valued: ["app"]))
case "status":
    cmdStatus()
default:
    stderrPrint("appdriver: unknown command \"\(command)\"\n")
    stderrPrint(usage)
    exit(ExitCode.usage.rawValue)
}
