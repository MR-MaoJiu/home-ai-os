import Foundation
import EventKit

struct ReminderSchedule: Codable, Equatable, Sendable {
    var due: Date?
    var notify: Bool
    static let empty = ReminderSchedule(due: nil, notify: false)

    static func remote(_ payload: [String: JSONValue]) throws -> Self {
        var due: Date?
        if let value = payload["due_at"], case .null = value { due = nil }
        else if let value = payload["due_at"] {
            guard case .string(let raw) = value else { throw APIClient.APIError.message("到期时间格式无效") }
            let formatter = ISO8601DateFormatter()
            due = formatter.date(from: raw)
            if due == nil { formatter.formatOptions.insert(.withFractionalSeconds); due = formatter.date(from: raw) }
            guard due != nil else { throw APIClient.APIError.message("到期时间缺少有效时区") }
        }
        let notify: Bool
        if let value = payload["notify_at_due"] {
            guard case .bool(let flag) = value else { throw APIClient.APIError.message("到期通知选项无效") }
            notify = flag
        } else { notify = false }
        guard !notify || due != nil else { throw APIClient.APIError.message("到期通知缺少时间") }
        return Self(due: due, notify: notify)
    }

    @MainActor static func local(_ reminder: EKReminder) throws -> Self {
        var due: Date?
        if let components = reminder.dueDateComponents {
            guard components.timeZone != nil, components.hour != nil, components.minute != nil else {
                throw APIClient.APIError.message("浮动或全天提醒时间需手工核对")
            }
            var calendar = Calendar(identifier: .gregorian)
            calendar.timeZone = components.timeZone!
            due = calendar.date(from: components)
            guard due != nil else { throw APIClient.APIError.message("系统提醒日期无效") }
            if let start = reminder.startDateComponents, calendar.date(from: start) != due {
                throw APIClient.APIError.message("独立开始时间不能自动覆盖")
            }
        } else if reminder.startDateComponents != nil { throw APIClient.APIError.message("开始时间需手工核对") }
        let alarms = reminder.alarms ?? []
        guard alarms.count <= 1 else { throw APIClient.APIError.message("多重提醒需手工核对") }
        if let alarm = alarms.first {
            guard let due, alarm.structuredLocation == nil,
                  alarm.absoluteDate == due || (alarm.absoluteDate == nil && alarm.relativeOffset == 0) else {
                throw APIClient.APIError.message("当前只同步到期时通知")
            }
        }
        return Self(due: due, notify: !alarms.isEmpty)
    }

    @MainActor func apply(to reminder: EKReminder) {
        if let due {
            var calendar = Calendar(identifier: .gregorian)
            calendar.timeZone = TimeZone(secondsFromGMT: 0)!
            var components = calendar.dateComponents([.year, .month, .day, .hour, .minute, .second], from: due)
            components.calendar = calendar
            components.timeZone = calendar.timeZone
            // EventKit 在 iOS 上要求到期时间同时带有开始时间。
            reminder.startDateComponents = components
            reminder.dueDateComponents = components
            reminder.alarms = notify ? [EKAlarm(absoluteDate: due)] : []
        } else {
            reminder.startDateComponents = nil
            reminder.dueDateComponents = nil
            reminder.alarms = []
        }
    }
}
