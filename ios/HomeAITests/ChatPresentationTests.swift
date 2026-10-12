import XCTest
import Foundation
import SwiftUI
import UIKit
@testable import HomeAI

/// 只验证展示层与公开文档链接，不加载图片、不访问家庭服务或执行卡片操作。
final class ChatPresentationTests: XCTestCase {
    @MainActor func testScrollGeometryCoalescesFramesAndCancelsAfterLeaving() async throws {
        let coordinator = ChatScrollGeometryCoordinator()
        var updates: [Int] = []
        for index in 0..<20 { coordinator.schedule { updates.append(index) } }
        XCTAssertTrue(updates.isEmpty, "布局回调不能同步修改展示状态")
        try await Task.sleep(for: .milliseconds(150))
        XCTAssertEqual(updates, [19])
        coordinator.schedule { updates.append(20) }
        coordinator.cancel()
        try await Task.sleep(for: .milliseconds(100))
        XCTAssertEqual(updates, [19], "离开页面后不再触发延迟滚动")
    }

    func testLateSendReceiptCannotReplaceCompletedAnswerButNewRetryAndRevocationCan() throws {
        func response(status: String, answer: String, revision: Int) throws -> StoredChatTurn {
            let wire: [String: Any] = ["id": "turn", "sequence": 1, "client_key": "original-send", "user_text": "原问题",
                "task_id": "original-task", "status": status, "assistant_text": answer, "approvals": [],
                "web_sources": [], "sources": [], "display_revision": revision]
            return try JSONDecoder().decode(StoredChatTurn.self, from: JSONSerialization.data(withJSONObject: wire))
        }
        let receipt = try response(status: "RECEIVED", answer: "", revision: 20)
        let final = try response(status: "SUCCEEDED", answer: "对应原问题的结果", revision: 23)
        let afterLateReceipt = ChatSnapshot.merge([final], updates: [receipt])
        XCTAssertEqual(afterLateReceipt.first?.assistant_text, "对应原问题的结果")
        XCTAssertEqual(afterLateReceipt.first?.status, "SUCCEEDED")
        XCTAssertEqual(afterLateReceipt.first?.task_id, "original-task")
        let retry = try response(status: "EXECUTING", answer: "", revision: 24)
        let afterRetry = ChatSnapshot.merge(afterLateReceipt, updates: [retry])
        XCTAssertEqual(afterRetry.first?.status, "EXECUTING")
        XCTAssertEqual(afterRetry.first?.assistant_text, "")
        let redacted = try response(status: "SUCCEEDED", answer: "关联资料已撤权，旧回答已隐藏。", revision: 25)
        let afterRevocation = ChatSnapshot.merge(afterRetry, updates: [redacted, final])
        XCTAssertEqual(afterRevocation.first?.assistant_text, redacted.assistant_text)
        XCTAssertEqual(afterRevocation.first?.display_revision, 25)
        let restored = try JSONDecoder().decode(StoredChatTurn.self, from: JSONEncoder().encode(redacted.cached()))
        XCTAssertEqual(restored.display_revision, 25)
    }

    func testConcurrentTaskAnswersStayWithOriginalQuestionAndCompletionOrder() {
        func turn(_ id: String, sequence: Int, created: Double, completed: Double) -> StoredChatTurn {
            var value = StoredChatTurn(id: id, sequence: sequence, client_key: id, user_text: "问题 " + id,
                task_id: "task-" + id, status: "SUCCEEDED", assistant_text: "答案 " + id,
                error: nil, approvals: [], web_sources: [], sources: [])
            value.created_at = created; value.completed_at = completed
            return value
        }
        let earlier = turn("earlier", sequence: 1, created: 100, completed: 300)
        let later = turn("later", sequence: 2, created: 200, completed: 210)
        let items = ChatTimelineItem.ordered([earlier, later])
        XCTAssertEqual(items.map(\.id), ["earlier:request", "later:request", "later:answer", "earlier:answer"])
        XCTAssertEqual(items.last?.turn.task_id, "task-earlier")
        XCTAssertEqual(items.last?.turn.user_text, "问题 earlier")
        let immediate = turn("same-time", sequence: 3, created: 400, completed: 400)
        XCTAssertEqual(ChatTimelineItem.ordered([immediate]).map(\.answer), [false, true])
    }

    func testStreamUpdatesKeepBubbleIdentityButAdvanceDisplayRevision() {
        let first = StoredChatTurn(id: "stream", sequence: 1, client_key: "message", user_text: "问题", task_id: "job",
            status: "EXECUTING", assistant_text: "第一段", error: nil, approvals: [], web_sources: [], sources: [])
        let second = StoredChatTurn(id: "stream", sequence: 1, client_key: "message", user_text: "问题", task_id: "job",
            status: "EXECUTING", assistant_text: "第一段，第二段", error: nil, approvals: [], web_sources: [], sources: [])
        let before = ChatTimelineItem.ordered([first]).last
        let after = ChatTimelineItem.ordered(ChatSnapshot.merge([first], updates: [second])).last
        XCTAssertEqual(before?.id, after?.id)
        XCTAssertNotEqual(before?.displayRevision, after?.displayRevision)
        XCTAssertEqual(after?.turn.assistant_text, "第一段，第二段")
    }

    func testMemoryRefreshCoalescesEventsAndOldCompletionCannotClearNewIdentity() throws {
        var gate = MemoryRefreshGate()
        let oldGeneration = gate.generation
        let first = try XCTUnwrap(gate.begin())
        XCTAssertNil(gate.begin())
        XCTAssertNil(gate.begin())
        XCTAssertTrue(gate.finish(first))
        XCTAssertFalse(gate.loading)
        let oldRun = try XCTUnwrap(gate.begin())
        gate.reset()
        let newRun = try XCTUnwrap(gate.begin())
        XCTAssertFalse(gate.accepts(oldRun, generation: oldGeneration))
        XCTAssertFalse(gate.finish(oldRun))
        XCTAssertTrue(gate.loading)
        XCTAssertTrue(gate.accepts(newRun, generation: gate.generation))
        XCTAssertFalse(gate.finish(newRun))
        XCTAssertFalse(gate.loading)
    }

    func testTaskMetadataSurvivesCacheAndPlainChatHasNoTaskPanel() throws {
        var turn = StoredChatTurn(id: "turn", sequence: 1, client_key: "message", user_text: "你好", task_id: "job", status: "SUCCEEDED", assistant_text: "你好", error: nil, approvals: [], web_sources: [], sources: [])
        turn.interaction_kind = "chat"
        XCTAssertFalse(turn.hasTaskDetails)
        turn.interaction_kind = "task"
        turn.acknowledgement = "我去查一下，查好告诉你。"
        turn.created_at = 100
        turn.completed_at = 200
        let restored = try JSONDecoder().decode(StoredChatTurn.self, from: JSONEncoder().encode(turn.cached()))
        XCTAssertTrue(restored.hasTaskDetails)
        XCTAssertEqual(restored.completed_at, 200)
        XCTAssertEqual(restored.acknowledgement, turn.acknowledgement)
    }

