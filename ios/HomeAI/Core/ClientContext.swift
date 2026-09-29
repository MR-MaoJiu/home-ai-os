import Foundation
import UIKit
import CoreLocation
import Network
import Observation

struct ClientLocation: Codable, Sendable {
    let latitude: Double
    let longitude: Double
    let accuracy_meters: Double
    let observed_at: String
    let precision: String
}

struct ClientContextSnapshot: Codable, Sendable {
    struct Availability: Codable, Sendable { let battery: String; let network: String; let location: String }
    let schema_version: String
    let app_version: String?
    let sampled_at: String
    let timezone: String
    let locale: String
    let battery_level: Double?
    let battery_state: String
    let low_power_mode: Bool
    let network_type: String
    let location: ClientLocation?
    let availability: Availability

    func json() throws -> Any { try JSONSerialization.jsonObject(with: JSONEncoder().encode(self)) }
}

/// 发送消息只读取已知快照，不等待传感器、网络或系统授权。
@MainActor @Observable
final class ClientContextSampler: NSObject, @preconcurrency CLLocationManagerDelegate {
    static let shared = ClientContextSampler()
    static let locationPreference = "chatCoarseLocationEnabled"
    static var locationHintsEnabled: Bool { UserDefaults.standard.object(forKey: locationPreference) == nil || UserDefaults.standard.bool(forKey: locationPreference) }
    private let manager = CLLocationManager()
    private let monitor = NWPathMonitor()
    private var networkType = "unknown"
    private var lastLocation: CLLocation?
    private var continuation: CheckedContinuation<ClientLocation, Error>?
    private var timeout: Task<Void, Never>?
    private var requestedPrecision = "coarse"
    var locationStatus = "未提供位置"

    override init() {
        super.init()
        UIDevice.current.isBatteryMonitoringEnabled = true
        manager.delegate = self
        manager.desiredAccuracy = kCLLocationAccuracyKilometer
        monitor.pathUpdateHandler = { [weak self] path in
            let type: String
            if path.status != .satisfied { type = "offline" }
            else if path.usesInterfaceType(.wifi) { type = "wifi" }
            else if path.usesInterfaceType(.cellular) { type = "cellular" }
            else if path.usesInterfaceType(.wiredEthernet) { type = "wired" }
            else { type = "other" }
            Task { @MainActor in self?.networkType = type }
        }
        monitor.start(queue: DispatchQueue(label: "homeai.context.network"))
    }

    static func coarse(_ location: CLLocation) -> ClientLocation {
        ClientLocation(latitude: (location.coordinate.latitude * 100).rounded() / 100,
                       longitude: (location.coordinate.longitude * 100).rounded() / 100,
                       accuracy_meters: max(0, location.horizontalAccuracy) + 1500,
                       observed_at: location.timestamp.ISO8601Format(), precision: "coarse")
    }

    func snapshot(now: Date = Date()) -> ClientContextSnapshot {
        let battery = UIDevice.current.batteryLevel
        let states: [UIDevice.BatteryState: String] = [.unknown: "unknown", .unplugged: "unplugged", .charging: "charging", .full: "full"]
        let authorized = [.authorizedWhenInUse, .authorizedAlways].contains(manager.authorizationStatus)
        var location: ClientLocation?
        let availability: String
        if !Self.locationHintsEnabled || !authorized { availability = "not_authorized" }
        else if let value = lastLocation ?? manager.location, value.horizontalAccuracy >= 0 {
            let age = now.timeIntervalSince(value.timestamp)
            if age >= 0 && age <= 900 { location = Self.coarse(value); availability = "available" }
            else { availability = "stale" }
        } else { availability = "unavailable" }
        return ClientContextSnapshot(schema_version: "1.0", app_version: Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String, sampled_at: now.ISO8601Format(), timezone: TimeZone.current.identifier,
            locale: Locale.current.identifier, battery_level: battery >= 0 ? Double(battery) : nil,
            battery_state: states[UIDevice.current.batteryState] ?? "unknown", low_power_mode: ProcessInfo.processInfo.isLowPowerModeEnabled,
            network_type: networkType, location: location,
            availability: .init(battery: battery >= 0 ? "available" : "unavailable", network: networkType == "unknown" ? "unavailable" : "available", location: availability))
    }

    func refreshAllowedLocation() {
        guard Self.locationHintsEnabled, UIApplication.shared.applicationState == .active,
              [.authorizedWhenInUse, .authorizedAlways].contains(manager.authorizationStatus) else { return }
        manager.desiredAccuracy = kCLLocationAccuracyKilometer
        manager.requestLocation()
    }

    func setLocationHints(_ enabled: Bool) {
        UserDefaults.standard.set(enabled, forKey: Self.locationPreference)
        if enabled {
            if manager.authorizationStatus == .notDetermined { manager.requestWhenInUseAuthorization() }
            else { refreshAllowedLocation() }
        } else { lastLocation = nil; locationStatus = "不向对话提供位置" }
    }

    func captureLocation(precision: String = "coarse") async throws -> ClientLocation {
        guard UIApplication.shared.applicationState == .active else { throw APIClient.APIError.message("请保持 App 在前台以提供位置") }
        guard continuation == nil else { throw APIClient.APIError.message("已有位置请求正在进行") }
        requestedPrecision = precision == "precise" ? "precise" : "coarse"
        manager.desiredAccuracy = requestedPrecision == "precise" ? kCLLocationAccuracyNearestTenMeters : kCLLocationAccuracyKilometer
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                self.continuation = continuation
                timeout = Task { try? await Task.sleep(for: .seconds(30)); if !Task.isCancelled { finish(.failure(APIClient.APIError.message("位置读取超时，可稍后重试"))) } }
                if manager.authorizationStatus == .notDetermined { manager.requestWhenInUseAuthorization() }
                else { locationManagerDidChangeAuthorization(manager) }
            }
        } onCancel: { Task { @MainActor in self.finish(.failure(CancellationError())) } }
    }

    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        if [.denied, .restricted].contains(manager.authorizationStatus) {
            lastLocation = nil; locationStatus = "位置未授权"; finish(.failure(APIClient.APIError.message("位置未授权，可在系统设置中调整")))
        } else if [.authorizedWhenInUse, .authorizedAlways].contains(manager.authorizationStatus) {
            if continuation != nil { manager.requestLocation() } else { refreshAllowedLocation() }
        }
    }
    func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard let value = locations.last, value.horizontalAccuracy >= 0 else { return }
        lastLocation = value; locationStatus = "已获得位置样本，聊天仅使用大致位置"
        let result = requestedPrecision == "precise" && manager.accuracyAuthorization == .fullAccuracy ? ClientLocation(latitude: value.coordinate.latitude, longitude: value.coordinate.longitude,
            accuracy_meters: value.horizontalAccuracy, observed_at: value.timestamp.ISO8601Format(), precision: "precise") : Self.coarse(value)
        finish(.success(result))
    }
    func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) { locationStatus = "位置暂不可用"; finish(.failure(error)) }
    private func finish(_ result: Result<ClientLocation, Error>) {
        timeout?.cancel(); timeout = nil
        let waiting = continuation; continuation = nil
        waiting?.resume(with: result)
    }
}
