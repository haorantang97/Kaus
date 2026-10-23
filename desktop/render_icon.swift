// Vector-only rendering of the existing sidebar wordmark, not a new logo.
import AppKit
import CoreText
import Foundation

let fontURL = URL(fileURLWithPath: CommandLine.arguments[1])
let output = URL(fileURLWithPath: CommandLine.arguments[2])
let temporary = URL(fileURLWithPath: CommandLine.arguments[3])
guard let provider = CGDataProvider(url: fontURL as CFURL), let fontData = CGFont(provider) else {
    fatalError("Cannot read the repository's Anton font")
}
let font = CTFontCreateWithGraphicsFont(fontData, 1000, nil, nil)
let line = CTLineCreateWithAttributedString(NSAttributedString(string: "KAUS", attributes: [
    NSAttributedString.Key(kCTFontAttributeName as String): font,
    NSAttributedString.Key(kCTKernAttributeName as String): 20,
]))
let letters = CGMutablePath()
for run in CTLineGetGlyphRuns(line) as! [CTRun] {
    let count = CTRunGetGlyphCount(run)
    var glyphs = [CGGlyph](repeating: 0, count: count)
    var positions = [CGPoint](repeating: .zero, count: count)
    CTRunGetGlyphs(run, CFRange(location: 0, length: 0), &glyphs)
    CTRunGetPositions(run, CFRange(location: 0, length: 0), &positions)
    for index in 0..<count {
        if let outline = CTFontCreatePathForGlyph(font, glyphs[index], nil) {
            letters.addPath(outline, transform: CGAffineTransform(translationX: positions[index].x, y: positions[index].y))
        }
    }
}
// CSS uses skewX(-7deg) with y pointing down. Core Graphics points up.
let slanted = CGMutablePath()
slanted.addPath(letters, transform: CGAffineTransform(a: 1, b: 0, c: tan(7 * .pi / 180), d: 1, tx: 0, ty: 0))
let bounds = slanted.boundingBoxOfPath
let scale = 660 / bounds.width
let wordmark = CGMutablePath()
wordmark.addPath(slanted, transform: CGAffineTransform(a: scale, b: 0, c: 0, d: scale, tx: 512 - bounds.midX * scale, ty: 512 - bounds.midY * scale))

// Smooth rounded-square silhouette with a 64px transparent margin.
let tile = CGMutablePath()
let steps = 1024
for index in 0..<steps {
    let angle = Double(index) * 2 * .pi / Double(steps)
    let x = cos(angle), y = sin(angle)
    let point = CGPoint(x: 512 + 448 * (x < 0 ? -1 : 1) * pow(abs(x), 2 / 4.6), y: 512 + 448 * (y < 0 ? -1 : 1) * pow(abs(y), 2 / 4.6))
    if index == 0 { tile.move(to: point) } else { tile.addLine(to: point) }
}
tile.closeSubpath()

func color(_ red: CGFloat, _ green: CGFloat, _ blue: CGFloat) -> CGColor {
    CGColor(srgbRed: red / 255, green: green / 255, blue: blue / 255, alpha: 1)
}

func render(size: Int, destination: URL) throws {
    let space = CGColorSpace(name: CGColorSpace.sRGB)!
    guard let context = CGContext(data: nil, width: size, height: size, bitsPerComponent: 8, bytesPerRow: size * 4, space: space, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { fatalError("Cannot create icon canvas") }
    context.clear(CGRect(x: 0, y: 0, width: size, height: size))
    context.scaleBy(x: CGFloat(size) / 1024, y: CGFloat(size) / 1024)
    context.setFillColor(color(249, 249, 247))
    context.addPath(tile); context.fillPath()
    context.setFillColor(color(94, 122, 18))
    context.addPath(wordmark); context.fillPath()
    let bitmap = NSBitmapImageRep(cgImage: context.makeImage()!)
    guard let png = bitmap.representation(using: .png, properties: [:]) else { fatalError("Cannot encode icon") }
    try png.write(to: destination)
}

func svgPath(_ path: CGPath) -> String {
    var commands: [String] = []
    func point(_ p: CGPoint) -> String { String(format: "%.4f %.4f", Double(p.x), Double(p.y)) }
    path.applyWithBlock { pointer in
        let element = pointer.pointee
        switch element.type {
        case .moveToPoint: commands.append("M" + point(element.points[0]))
        case .addLineToPoint: commands.append("L" + point(element.points[0]))
        case .addQuadCurveToPoint: commands.append("Q" + point(element.points[0]) + " " + point(element.points[1]))
        case .addCurveToPoint: commands.append("C" + point(element.points[0]) + " " + point(element.points[1]) + " " + point(element.points[2]))
        case .closeSubpath: commands.append("Z")
        @unknown default: fatalError("Unknown vector segment")
        }
    }
    return commands.joined(separator: " ")
}

let svg = """
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024" role="img" aria-label="KAUS">
  <!-- Existing Anton wordmark: 0.02em tracking, -7deg CSS skew, light-theme green. -->
  <g transform="translate(0 1024) scale(1 -1)">
    <path fill="#f9f9f7" d="\(svgPath(tile))"/>
    <path fill="#5e7a12" d="\(svgPath(wordmark))"/>
  </g>
</svg>
"""
try svg.write(to: output.appendingPathComponent("AppIcon.svg"), atomically: true, encoding: .utf8)
try render(size: 1024, destination: output.appendingPathComponent("AppIcon.png"))
let iconset = temporary.appendingPathComponent("AppIcon.iconset")
try FileManager.default.createDirectory(at: iconset, withIntermediateDirectories: true)
for base in [16, 32, 128, 256, 512] {
    for resolution in [1, 2] {
        let suffix = resolution == 2 ? "@2x" : ""
        try render(size: base * resolution, destination: iconset.appendingPathComponent("icon_\(base)x\(base)\(suffix).png"))
    }
}