    @MainActor
    func testAndroidAssistanceCannotSubmitPasswordOrFilesAndReceiptEndsTheCard() async {
        let request = ChatPresentationFixture.action(kind: "android.assist", parameters: ["workspace_id": .string("public-workspace"), "reason": .string("当前页面需要本人处理")])
        XCTAssertFalse(request.permitsAutomaticCoarseLocation())
        for (records, text) in [([String](), "不得发送的测试输入" as String?), (["test-record"], nil)] {
            do {
                _ = try await ClientActionStore.respond(request, recordIDs: records, text: text, api: APIClient())
                XCTFail("设备协助不应接受文字或资料")
            } catch {
                XCTAssertFalse(error.localizedDescription.contains("不得发送的测试输入"))
            }
        }
        var display = ClientActionPresentation(request: request)
        XCTAssertTrue(display.acknowledge(ClientActionReceipt(id: request.id, status: "RESPONDED")))
        XCTAssertTrue(display.current.finished)
        XCTAssertFalse(display.current.can_respond)
        XCTAssertTrue(display.current.summaryLabel.contains("继续"))
        XCTAssertFalse(display.receive(request))
        let canceled = request.applyingReceipt(ClientActionReceipt(id: request.id, status: "CANCELED"))
        XCTAssertEqual(canceled.summaryLabel, "已取消任务")
    }

    private let python = "https://docs.python.org/3/library/asyncio.html"
    private let fastAPI = "https://fastapi.tiangolo.com/"

