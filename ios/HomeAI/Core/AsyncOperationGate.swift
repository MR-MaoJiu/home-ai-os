import Foundation

/// 取消等待不占用执行权；持有者无论成功或失败都必须释放。
actor AsyncOperationGate {
    private var running = false
    private var waiters: [(UUID, CheckedContinuation<Void, Error>)] = []
    var waitingCount: Int { waiters.count }

    func acquire() async throws {
        let identifier = UUID()
        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
                if Task.isCancelled { continuation.resume(throwing: CancellationError()) }
                else if !running { running = true; continuation.resume() }
                else { waiters.append((identifier, continuation)) }
            }
        } onCancel: {
            Task { await self.cancelWaiter(identifier) }
        }
    }

    private func cancelWaiter(_ identifier: UUID) {
        guard let index = waiters.firstIndex(where: { $0.0 == identifier }) else { return }
        waiters.remove(at: index).1.resume(throwing: CancellationError())
    }

    func release() {
        if waiters.isEmpty { running = false } else { waiters.removeFirst().1.resume() }
    }

    func withPermit<T: Sendable>(_ operation: @Sendable () async throws -> T) async throws -> T {
        try await acquire()
        do {
            try Task.checkCancellation()
            let result = try await operation()
            release()
            return result
        } catch {
            release()
            throw error
        }
    }
}
