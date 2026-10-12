import Foundation
import HealthKit

enum HealthMetric: String, CaseIterable {
    case sleep, steps, heart_rate, body_mass, resting_heart_rate, heart_rate_variability
    case respiratory_rate, oxygen_saturation, active_energy, basal_energy, exercise_minutes
    case walking_running_distance, body_temperature, body_fat_percentage

    var title: String {
        switch self {
        case .sleep: "睡眠"
        case .steps: "步数"
        case .heart_rate: "心率"
        case .body_mass: "体重"
        case .resting_heart_rate: "静息心率"
        case .heart_rate_variability: "心率变异性"
        case .respiratory_rate: "呼吸频率"
        case .oxygen_saturation: "血氧饱和度"
        case .active_energy: "活动能量"
        case .basal_energy: "静息能量"
        case .exercise_minutes: "运动时间"
        case .walking_running_distance: "步行与跑步距离"
        case .body_temperature: "体温"
        case .body_fat_percentage: "体脂率"
        }
    }

    var sampleType: HKSampleType {
        if self == .sleep { return HKObjectType.categoryType(forIdentifier: .sleepAnalysis)! }
        let identifier: HKQuantityTypeIdentifier = switch self {
        case .steps: .stepCount
        case .heart_rate: .heartRate
        case .body_mass: .bodyMass
        case .resting_heart_rate: .restingHeartRate
        case .heart_rate_variability: .heartRateVariabilitySDNN
        case .respiratory_rate: .respiratoryRate
        case .oxygen_saturation: .oxygenSaturation
        case .active_energy: .activeEnergyBurned
        case .basal_energy: .basalEnergyBurned
        case .exercise_minutes: .appleExerciseTime
        case .walking_running_distance: .distanceWalkingRunning
        case .body_temperature: .bodyTemperature
        case .body_fat_percentage: .bodyFatPercentage
        case .sleep: preconditionFailure("睡眠使用分类样本")
        }
        return HKObjectType.quantityType(forIdentifier: identifier)!
    }

    var measurement: (unit: HKUnit, name: String, scale: Double) {
        switch self {
        case .sleep, .steps: (.count(), self == .sleep ? "category" : "count", 1)
        case .heart_rate, .resting_heart_rate, .respiratory_rate: (.count().unitDivided(by: .minute()), "count/min", 1)
        case .body_mass: (.gramUnit(with: .kilo), "kg", 1)
        case .heart_rate_variability: (.secondUnit(with: .milli), "ms", 1)
        case .oxygen_saturation, .body_fat_percentage: (.percent(), "%", 100)
        case .active_energy, .basal_energy: (.kilocalorie(), "kcal", 1)
        case .exercise_minutes: (.minute(), "min", 1)
        case .walking_running_distance: (.meter(), "m", 1)
        case .body_temperature: (.degreeCelsius(), "degC", 1)
        }
    }
}

@MainActor
enum HealthCapture {
    static let maximumSamples = 20_000
    static let chunkSize = 1_000

    static func collect(_ action: ClientActionRequest, allowEmpty: Bool, allowPermissionPrompt: Bool = true,
                        automaticReviewIsEnabled: (@MainActor () -> Bool)? = nil) async throws -> [[String: JSONValue]] {
        let scope = try HealthReadScope(parameters: action.parameters)
        guard HKHealthStore.isHealthDataAvailable() else { throw HealthReadFailure.unavailable }
        try await ClientActionStore.waitForForeground()
        let metrics = scope.types.compactMap(HealthMetric.init(rawValue:))
        let store = HKHealthStore()
        if allowPermissionPrompt {
            do {
                // 系统只询问尚未决定的指标；面板完成不代表获得读取权限。
                try await store.requestAuthorization(toShare: [], read: Set(metrics.map { $0.sampleType as HKObjectType }))
            } catch { throw HealthReadFailure.readable(error) }
        } else {
            try await requirePreviouslyRequestedAuthorization(action, store: store)
        }
        try await ClientActionStore.waitForForeground()
        try checkAutomaticReview(automaticReviewIsEnabled)
        var samples: [JSONValue] = []
        for metric in metrics {
            // 睡眠取重叠片段，避免漏掉午夜前入睡的数据；计量值不按时间比例猜测。
            let options: HKQueryOptions = metric == .sleep ? [] : [.strictStartDate, .strictEndDate]
            let predicate = HKQuery.predicateForSamples(withStart: scope.start, end: scope.end, options: options)
            var anchor: HKQueryAnchor?
            var captured: [UUID: HKSample] = [:]
            while true {
                try await ClientActionStore.waitForForeground()
                try checkAutomaticReview(automaticReviewIsEnabled)
                let page: ([HKSample], [HKDeletedObject], HKQueryAnchor?) = try await withCheckedThrowingContinuation { continuation in
                    store.execute(HKAnchoredObjectQuery(type: metric.sampleType, predicate: predicate, anchor: anchor, limit: chunkSize) { _, rows, deleted, next, error in
                        if let error { continuation.resume(throwing: HealthReadFailure.readable(error)) }
                        else { continuation.resume(returning: (rows ?? [], deleted ?? [], next)) }
                    })
                }
                for item in page.0 { captured[item.uuid] = item }
                for item in page.1 { captured.removeValue(forKey: item.uuid) }
                guard captured.count + samples.count <= maximumSamples else {
                    throw APIClient.APIError.message("本次健康记录超过 20000 条，请缩小时间范围或分指标读取；没有提交截断的数据。")
                }
                anchor = page.2
                if page.0.count + page.1.count < chunkSize { break }
                guard anchor != nil else { throw APIClient.APIError.message("健康数据分页未完成，请重新采集") }
            }
            samples += try captured.values.sorted { $0.uuid.uuidString < $1.uuid.uuidString }.map { try serialize($0, metric: metric, scope: scope) }
        }
        try Task.checkCancellation()
        try checkAutomaticReview(automaticReviewIsEnabled)
        return try chunks(samples: samples, scope: scope, allowEmpty: allowEmpty)
    }