    func testDataReferencesSendOnlyIdentityAndVersionAndRestoreDraft() throws {
        let reference = ChatDataReference(record_id: "existing-record", version: 3, title: "仅展示的资料名称", kind: "health.steps")
        XCTAssertEqual(Set(reference.wireValue.keys), ["record_id", "version"])
        XCTAssertEqual(reference.wireValue["record_id"] as? String, "existing-record")
        XCTAssertEqual(reference.wireValue["version"] as? Int, 3)
        let pending = PendingChatSend(conversationID: "conversation", clientKey: "client-key", content: "请分析", timezone: "Asia/Shanghai", references: [reference])
        let restored = try JSONDecoder().decode(PendingChatSend.self, from: JSONEncoder().encode(pending))
        XCTAssertEqual(restored.references, [reference])
        let old = Data(#"{"conversationID":"conversation","clientKey":"client-key","content":"旧草稿","timezone":"Asia/Shanghai"}"#.utf8)
        XCTAssertNil(try JSONDecoder().decode(PendingChatSend.self, from: old).references)
    }

    func testDataReferenceChoicesExcludeMemoriesSecretsAndMissingVersion() throws {
        func entry(kind: String = "document.file", sensitivity: String = "PRIVATE", version: Int? = 1) throws -> DataEntry {
            var value: [String: Any] = ["id": "record", "kind": kind, "sensitivity": sensitivity, "payload": ["name": "资料"]]
            if let version { value["version"] = version }
            return try JSONDecoder().decode(DataEntry.self, from: JSONSerialization.data(withJSONObject: value))
        }
        XCTAssertTrue(ChatDataReferenceRules.selectable(try entry()))
        XCTAssertTrue(ChatDataReferenceRules.selectable(try entry(kind: "health.steps", sensitivity: "SENSITIVE")))
        XCTAssertFalse(ChatDataReferenceRules.selectable(try entry(kind: "memory.fact")))
        XCTAssertFalse(ChatDataReferenceRules.selectable(try entry(sensitivity: "SECRET")))
        XCTAssertFalse(ChatDataReferenceRules.selectable(try entry(version: nil)))
        XCTAssertFalse(ChatDataReferenceRules.selectable(try entry(version: 0)))
    }

    func testSourcesDeduplicateByURLAndPreserveFirstTitleAndOrder() {
        let values = ChatSourcePresentation.items([
            ChatWebSource(title: "  Python asyncio 官方文档\n", url: python),
            ChatWebSource(title: "FastAPI 官方文档", url: fastAPI),
            ChatWebSource(title: "不应覆盖首个标题", url: python)
        ])
        XCTAssertEqual(values.map(\.id), [python, fastAPI])
        XCTAssertEqual(values.map(\.title), ["Python asyncio 官方文档", "FastAPI 官方文档"])
        XCTAssertEqual(values.map(\.domain), ["docs.python.org", "fastapi.tiangolo.com"])
        XCTAssertEqual(values.map(\.url.absoluteString), [python, fastAPI])
    }

    func testUnsafeSourcesAreExcludedWithoutRemovingLaterSafeEntries() {
        let unsafe = [
            "javascript:alert(1)", "data:text/html,<script></script>",
            "file:///private/example.txt", "ftp://example.org/file", "/relative/path",
            "https:relative", "https://user:password@example.org/", "https://user@example.org/", ""
        ]
        let input = unsafe.map { ChatWebSource(title: "不应成为链接", url: $0) }
            + [ChatWebSource(title: "官方文档", url: python)]
        let values = ChatSourcePresentation.items(input)
        XCTAssertEqual(values.count, 1)
        XCTAssertEqual(values.first?.url.absoluteString, python)
        XCTAssertTrue(ChatSourcePresentation.items(unsafe.map { ChatWebSource(title: "无效", url: $0) }).isEmpty)
    }

    func testBlankTitleFallsBackToDomainAndVisibleTitleRemainsLiteral() {
        let title = "**文档标题** ![不可自动加载](https://example.org/image.png)"
        let values = ChatSourcePresentation.items([
            ChatWebSource(title: " \n\t ", url: python),
            ChatWebSource(title: title, url: fastAPI)
        ])
        XCTAssertEqual(values.first?.title, "docs.python.org")
        XCTAssertEqual(values.last?.title, title)
        XCTAssertEqual(values.last?.url.absoluteString, fastAPI)
    }

    func testDistinctAnchorsRemainSeparateAndDomainSummaryIsBounded() {
        let urls = [python, python + "#module-asyncio", fastAPI,
                    "https://developer.apple.com/documentation/swiftui",
                    "https://www.postgresql.org/docs/"]
        let values = ChatSourcePresentation.items(urls.map { ChatWebSource(title: "公开文档", url: $0) })
        XCTAssertEqual(values.count, urls.count)
        XCTAssertEqual(ChatSourcePresentation.summary(values), "docs.python.org · fastapi.tiangolo.com · developer.apple.com")
        XCTAssertEqual(ChatSourcePresentation.summary([]), "")
        XCTAssertTrue(ChatSourcePresentation.items([]).isEmpty)
    }

    func testApprovalLabelsDoNotHideUnknownCapabilityOrFieldValues() {
        XCTAssertEqual(ChatApprovalPresentation.title("mail.send@v1"), "确认发送邮件")
        XCTAssertEqual(ChatApprovalPresentation.title("home.execute@v1"), "确认家居操作")
        XCTAssertEqual(ChatApprovalPresentation.title("shell.run@v1"), "确认执行 Shell")
        XCTAssertEqual(ChatApprovalPresentation.title("host.execute@v1"), "确认宿主管理")
        XCTAssertEqual(ChatApprovalPresentation.title("host.inspect@v1"), "允许读取服务器信息")
        XCTAssertEqual(ChatApprovalPresentation.title("future.operation@v1"), "确认本次操作")
        XCTAssertEqual(ChatApprovalPresentation.field("to"), "收件人")
        XCTAssertEqual(ChatApprovalPresentation.field("subject"), "主题")
        XCTAssertEqual(ChatApprovalPresentation.field("text"), "正文")
        XCTAssertEqual(ChatApprovalPresentation.field("script"), "执行脚本")
        XCTAssertEqual(ChatApprovalPresentation.field("language"), "执行语言")
        XCTAssertEqual(ChatApprovalPresentation.field("inputs"), "输入文件")
        XCTAssertEqual(ChatApprovalPresentation.field("outputs"), "返回文件")
        XCTAssertEqual(ChatApprovalPresentation.field("timeout_seconds"), "最长执行秒数")
        XCTAssertEqual(ChatApprovalPresentation.field("action_id"), "管理动作")
        XCTAssertEqual(ChatApprovalPresentation.field("execution"), "执行配置")
        XCTAssertEqual(ChatApprovalPresentation.field("impact_summary"), "AI 预计影响（未经独立验证）")
        XCTAssertEqual(ChatApprovalPresentation.field("future_field"), "future_field")
    }

    func testAndroidApprovalDisplaysCurrentActionWithoutControlTokensOrEmptyFields() {
        let arguments: [String: JSONValue] = ["purpose": .string("输入本次搜索词"), "impact_summary": .string("将文字写入当前输入框"),
            "input": .object(["action": .string("text"), "text": .string("公开测试内容"), "epoch": .number(2),
                              "sequence": .number(3), "frame_id": .string("internal-frame"), "focus_token": .string("internal-focus"),
                              "x": .null, "package": .null, "future_option": .string("保留未知动作参数")])]
        let approval = ApprovalEntry(id: "approval", task_id: "task", capability: "android.action@v1", arguments: arguments)
        let fields = ChatApprovalPresentation.fields(approval)
        XCTAssertTrue(fields.contains { $0.id == "input.text" && $0.value == "公开测试内容" })
        XCTAssertTrue(fields.contains { $0.id == "impact_summary" && $0.value == "将文字写入当前输入框" })
        XCTAssertTrue(fields.contains { $0.id == "input.future_option" })
        XCTAssertFalse(fields.contains { ["input.epoch", "input.sequence", "input.frame_id", "input.focus_token", "input.x", "input.package"].contains($0.id) })
        XCTAssertEqual(approval.arguments, arguments)
        let future = ApprovalEntry(id: "future", task_id: "task", capability: "future.action@v1", arguments: ["custom": .string("完整保留")])
        XCTAssertEqual(ChatApprovalPresentation.fields(future).first?.value, "完整保留")
    }

    func testShellApprovalPreservesFullCommandAndSeparatesUnverifiedModelClaim() throws {
        let command = "printf updated > /workspace/output/result.txt\nrm /workspace/output/temporary.txt"
        let values = try approvalArguments(command: command)
        let entry = ApprovalEntry(id: "approval", task_id: "task", capability: "shell.run@v1", arguments: values)
        let details = try XCTUnwrap(ChatApprovalPresentation.executionDetails(entry))
        XCTAssertEqual(details.command, command)
        XCTAssertEqual(details.proposalImpact, "AI 声称无任何修改")
        XCTAssertEqual(details.fileChanges, ["可修改和删除工作区文件"])
        XCTAssertEqual(details.summary, "成果和执行报告会保存为数据")
        XCTAssertEqual(details.effectsSourceLabel, "服务端确定的处理方式")
        XCTAssertEqual(details.resourceScope, ["返回：/workspace/output/result.txt"])
        XCTAssertFalse(details.uncertainties.isEmpty)
    }

    func testApprovalCopyKeepsActualCommandAndCustomImpactWithoutListingUnrelatedEffects() throws {
        var arguments = try approvalArguments(command: "printf current-status", source: "local_administrator")
        guard case .object(var context) = arguments["approval_context"], case .object(var effects) = context["effects"] else { return XCTFail("缺少测试上下文") }
        effects["summary"] = .string("本机管理员尚未登记完整影响；脚本可能新增、修改或删除文件，也可能改变服务和软件。")
        effects["file_changes"] = .array([])
        effects["service_changes"] = .array([.string("无服务变更")])
        let actualImpact = "删除本次指定的临时报告后无法恢复"
        effects["uncertainties"] = .array([.string("影响说明由本机管理员登记，未对脚本进行完整静态证明；请结合完整指令确认。"), .string(actualImpact)])
        context["effects"] = .object(effects)
        arguments["approval_context"] = .object(context)
        let original = arguments
        let details = try XCTUnwrap(ChatExecutionApprovalDetails(arguments))
        XCTAssertEqual(details.command, "printf current-status")
        XCTAssertTrue(details.fileChanges.isEmpty)
        XCTAssertTrue(details.serviceChanges.isEmpty)
        XCTAssertEqual(details.uncertainties, [actualImpact])
        XCTAssertFalse(details.summary.contains("删除文件"))
        XCTAssertFalse(details.summary.contains("服务和软件"))
        XCTAssertEqual(arguments, original)
    }

    func testSandboxApprovalMentionsOutputOnlyWhenThisRequestDeclaresOutput() throws {
        var arguments = try approvalArguments(command: "printf result")
        guard case .object(var context) = arguments["approval_context"], case .object(var effects) = context["effects"] else { return XCTFail("缺少测试上下文") }
        effects["summary"] = .string("运行脚本并保存私有执行报告；声明的成果文件会作为当前成员的数据附件入库。")
        context["effects"] = .object(effects)
        arguments["approval_context"] = .object(context)
        arguments["outputs"] = .array([])
        XCTAssertFalse(try XCTUnwrap(ChatExecutionApprovalDetails(arguments)).summary.contains("成果文件"))
        arguments["outputs"] = .array([.object(["path": .string("result.txt")])])
        XCTAssertTrue(try XCTUnwrap(ChatExecutionApprovalDetails(arguments)).summary.contains("成果文件"))
    }

    func testHostAndInspectApprovalUseDeclaredSourceWithoutLosingPrivacyWarning() throws {
        for capability in ["host.execute@v1", "host.inspect@v1"] {
            let arguments = try approvalArguments(command: "/usr/sbin/screencapture -x capture.png", source: "local_administrator")
            let details = try XCTUnwrap(ChatApprovalPresentation.executionDetails(
                ApprovalEntry(id: "approval", task_id: "task", capability: capability, arguments: arguments)))
            XCTAssertEqual(details.effectsSourceLabel, "本机管理员登记的影响")
            XCTAssertEqual(details.command, "/usr/sbin/screencapture -x capture.png")
        }
    }

    func testIncompleteOrUnknownApprovalContextFallsBackToOriginalArguments() throws {
        var arguments = try approvalArguments(command: "printf example")
        guard case .object(var context) = arguments["approval_context"] else { return XCTFail("缺少测试上下文") }
        context["version"] = .number(2)
        arguments["approval_context"] = .object(context)
        XCTAssertNil(ChatExecutionApprovalDetails(arguments))
        context["version"] = .number(1)
        context["constraints"] = .array([.number(1)])
        arguments["approval_context"] = .object(context)
        XCTAssertNil(ChatExecutionApprovalDetails(arguments))
        let valid = try approvalArguments(command: "printf example")
        XCTAssertNil(ChatApprovalPresentation.executionDetails(ApprovalEntry(id: "approval", task_id: "task",
            capability: "future.operation@v1", arguments: valid)))
    }

    private func approvalArguments(command: String, source: String = "sandbox_policy") throws -> [String: JSONValue] {
        let raw: [String: Any] = ["approval_context": [
            "version": 1, "source": source, "command": command,
            "execution_location": "隔离工作区", "execution_identity": "UID 65532",
            "constraints": ["关闭网络"], "resource_scope": ["返回：/workspace/output/result.txt"],
            "effects": ["summary": "成果和执行报告会保存为数据", "file_changes": ["可修改和删除工作区文件"],
                        "service_changes": [], "uncertainties": ["影响无法完整预先证明"]],
            "proposal_impact": "AI 声称无任何修改",
        ]]
        return try JSONDecoder().decode([String: JSONValue].self, from: JSONSerialization.data(withJSONObject: raw))
    }

    func testComposerSendRulesRespectIdentityAndEveryBusyState() {
        let ready = ChatPresentationFixture.media(ready: true)
        let queued = ChatPresentationFixture.media(status: "queued")
        func allowed(base: Bool = true, busy: Bool = false, recording: Bool = false,
                     mediaBusy: Bool = false, picker: Bool = false, expected: String? = "qa-owner-a",
                     draft: String? = "qa-owner-a", items: [QueuedMedia] = []) -> Bool {
            ChatComposerRules.canSend(baseAllowed: base, busy: busy, recording: recording, mediaBusy: mediaBusy,
                                      pickerPending: picker, expectedNamespace: expected, draftNamespace: draft, items: items)
        }
        XCTAssertTrue(allowed())
        XCTAssertFalse(allowed(draft: nil))
        XCTAssertFalse(allowed(draft: "qa-owner-b"))
        XCTAssertTrue(allowed(items: [ready]))
        XCTAssertFalse(allowed(base: false))
        XCTAssertFalse(allowed(busy: true, items: [ready]))
        XCTAssertFalse(allowed(recording: true, items: [ready]))
        XCTAssertFalse(allowed(mediaBusy: true, items: [ready]))
        XCTAssertFalse(allowed(picker: true, items: [ready]))
        XCTAssertFalse(allowed(expected: nil))
        XCTAssertFalse(allowed(draft: nil, items: [ready]))
        XCTAssertFalse(allowed(draft: "qa-owner-b", items: [ready]))
        XCTAssertFalse(allowed(items: [queued]))
        XCTAssertFalse(allowed(items: [ready, queued]))
    }

    func testMediaSelectionContextRejectsChangedIdentityAndLatePickerResult() {
        let selectionID = UUID()
        let revision = UUID()
        let context = MediaSelectionContext(id: selectionID, revision: revision, namespace: "qa-owner-a")
        XCTAssertTrue(context.matches(revision: revision, namespace: "qa-owner-a", selectionID: selectionID))
        XCTAssertFalse(context.matches(revision: UUID(), namespace: "qa-owner-a", selectionID: selectionID))
        XCTAssertFalse(context.matches(revision: revision, namespace: "qa-owner-b", selectionID: selectionID))
        XCTAssertFalse(context.matches(revision: revision, namespace: nil, selectionID: selectionID))
        XCTAssertFalse(context.matches(revision: revision, namespace: "qa-owner-a", selectionID: UUID()))
        XCTAssertFalse(context.matches(revision: revision, namespace: "qa-owner-a", selectionID: nil))
    }

    func testDraftRestoreRejectsStaleGenerationChangedIdentityAndCancellation() {
        let request = UUID()
        func accepted(latest: UUID, current: String? = "qa-owner-a", authenticated: String? = "qa-owner-a", canceled: Bool = false) -> Bool {
            MediaDraft.acceptsRestoreSnapshot(request: request, latest: latest, namespace: "qa-owner-a",
                                              currentNamespace: current, authenticatedNamespace: authenticated, canceled: canceled)
        }
        XCTAssertTrue(accepted(latest: request))
        XCTAssertFalse(accepted(latest: UUID()))
        XCTAssertFalse(accepted(latest: request, current: "qa-owner-b"))
        XCTAssertFalse(accepted(latest: request, authenticated: "qa-owner-b"))
        XCTAssertFalse(accepted(latest: request, current: nil))
        XCTAssertFalse(accepted(latest: request, authenticated: nil))
        XCTAssertFalse(accepted(latest: request, canceled: true))
    }

    func testHandledReceiptImmediatelyFinishesInteractionWithoutFollowUpRead() {
        let request = ChatPresentationFixture.action()
        for status in ClientActionReceipt.handledStatuses {
            var display = ClientActionPresentation(request: request)
            XCTAssertTrue(display.acknowledge(ClientActionReceipt(id: request.id, status: status)))
            XCTAssertEqual(display.current.status, status)
            XCTAssertTrue(display.current.finished)
            XCTAssertFalse(display.current.pending)
            XCTAssertFalse(display.current.can_respond)
            // 回执只有状态，撤回权限必须等待完整、重新鉴权的GET结果。
            XCTAssertFalse(display.current.can_revoke)
        }
    }

    func testAutomaticLocationOnlyAcceptsPendingCoarseRequestFromOwner() {
        let time = Date(timeIntervalSince1970: 1000)
        func request(kind: String = "capture_location", precision: String = "coarse", status: String = "PENDING",
                     requester: String = "public-target", canRespond: Bool = true, expires: Double = 2000) -> ClientActionRequest {
            ChatPresentationFixture.action(kind: kind, status: status, canRespond: canRespond,
                parameters: ["precision": .string(precision)], expiresAt: expires, requesterID: requester)
        }
        XCTAssertTrue(request().permitsAutomaticCoarseLocation(at: time))
        XCTAssertFalse(request(precision: "precise").permitsAutomaticCoarseLocation(at: time))
        XCTAssertFalse(request(kind: "read_health").permitsAutomaticCoarseLocation(at: time))
        XCTAssertFalse(request(kind: "cloud.disclose").permitsAutomaticCoarseLocation(at: time))
        XCTAssertFalse(request(requester: "other-member").permitsAutomaticCoarseLocation(at: time))
        XCTAssertFalse(request(canRespond: false).permitsAutomaticCoarseLocation(at: time))
        XCTAssertFalse(request(expires: 1000).permitsAutomaticCoarseLocation(at: time))
        for status in ClientActionReceipt.handledStatuses {
            XCTAssertFalse(request(status: status).permitsAutomaticCoarseLocation(at: time))
        }
    }

    func testConsentRecipientNoticeOnlyForDataProvidedToAnotherMember() {
        for kind in ["member.notify", "cloud.disclose"] {
            XCTAssertNil(ClientActionConsentCopy.recipientNotice(ChatPresentationFixture.action(kind: kind)))
        }
        for kind in ["capture_location", "read_health", "choose_files", "choose_photos", "authorize_records", "data.share", "provide_text", "choose_option"] {
            XCTAssertNotNil(ClientActionConsentCopy.recipientNotice(ChatPresentationFixture.action(kind: kind)))
            XCTAssertNil(ClientActionConsentCopy.recipientNotice(ChatPresentationFixture.action(kind: kind, requesterID: "public-target")))
        }
    }

    func testClarificationUsesOnlyPendingPersonalRequestBoundToThisTask() {
        let date = Date(timeIntervalSince1970: 1000)
        var turn = ChatPresentationFixture.turn(status: "WAITING_CLIENT")
        for kind in ["provide_text", "choose_option"] {
            let request = ChatPresentationFixture.action(kind: kind, taskID: turn.task_id, requesterID: "public-target")
            turn.client_actions = [request]
            XCTAssertTrue(turn.awaitsPersonalClarification(at: date))
            XCTAssertFalse(turn.blocksComposer)
            XCTAssertTrue(turn.pending)
            for status in ["WAITING_PRIVACY", "EXECUTING", "SUCCEEDED", "FAILED", "CANCELED"] {
                var otherState = turn; otherState.status = status
                XCTAssertFalse(otherState.awaitsPersonalClarification(at: date))
            }
            for unrelated in [
                ChatPresentationFixture.action(kind: kind, taskID: "other-task", requesterID: "public-target"),
                ChatPresentationFixture.action(kind: kind, taskID: nil, requesterID: "public-target"),
                ChatPresentationFixture.action(kind: kind, taskID: turn.task_id),
                ChatPresentationFixture.action(kind: kind, canRespond: false, taskID: turn.task_id, requesterID: "public-target"),
                ChatPresentationFixture.action(kind: kind, expiresAt: 1000, taskID: turn.task_id, requesterID: "public-target"),
                ChatPresentationFixture.action(kind: kind, taskID: turn.task_id, requesterID: "public-target", scope: "family")
            ] {
                turn.client_actions = [unrelated]
                XCTAssertFalse(turn.awaitsPersonalClarification(at: date))
            }
        }
        for kind in ["cloud.disclose", "read_health", "android.assist", "capture_location", "authorize_records"] {
            turn.client_actions = [ChatPresentationFixture.action(kind: kind, taskID: turn.task_id, requesterID: "public-target")]
            XCTAssertFalse(turn.awaitsPersonalClarification(at: date))
        }
    }

    func testClarificationReceiptStopsWaitingWithoutChangingTaskOrConversationIdentity() {
        let date = Date(timeIntervalSince1970: 1000)
        for kind in ["provide_text", "choose_option"] {
            var turn = ChatPresentationFixture.turn(status: "WAITING_CLIENT")
            let request = ChatPresentationFixture.action(kind: kind, taskID: turn.task_id, requesterID: "public-target")
            var presentation = ClientActionPresentation(request: request)
            XCTAssertTrue(presentation.current.awaitsPersonalClarification(for: turn.task_id, at: date))
            XCTAssertTrue(presentation.acknowledge(ClientActionReceipt(id: request.id, status: "RESPONDED")))
            XCTAssertFalse(presentation.receive(request))
            turn.client_actions = [presentation.current]
            XCTAssertFalse(turn.awaitsPersonalClarification(at: date))
            XCTAssertEqual(presentation.current.task_id, request.task_id)
            XCTAssertEqual(presentation.current.conversation_id, request.conversation_id)
            XCTAssertFalse(presentation.current.can_respond)
            XCTAssertFalse(presentation.current.can_revoke)
            // 回执只结束补充交互，客户端不能自行把服务端任务改成成功或执行中。
            XCTAssertEqual(turn.status, "WAITING_CLIENT")
            for status in ["DENIED", "EXPIRED", "REVOKED", "CANCELED"] {
                turn.client_actions = [request.applyingReceipt(ClientActionReceipt(id: request.id, status: status))]
                XCTAssertFalse(turn.awaitsPersonalClarification(at: date))
            }
        }
    }

    @MainActor
    func testAutomaticLocationAttemptsCannotRepeatOrRunAcrossConcurrentCards() {
        let attempts = AutomaticClientActionAttempts()
        XCTAssertTrue(attempts.begin(actionID: "one", namespace: "alice", expiresAt: 2000, now: 1000))
        XCTAssertFalse(attempts.begin(actionID: "one", namespace: "alice", expiresAt: 2000, now: 1000))
        XCTAssertFalse(attempts.begin(actionID: "two", namespace: "alice", expiresAt: 2000, now: 1000))
        attempts.finish(actionID: "one", namespace: "bob")
        XCTAssertFalse(attempts.begin(actionID: "two", namespace: "alice", expiresAt: 2000, now: 1000))
        attempts.finish(actionID: "one", namespace: "alice")
        XCTAssertFalse(attempts.begin(actionID: "one", namespace: "alice", expiresAt: 2000, now: 1000))
        XCTAssertTrue(attempts.begin(actionID: "two", namespace: "alice", expiresAt: 2000, now: 1000))
        attempts.finish(actionID: "two", namespace: "alice")
        XCTAssertTrue(attempts.begin(actionID: "one", namespace: "bob", expiresAt: 2000, now: 1000))
        attempts.finish(actionID: "one", namespace: "bob")
        XCTAssertFalse(attempts.begin(actionID: "expired", namespace: "alice", expiresAt: 999, now: 1000))
    }

    func testLatePendingOrRespondedCannotRestoreHandledOrRevokedRequest() {
        let pending = ChatPresentationFixture.action()
        var display = ClientActionPresentation(request: pending)
        XCTAssertTrue(display.acknowledge(ClientActionReceipt(id: pending.id, status: "RESPONDED")))
        XCTAssertFalse(display.receive(pending))
        XCTAssertEqual(display.current.status, "RESPONDED")
        let revoked = ChatPresentationFixture.action(status: "REVOKED")
        XCTAssertTrue(display.receive(revoked))
        XCTAssertFalse(display.current.can_revoke)
        XCTAssertFalse(display.receive(ChatPresentationFixture.action(status: "RESPONDED", canRevoke: true)))
        XCTAssertFalse(display.acknowledge(ClientActionReceipt(id: pending.id, status: "RESPONDED")))
        XCTAssertEqual(display.current.status, "REVOKED")
        for terminal in ["DENIED", "EXPIRED", "REVOKED", "CANCELED"] {
            var closed = ClientActionPresentation(request: ChatPresentationFixture.action(status: terminal))
            XCTAssertFalse(closed.receive(pending))
            XCTAssertFalse(closed.receive(ChatPresentationFixture.action(status: "RESPONDED")))
            XCTAssertEqual(closed.current.status, terminal)
        }
    }

    func testFullSameStatusUpdateRefreshesPermissionsAndRepeatedReceiptPreservesThem() {
        let pending = ChatPresentationFixture.action()
        var display = ClientActionPresentation(request: pending)
        XCTAssertTrue(display.acknowledge(ClientActionReceipt(id: pending.id, status: "RESPONDED")))
        let complete = ChatPresentationFixture.action(status: "RESPONDED", canRevoke: true,
                                                     parameters: ["prompt": .string("公开的当前范围")])
        XCTAssertTrue(display.receive(complete))
        XCTAssertTrue(display.current.can_revoke)
        XCTAssertTrue(display.acknowledge(ClientActionReceipt(id: pending.id, status: "RESPONDED")))
        XCTAssertTrue(display.current.can_revoke)
        XCTAssertEqual(display.current.parameters["prompt"], .string("公开的当前范围"))
        let changed = ChatPresentationFixture.action(status: "RESPONDED", canRevoke: false,
                                                    parameters: ["prompt": .string("范围已更新")])
        XCTAssertTrue(display.receive(changed))
        XCTAssertFalse(display.current.can_revoke)
        XCTAssertEqual(display.current.parameters["prompt"], .string("范围已更新"))
        var waiting = ClientActionPresentation(request: pending)
        XCTAssertTrue(waiting.receive(ChatPresentationFixture.action(canRespond: false,
                                  parameters: ["prompt": .string("等待其他设备处理")])))
        XCTAssertFalse(waiting.current.can_respond)
        XCTAssertEqual(waiting.current.parameters["prompt"], .string("等待其他设备处理"))
    }

    func testDifferentRequestIdentityResetsPresentationAndWrongReceiptIsIgnored() {
        let first = ChatPresentationFixture.action(status: "REVOKED")
        let second = ChatPresentationFixture.action(id: "55555555-5555-4555-8555-555555555555",
                                                   parameters: ["prompt": .string("新的公开问题")])
        var display = ClientActionPresentation(request: first)
        XCTAssertTrue(display.receive(second))
        XCTAssertEqual(display.current.id, second.id)
        XCTAssertTrue(display.current.pending)
        XCTAssertTrue(display.current.can_respond)
        XCTAssertEqual(display.current.parameters, second.parameters)
        XCTAssertFalse(display.acknowledge(ClientActionReceipt(id: first.id, status: "RESPONDED")))
        XCTAssertFalse(display.acknowledge(ClientActionReceipt(id: second.id, status: "PENDING")))
        XCTAssertFalse(display.acknowledge(ClientActionReceipt(id: second.id, status: "UNKNOWN")))
        XCTAssertEqual(display.current, second)
    }

    func testExpiryUsesFixedClockAndMemberMessageDoesNotClaimReadReceipt() {
        let instant = Date(timeIntervalSince1970: 1000)
        XCTAssertEqual(ChatPresentationFixture.action(expiresAt: 1001).effectiveStatus(at: instant), "PENDING")
        XCTAssertEqual(ChatPresentationFixture.action(expiresAt: 1000).effectiveStatus(at: instant), "EXPIRED")
        XCTAssertEqual(ChatPresentationFixture.action(expiresAt: 999).effectiveStatus(at: instant), "EXPIRED")
        XCTAssertEqual(ChatPresentationFixture.action(status: "RESPONDED", expiresAt: 999).effectiveStatus(at: instant), "RESPONDED")
        let incoming = ChatPresentationFixture.action(kind: "member.notify", status: "RESPONDED", taskID: nil)
        let outgoing = ChatPresentationFixture.action(kind: "member.notify", status: "RESPONDED")
        XCTAssertEqual(incoming.summaryLabel, "成员消息")
        XCTAssertEqual(outgoing.summaryLabel, "成员消息已发送")
        XCTAssertFalse(incoming.summaryLabel.contains("已读"))
        XCTAssertFalse(outgoing.summaryLabel.contains("已读"))
        XCTAssertEqual(ChatPresentationFixture.action(kind: "cloud.disclose", status: "RESPONDED").summaryLabel, "已允许本次云端披露")
        XCTAssertEqual(ChatPresentationFixture.action(kind: "choose_option", status: "RESPONDED").summaryLabel, "已完成选择")
    }

    func testApprovalReceiptRemovesActionImmediatelyAndDoesNotRewriteConversation() {
        let turn = ChatPresentationFixture.turn()
        let identifier = turn.approvals[0].id
        let approved = turn.acknowledgingApproval(identifier, status: "APPROVED")
        XCTAssertTrue(approved.approvals.isEmpty)
        XCTAssertEqual(approved.status, "APPROVED")
        XCTAssertEqual(approved.user_text, turn.user_text)
        XCTAssertEqual(approved.assistant_text, turn.assistant_text)
        XCTAssertEqual(approved.id, turn.id)
        XCTAssertEqual(approved.acknowledgingApproval(identifier, status: "CANCELED").status, "APPROVED")
        let denied = turn.acknowledgingApproval(identifier, status: "CANCELED")
        XCTAssertEqual(denied.status, "CANCELED")
        XCTAssertTrue(denied.approvals.isEmpty)
        let completed = ChatPresentationFixture.turn(status: "SUCCEEDED").acknowledgingApproval(identifier, status: "APPROVED")
        XCTAssertEqual(completed.status, "SUCCEEDED")
        XCTAssertTrue(completed.approvals.isEmpty)
    }

    func testApprovalReceiptCannotRemoveOtherApprovalOrApplyUnknownStatus() {
        let turn = ChatPresentationFixture.turn()
        let unrelated = turn.acknowledgingApproval("unrelated-approval", status: "APPROVED")
        XCTAssertEqual(unrelated.approvals.map(\.id), turn.approvals.map(\.id))
        XCTAssertEqual(unrelated.status, "AWAITING_APPROVAL")
        let invalid = turn.acknowledgingApproval(turn.approvals[0].id, status: "SUCCEEDED")
        XCTAssertEqual(invalid.approvals.map(\.id), turn.approvals.map(\.id))
        XCTAssertEqual(invalid.status, "AWAITING_APPROVAL")
    }

    @MainActor
    func testHandledRequestScreenshotsCollapsedExpandedAndAccessible() async throws {
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        let cases: [(String, ColorScheme, DynamicTypeSize, Bool)] = [
            ("chat-handled-light-collapsed", .light, .large, false),
            ("chat-handled-light-expanded", .light, .large, true),
            ("chat-handled-dark-expanded", .dark, .large, true),
            ("chat-handled-dark-accessibility", .dark, .accessibility2, false)
        ]
        for (name, scheme, size, expanded) in cases {
            let controller = UIHostingController(rootView: ChatHandledPublicPage(scheme: scheme, size: size, expanded: expanded,
                onUnexpectedAction: { XCTFail("终态截图不应撤回授权或访问资料") }))
            controller.overrideUserInterfaceStyle = scheme == .dark ? .dark : .light
            let window = UIWindow(windowScene: scene)
            window.frame = CGRect(x: 0, y: 0, width: 390, height: 1500)
            window.rootViewController = controller
            window.isHidden = false
            defer { window.isHidden = true; window.rootViewController = nil }
            controller.view.frame = window.bounds
            controller.view.layoutIfNeeded()
            try await Task.sleep(for: .milliseconds(350))
            controller.view.layoutIfNeeded()
            let format = UIGraphicsImageRendererFormat(); format.scale = 2
            var rendered = false
            let image = UIGraphicsImageRenderer(bounds: controller.view.bounds, format: format).image { _ in
                rendered = controller.view.drawHierarchy(in: controller.view.bounds, afterScreenUpdates: true)
            }
            XCTAssertTrue(rendered)
            XCTAssertEqual(image.size.width, 390, accuracy: 1)
            let attachment = XCTAttachment(image: image)
            attachment.name = name
            attachment.lifetime = .keepAlways
            add(attachment)
        }
    }

    @MainActor
    func testPublicComposerScreenshotsInLightDarkAndLargeType() async throws {
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        let cases: [(String, ColorScheme, DynamicTypeSize)] = [
            ("chat-composer-light", .light, .large),
            ("chat-composer-dark", .dark, .large),
            ("chat-composer-dark-accessibility", .dark, .accessibility2)
        ]
        for (name, scheme, size) in cases {
            let controller = UIHostingController(rootView: ChatPublicComposerPage(
                scheme: scheme, size: size, onUnexpectedAction: { XCTFail("截图渲染不得打开选择器或执行任何操作") }
            ))
            controller.overrideUserInterfaceStyle = scheme == .dark ? .dark : .light
            let window = UIWindow(windowScene: scene)
            window.frame = CGRect(x: 0, y: 0, width: 390, height: 1000)
            window.rootViewController = controller
            window.isHidden = false
            defer { window.isHidden = true; window.rootViewController = nil }
            controller.view.frame = window.bounds
            controller.view.layoutIfNeeded()
            try await Task.sleep(for: .milliseconds(350))
            controller.view.layoutIfNeeded()
            let format = UIGraphicsImageRendererFormat(); format.scale = 2
            var rendered = false
            let image = UIGraphicsImageRenderer(bounds: controller.view.bounds, format: format).image { _ in
                rendered = controller.view.drawHierarchy(in: controller.view.bounds, afterScreenUpdates: true)
            }
            XCTAssertTrue(rendered)
            XCTAssertEqual(image.size.width, 390, accuracy: 1)
            XCTAssertEqual(image.size.height, 1000, accuracy: 1)
            let attachment = XCTAttachment(image: image)
            attachment.name = name
            attachment.lifetime = .keepAlways
            add(attachment)
        }
    }

    @MainActor
    func testPublicPresentationScreenshotsInLightDarkAndLargeType() async throws {
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        let cases: [(String, ColorScheme, DynamicTypeSize, Bool)] = [
            ("chat-presentation-light-collapsed", .light, .large, false),
            ("chat-presentation-light-expanded", .light, .large, true),
            ("chat-presentation-dark-expanded", .dark, .large, true),
            ("chat-presentation-dark-accessibility", .dark, .accessibility2, false)
        ]
        for (name, scheme, size, expanded) in cases {
            let controller = UIHostingController(rootView: ChatPublicPresentationPage(
                scheme: scheme, size: size, expanded: expanded,
                onDecision: { _ in XCTFail("渲染展示卡片不应触发审批决定") }
            ))
            controller.overrideUserInterfaceStyle = scheme == .dark ? .dark : .light
            let window = UIWindow(windowScene: scene)
            window.frame = CGRect(x: 0, y: 0, width: 390, height: 1500)
            window.rootViewController = controller
            window.isHidden = false
            defer { window.isHidden = true; window.rootViewController = nil }
            controller.view.frame = window.bounds
            controller.view.layoutIfNeeded()
            try await Task.sleep(for: .milliseconds(350))
            controller.view.layoutIfNeeded()
            let format = UIGraphicsImageRendererFormat()
            format.scale = 2
            var rendered = false
            let image = UIGraphicsImageRenderer(bounds: controller.view.bounds, format: format).image { _ in
                rendered = controller.view.drawHierarchy(in: controller.view.bounds, afterScreenUpdates: true)
            }
            XCTAssertTrue(rendered)
            XCTAssertEqual(image.size.width, 390, accuracy: 1)
            XCTAssertEqual(image.size.height, 1500, accuracy: 1)
            let attachment = XCTAttachment(image: image)
            attachment.name = name
            attachment.lifetime = .keepAlways
            add(attachment)
        }
    }
}

private enum ChatPresentationFixture {
    static func turn(status: String = "AWAITING_APPROVAL") -> StoredChatTurn {
        StoredChatTurn(id: "public-turn", sequence: 1, client_key: "public-message", user_text: "请整理公开文档说明",
                       task_id: "public-task", status: status, assistant_text: "以下操作等待你确认", error: nil,
                       approvals: [ApprovalEntry(id: "public-approval", task_id: "public-task", capability: "mail.send@v1", arguments: [:])],
                       web_sources: [], sources: [])
    }

