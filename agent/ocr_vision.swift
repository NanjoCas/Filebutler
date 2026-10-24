// Text in images, by macOS Vision (on device). Built once by agent/ocr.py.
// Usage: ocr-vision IMAGE...   Output: one JSON line per image, {"path", "text"} or {"path", "error"}.
import Foundation
import ImageIO
import Vision

struct Result: Codable { let path: String; let text: String?; let error: String? }

func recognize(_ path: String) -> Result {
    let url = URL(fileURLWithPath: path)
    guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
          let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
        return Result(path: path, text: nil, error: "无法打开图片")
    }
    // Phone photos are stored sideways with an EXIF orientation; Vision must be told.
    let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any]
    let raw = properties?[kCGImagePropertyOrientation] as? UInt32 ?? 1
    let orientation = CGImagePropertyOrientation(rawValue: raw) ?? .up
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    let wanted = ["zh-Hans", "zh-Hant", "ja-JP", "en-US"]
    let supported = (try? request.supportedRecognitionLanguages()) ?? []
    request.recognitionLanguages = wanted.filter { supported.contains($0) }
    do {
        try VNImageRequestHandler(cgImage: image, orientation: orientation, options: [:]).perform([request])
        let lines = (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }
        return Result(path: path, text: lines.joined(separator: "\n"), error: nil)
    } catch {
        return Result(path: path, text: nil, error: "识别失败：\(error.localizedDescription)")
    }
}

let encoder = JSONEncoder()
for path in CommandLine.arguments.dropFirst() {
    autoreleasepool {
        let line = (try? encoder.encode(recognize(path))).flatMap { String(data: $0, encoding: .utf8) }
        print(line ?? "{\"path\":\"\",\"error\":\"输出编码失败\"}")
    }
}
