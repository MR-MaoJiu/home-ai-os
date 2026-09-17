import CoreLocation
import Foundation

@MainActor
// CLLocationManager 在主线程创建，委托回调使用同一运行循环。
final class LocationCapture: NSObject, @preconcurrency CLLocationManagerDelegate {
    private let manager = CLLocationManager()
    private var continuation: CheckedContinuation<CLLocationCoordinate2D, Error>?
    private var timeout: Task<Void, Never>?

    override init() { super.init(); manager.delegate = self }

    func once() async throws -> CLLocationCoordinate2D {
        guard continuation == nil else { throw APIClient.APIError.message("正在获取位置") }
        return try await withCheckedThrowingContinuation { continuation in
            self.continuation = continuation
            timeout = Task {
                try? await Task.sleep(for: .seconds(30))
                if !Task.isCancelled { self.finish(.failure(APIClient.APIError.message("位置请求超时"))) }
            }
            manager.requestWhenInUseAuthorization()
            locationManagerDidChangeAuthorization(manager)
        }
    }
    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        guard continuation != nil else { return }
        switch manager.authorizationStatus {
        case .authorizedAlways, .authorizedWhenInUse: manager.requestLocation()
        case .denied, .restricted: finish(.failure(APIClient.APIError.message("位置未授权")))
        default: break
        }
    }
    func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        if let coordinate = locations.last?.coordinate { finish(.success(coordinate)) }
    }
    func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) { finish(.failure(error)) }
    private func finish(_ result: Result<CLLocationCoordinate2D, Error>) {
        timeout?.cancel(); timeout = nil
        let pending = continuation; continuation = nil
        pending?.resume(with: result)
    }
}