    static func action(id: String = "33333333-3333-4333-8333-333333333333", kind: String = "choose_option",
                       status: String = "PENDING", canRespond: Bool? = nil, canRevoke: Bool = false,
                       parameters: [String: JSONValue] = [:], expiresAt: Double = 4_000_000_000,
                       taskID: String? = "44444444-4444-4444-8444-444444444444", requesterID: String = "public-requester", scope: String = "personal") -> ClientActionRequest {
        ClientActionRequest(id: id, kind: kind, status: status, purpose: "公开文档展示验收",
                            requested_by: .init(id: requesterID, name: "公开测试成员"),
                            target_member_id: "public-target", scope: scope, created_at: 500, expires_at: expiresAt,
                            parameters: parameters, task_id: taskID, conversation_id: nil, notification_id: nil,
                            can_respond: canRespond ?? (status == "PENDING"), can_revoke: canRevoke)
    }

    /// 只构造值对象，不访问MediaUploadStore，也不创建任何磁盘或服务端上传。
    static func media(status: String = "uploading", ready: Bool = false) -> QueuedMedia {
        let identifier = "22222222-2222-4222-8222-222222222222"
        let asset = ready ? MediaAsset(record_id: identifier, version: 1, kind: "file", mime_type: "text/plain",
                                      name: "Python公开说明.txt", size: 3 * 1048576,
                                      sha256: String(repeating: "0", count: 64), duration_seconds: nil, processing: nil) : nil
        return QueuedMedia(id: identifier, clientID: identifier, name: "Python公开说明.txt", kind: "file", mime: "text/plain",
                           size: 3 * 1048576, sha256: String(repeating: "0", count: 64), totalChunks: 3, scope: "chat",
                           createdAt: 1_800_000_000, serverID: nil, received: ready ? [0, 1, 2] : [0],
                           status: ready ? "complete" : status, asset: asset, error: nil)
    }
}

private struct ChatHandledPublicPage: View {
    let scheme: ColorScheme
    let size: DynamicTypeSize
    let expanded: Bool
    let onUnexpectedAction: () -> Void
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                Text("已处理请求展示验收").font(.title2.bold())
                Text("仅包含公开文档示例；查看详情不会访问服务器，撤回按钮不会执行业务操作。")
                    .font(.caption).foregroundStyle(.secondary)
                ClientActionTerminalSummary(summary: ChatPresentationFixture.action(status: "RESPONDED").summaryLabel,
                    requester: "公开测试成员", icon: "checkmark.circle", tint: .teal, notice: nil, expanded: .constant(false)) {
                    Text("已选择：简短总结")
                }
                ClientActionTerminalSummary(summary: ChatPresentationFixture.action(kind: "choose_files", status: "RESPONDED", canRevoke: true).summaryLabel,
                    requester: "公开测试成员", icon: "doc", tint: .teal, notice: expanded ? "本次资料范围可在详情中查看" : nil,
                    expanded: .constant(expanded)) {
                    Text("本次用途：整理 Python asyncio 官方文档。")
                    Text("已提供公开文档说明，仅用于当前任务，不改变长期共享设置。")
                        .font(.caption).foregroundStyle(.secondary)
                    Button(role: .destructive, action: onUnexpectedAction) {
                        Label("撤回本次任务授权", systemImage: "arrow.uturn.backward.circle")
                            .frame(maxWidth: .infinity, minHeight: 44)
                    }.buttonStyle(.bordered)
                }
                ForEach(["DENIED", "REVOKED", "EXPIRED", "CANCELED"], id: \.self) { status in
                    ClientActionTerminalSummary(summary: ChatPresentationFixture.action(status: status).summaryLabel,
                        requester: "公开测试成员", icon: status == "EXPIRED" ? "clock" : "xmark.circle", tint: .secondary,
                        notice: nil, expanded: .constant(false)) {
                        Text("这项请求已结束，不能再次提交。")
                    }
                }
            }.padding(16)
        }
        .background(Color(uiColor: .systemGroupedBackground))
        .environment(\.colorScheme, scheme)
        .environment(\.dynamicTypeSize, size)
    }
}

