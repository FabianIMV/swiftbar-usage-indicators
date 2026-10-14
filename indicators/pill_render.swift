import AppKit
import Foundation

// Renders a pill at scale 1 (1px = 1pt in the menu bar) with bold white text
// and a drop shadow, and writes the PNG as base64 to stdout (no newline).
// Usage: pill_render "text" "baseHex" ["topHex"] ["heightPt"]

func colorFromHex(_ hex: String) -> NSColor? {
    var s = hex.trimmingCharacters(in: .whitespacesAndNewlines)
    if s.hasPrefix("#") { s.removeFirst() }
    guard s.count == 6, let v = UInt32(s, radix: 16) else { return nil }
    let r = CGFloat((v >> 16) & 0xff) / 255.0
    let g = CGFloat((v >> 8) & 0xff) / 255.0
    let b = CGFloat(v & 0xff) / 255.0
    return NSColor(srgbRed: r, green: g, blue: b, alpha: 1.0)
}

func lighten(_ c: NSColor, _ amt: CGFloat) -> NSColor {
    let c2 = c.usingColorSpace(.sRGB) ?? c
    var r: CGFloat = 0, g: CGFloat = 0, b: CGFloat = 0, a: CGFloat = 0
    c2.getRed(&r, green: &g, blue: &b, alpha: &a)
    return NSColor(
        srgbRed: min(1.0, r + (1.0 - r) * amt),
        green: min(1.0, g + (1.0 - g) * amt),
        blue: min(1.0, b + (1.0 - b) * amt),
        alpha: a
    )
}

func darken(_ c: NSColor, _ amt: CGFloat) -> NSColor {
    let c2 = c.usingColorSpace(.sRGB) ?? c
    var r: CGFloat = 0, g: CGFloat = 0, b: CGFloat = 0, a: CGFloat = 0
    c2.getRed(&r, green: &g, blue: &b, alpha: &a)
    return NSColor(
        srgbRed: max(0.0, r * (1.0 - amt)),
        green: max(0.0, g * (1.0 - amt)),
        blue: max(0.0, b * (1.0 - amt)),
        alpha: a
    )
}

let args = CommandLine.arguments
guard args.count >= 3 else {
    fputs("usage: pill_render \"text\" \"baseHex\" [\"topHex\"] [heightPt]\n", stderr)
    exit(1)
}

var text = args[1]
if text.isEmpty { text = " " }
guard let base = colorFromHex(args[2]) else {
    fputs("pill_render: invalid base hex\n", stderr)
    exit(2)
}

var top: NSColor
if args.count >= 4 && !args[3].isEmpty {
    guard let t = colorFromHex(args[3]) else {
        fputs("pill_render: invalid top hex\n", stderr)
        exit(2)
    }
    top = t
} else {
    top = lighten(base, 0.06)
}

var hPt: CGFloat = 17.0
if args.count >= 5, let h = Double(args[4]), h >= 12, h <= 26 {
    hPt = CGFloat(h)
}

// Look: SF Bold ~12.5pt, pure white, strong black shadow.
let fontSize: CGFloat = hPt - 4.5
// Compact padding so the pills fit on crowded menu bars. The final width
// comes from the short text the plugin passes (just the percentage).
let padH: CGFloat = 7.0
let font = NSFont.systemFont(ofSize: fontSize, weight: .bold)

let shadow = NSShadow()
shadow.shadowColor = NSColor.black.withAlphaComponent(0.75)
shadow.shadowOffset = NSSize(width: 0, height: -1)
shadow.shadowBlurRadius = 2.0

let paragraph = NSMutableParagraphStyle()
paragraph.alignment = .center
let attrs: [NSAttributedString.Key: Any] = [
    .font: font,
    .foregroundColor: NSColor.white,
    .shadow: shadow,
    .paragraphStyle: paragraph,
]
let attrStr = NSAttributedString(string: text, attributes: attrs)
let textSize = attrStr.size()
let wPt = ceil(textSize.width) + padH * 2.0

// Scale-1 canvas: SwiftBar draws the base64 image at native pixels and
// ignores pHYs/DPI, so 1px = 1pt in the bar. The pixel height must equal the
// final logical height (~17px for the menu bar row).
let scale: CGFloat = 1.0
let wPx = Int(ceil(wPt * scale))
let hPx = Int(ceil(hPt * scale))
guard
    let rep = NSBitmapImageRep(
        bitmapDataPlanes: nil,
        pixelsWide: wPx,
        pixelsHigh: hPx,
        bitsPerSample: 8,
        samplesPerPixel: 4,
        hasAlpha: true,
        isPlanar: false,
        colorSpaceName: .deviceRGB,
        bytesPerRow: 0,
        bitsPerPixel: 0
    )
else {
    fputs("pill_render: could not create the bitmap\n", stderr)
    exit(3)
}
rep.size = NSSize(width: wPt, height: hPt)

NSGraphicsContext.saveGraphicsState()
defer { NSGraphicsContext.restoreGraphicsState() }
guard let nctx = NSGraphicsContext(bitmapImageRep: rep) else {
    fputs("pill_render: could not create the graphics context\n", stderr)
    exit(3)
}
NSGraphicsContext.current = nctx
guard let cg = NSGraphicsContext.current?.cgContext else {
    fputs("pill_render: no cgContext\n", stderr)
    exit(3)
}
cg.scaleBy(x: scale, y: scale)

// Background: full pill (radius = half the height) with a subtle gradient.
let radius = hPt / 2.0
let bgPath = NSBezierPath(
    roundedRect: NSRect(x: 0.5, y: 0.5, width: wPt - 1.0, height: hPt - 1.0),
    xRadius: radius,
    yRadius: radius
)
if let grad = NSGradient(colors: [base, top], atLocations: [0.0, 1.0], colorSpace: .deviceRGB) {
    grad.draw(in: bgPath, angle: 90.0)
} else {
    base.setFill()
    bgPath.fill()
}

// 1px darker border for depth.
darken(base, 0.22).setStroke()
bgPath.lineWidth = 1.0
bgPath.stroke()

// Subtle top highlight (10% white, 1px) clipped to the pill.
NSGraphicsContext.saveGraphicsState()
bgPath.setClip()
NSColor.white.withAlphaComponent(0.10).setFill()
NSRect(x: 1.0, y: hPt - 2.5, width: wPt - 2.0, height: 1.0).fill(using: .sourceOver)
NSGraphicsContext.restoreGraphicsState()

// Centered text, nudged up slightly for optical balance.
let tx = padH
let ty = (hPt - textSize.height) / 2.0 + 0.5
attrStr.draw(at: NSPoint(x: tx, y: ty))

guard let png = rep.representation(using: .png, properties: [:]) else {
    fputs("pill_render: could not encode the PNG\n", stderr)
    exit(4)
}
FileHandle.standardOutput.write(Data(png.base64EncodedString().utf8))
