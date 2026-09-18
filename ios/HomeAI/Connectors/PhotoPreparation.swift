import Foundation
import ImageIO
import UniformTypeIdentifiers

// 通过 ImageIO 缩略解码，避免完整展开高像素照片，并重新编码以移除 EXIF/GPS。
enum PhotoPreparation {
    static func jpeg(_ data: Data) throws -> Data {
        guard data.count <= 20 * 1024 * 1024,
              let source = CGImageSourceCreateWithData(data as CFData, [kCGImageSourceShouldCache: false] as CFDictionary),
              let image = CGImageSourceCreateThumbnailAtIndex(source, 0, [
                kCGImageSourceCreateThumbnailFromImageAlways: true,
                kCGImageSourceCreateThumbnailWithTransform: true,
                kCGImageSourceThumbnailMaxPixelSize: 1536
              ] as CFDictionary) else { throw APIClient.APIError.message("无法读取照片或照片超过 20 MB") }
        let output = NSMutableData()
        guard let destination = CGImageDestinationCreateWithData(output, UTType.jpeg.identifier as CFString, 1, nil) else {
            throw APIClient.APIError.message("无法转换照片")
        }
        CGImageDestinationAddImage(destination, image, [kCGImageDestinationLossyCompressionQuality: 0.85] as CFDictionary)
        guard CGImageDestinationFinalize(destination) else { throw APIClient.APIError.message("照片转换失败") }
        return output as Data
    }
}