private struct ChatPublicComposerPage: View {
    let scheme: ColorScheme
    let size: DynamicTypeSize
    let onUnexpectedAction: () -> Void
    @State private var input = "请结合这份公开文档，给出三点总结。"
    private let mentions = [ChatComposerMention(id: "public-member-a", name: "公开成员甲"),
                            ChatComposerMention(id: "public-member-b", name: "公开成员乙")]
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                Text("输入区展示验收").font(.title2.bold())
                Text("公开文本、两个成员标签与附件上传进度；不读取照片或写入队列。")
                    .font(.caption).foregroundStyle(.secondary)
                ChatComposerSurface(input: $input, mentions: mentions,
                                    references: [ChatDataReference(record_id: "8b934cc8-9f9f-4a2b-9b3e-9f56c7d174de", version: 1, title: "公开文档", kind: "document.file")], replyTitle: "继续讨论公开文档",
                                    busy: false, canSend: false, voiceRecording: false, canRecord: true, canAddAttachments: true, canReferenceData: true,
                                    onAdd: { _ in onUnexpectedAction() }, onMention: onUnexpectedAction, onReference: onUnexpectedAction,
                                    onInbox: onUnexpectedAction, onSend: onUnexpectedAction, onVoice: onUnexpectedAction,
                                    onRemoveMention: { _ in onUnexpectedAction() }, onRemoveReference: { _ in onUnexpectedAction() }, onCancelReply: onUnexpectedAction) {
                    MediaQueueChip(item: ChatPresentationFixture.media(), removing: false, disabled: false,
                                   onRemove: onUnexpectedAction, onRetry: onUnexpectedAction)
                }
            }.padding(16)
        }
        .background(Color(uiColor: .systemGroupedBackground))
        .environment(\.colorScheme, scheme)
        .environment(\.dynamicTypeSize, size)
    }
}