    /// unnecessary 只表示无需再询问权限；不能证明读取获准，空结果仍交给用户处理。
    static func mayReadWithoutPermissionPrompt(_ status: HKAuthorizationRequestStatus) -> Bool {
        status == .unnecessary
    }

    static func requirePreviouslyRequestedAuthorization(_ action: ClientActionRequest, store: HKHealthStore = HKHealthStore()) async throws {
        guard HKHealthStore.isHealthDataAvailable() else { throw HealthReadFailure.unavailable }
        let scope = try HealthReadScope(parameters: action.parameters)
        let types = Set(scope.types.compactMap(HealthMetric.init(rawValue:)).map { $0.sampleType as HKObjectType })
        let status: HKAuthorizationRequestStatus = try await withCheckedThrowingContinuation { continuation in
            store.getRequestStatusForAuthorization(toShare: [], read: types) { status, error in
                if let error { continuation.resume(throwing: HealthReadFailure.readable(error)) }
                else { continuation.resume(returning: status) }
            }
        }
        guard mayReadWithoutPermissionPrompt(status) else { throw HealthReadFailure.authorizationRequired }
    }

    private static func checkAutomaticReview(_ isEnabled: (@MainActor () -> Bool)?) throws {
        try Task.checkCancellation()
        if let isEnabled, !isEnabled() { throw CancellationError() }
    }

    static func serialize(_ sample: HKSample, metric: HealthMetric, scope: HealthReadScope) throws -> JSONValue {
        let measurement = metric.measurement
        let value: Double
        if let category = sample as? HKCategorySample, metric == .sleep { value = Double(category.value) }
        else if let quantity = sample as? HKQuantitySample, metric != .sleep {
            value = quantity.quantity.doubleValue(for: measurement.unit) * measurement.scale
        } else { throw APIClient.APIError.message("健康样本类型不符合请求，未忽略该记录") }
        let start = metric == .sleep ? max(scope.start, sample.startDate) : sample.startDate
        let end = metric == .sleep ? min(scope.end, sample.endDate) : sample.endDate
        var result: [String: JSONValue] = ["type": .string(metric.rawValue), "sample_id": .string(sample.uuid.uuidString.lowercased()),
            "start_at": .string(start.ISO8601Format()), "end_at": .string(end.ISO8601Format()), "value": .number(value), "unit": .string(measurement.name)]
        if metric == .sleep {
            result["original_start_at"] = .string(sample.startDate.ISO8601Format())
            result["original_end_at"] = .string(sample.endDate.ISO8601Format())
        }
        let source = sample.sourceRevision
        for (key, text) in ["source_name": source.source.name, "source_bundle": source.source.bundleIdentifier,
                             "source_version": source.version ?? "", "source_product": source.productType ?? "",
                             "timezone": sample.metadata?[HKMetadataKeyTimeZone] as? String ?? ""] {
            result[key] = .string(String(text.unicodeScalars.filter { $0.value >= 32 }.prefix(200).map(String.init).joined()))
        }
        return .object(result)
    }

    static func chunks(samples: [JSONValue], scope: HealthReadScope, allowEmpty: Bool, captureID: String = UUID().uuidString.lowercased()) throws -> [[String: JSONValue]] {
        guard samples.count <= maximumSamples else { throw APIClient.APIError.message("健康样本超出单次范围，请分批读取") }
        guard !samples.isEmpty || allowEmpty else { throw HealthReadFailure.noReadableData }
        let count = max(1, (samples.count + chunkSize - 1) / chunkSize)
        return try (0..<count).map { index in
            let lower = index * chunkSize, upper = min(samples.count, lower + chunkSize)
            var payload = try scope.payload(samples: Array(samples[lower..<upper]), allowEmpty: allowEmpty)
            payload["schema_version"] = .number(2)
            payload["query_complete"] = .bool(true)
            payload["truncated"] = .bool(false)
            payload["capture_id"] = .string(captureID)
            payload["chunk_index"] = .number(Double(index))
            payload["chunk_count"] = .number(Double(count))
            return payload
        }
    }
}