private struct ChatPublicPresentationPage: View {
    let scheme: ColorScheme
    let size: DynamicTypeSize
    let expanded: Bool
    let onDecision: (String) -> Void
    private let sources = [
        ChatWebSource(title: "asyncio — Asynchronous I/O · Python 官方文档", url: "https://docs.python.org/3/library/asyncio.html"),
        ChatWebSource(title: "FastAPI 官方文档", url: "https://fastapi.tiangolo.com/"),
        ChatWebSource(title: "Python 协程与任务说明", url: "https://docs.python.org/3/library/asyncio-task.html")
    ]
    private var approval: ApprovalEntry {
        ApprovalEntry(id: "11111111-1111-4111-8111-111111111111", task_id: nil, capability: "mail.send@v1",
                      arguments: ["to": .string("documentation@example.invalid"),
                                  "subject": .string("Python asyncio 官方文档"),
                                  "text": .string("文档介绍如何使用 async/await 编写并发代码。\nhttps://docs.python.org/3/library/asyncio.html")])
    }
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                Text("聊天展示验收").font(.title2.bold())
                Text("仅包含公开文档与保留测试地址，卡片不会发出请求。")
                    .font(.caption).foregroundStyle(.secondary)
                ChatSearchSources(sources: sources, initiallyExpanded: expanded)
                ChatInteractionCard(title: "请选择下一步", subtitle: "依据公开文档继续说明", icon: "list.bullet.rectangle", status: "等待选择") {
                    Text("需要简短总结，还是希望查看协程示例？").font(.subheadline)
                    HStack {
                        Label("简短总结", systemImage: "text.alignleft")
                        Spacer()
                        Image(systemName: "chevron.right")
                    }.font(.subheadline).padding(12).background(.teal.opacity(0.07), in: RoundedRectangle(cornerRadius: 10))
                }
                ChatApprovalCard(approval: approval, busy: false, onDecision: onDecision)
            }.padding(16)
        }
        .background(Color(uiColor: .systemGroupedBackground))
        .environment(\.colorScheme, scheme)
        .environment(\.dynamicTypeSize, size)
    }
}
