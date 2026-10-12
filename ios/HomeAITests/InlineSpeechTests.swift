import XCTest
@testable import HomeAI

final class InlineSpeechTests: XCTestCase {
    private func answer(_ text: String, status: String = "EXECUTING", id: String = "current", kind: String = "chat") -> StoredChatTurn {
        var turn = StoredChatTurn(id: id, sequence: 1, client_key: id, user_text: "公开测试", task_id: "task-" + id,
            status: status, assistant_text: text, error: nil, approvals: [], web_sources: [], sources: [])
        turn.interaction_kind = kind
        turn.requires_online_revalidation = status == "EXECUTING"
        return turn
    }

    func testAutomaticReadingWaitsForSentenceAndDoesNotRepeatFinalSnapshot() {
        var reader = ChatAutomaticSpeech()
        XCTAssertTrue(reader.update([answer("你好，还在")], enabled: true).isEmpty)
        XCTAssertEqual(reader.update([answer("你好，还在这里。下一")], enabled: true).map(\.text), ["你好，还在这里。"])
        XCTAssertTrue(reader.update([answer("你好，还在这里。下一段")], enabled: true).isEmpty)
        XCTAssertEqual(reader.update([answer("你好，还在这里。下一段来了。尾句", status: "SUCCEEDED")], enabled: true).map(\.text), ["下一段来了。尾句"])
        XCTAssertTrue(reader.update([answer("你好，还在这里。下一段来了。尾句", status: "SUCCEEDED")], enabled: true).isEmpty)
    }

    func testAutomaticReadingSkipsInitialHistoryAndOlderPagesButReadsNewCompletion() {
        var reader = ChatAutomaticSpeech()
        let history = answer("之前的回答。", status: "SUCCEEDED", id: "history")
        let waiting = answer("", status: "WAITING_CLIENT", id: "waiting", kind: "task")
        reader.baseline([history, waiting])
        XCTAssertTrue(reader.update([history, waiting], enabled: true).isEmpty)
        XCTAssertEqual(reader.update([answer("任务做好了。", status: "SUCCEEDED", id: "waiting", kind: "task")], enabled: true).map(\.text), ["任务做好了。"])
        let older = answer("更旧的回答。", status: "SUCCEEDED", id: "older")
        reader.baseline([older])
        XCTAssertTrue(reader.update([older], enabled: true).isEmpty)
    }

    func testTaskVisibleTextIsReadAndDisabledAnswerDoesNotReplay() {
        var reader = ChatAutomaticSpeech()
        XCTAssertEqual(reader.update([answer("已经找到资料，正在整理。", kind: "task")], enabled: true).map(\.text), ["已经找到资料，正在整理。"])
        XCTAssertEqual(reader.update([answer("最终回复。", status: "SUCCEEDED", kind: "task")], enabled: true).map(\.text), ["最终回复。"])
        XCTAssertTrue(reader.update([answer("已关闭时的回答。", status: "SUCCEEDED", id: "disabled")], enabled: false).isEmpty)
        XCTAssertTrue(reader.update([answer("已关闭时的回答。", status: "SUCCEEDED", id: "disabled")], enabled: true).isEmpty)
    }

    func testChangedStreamingTextWaitsForFinalWhileVisibleFailureAndTemporaryAccessHaveValidation() {
        var reader = ChatAutomaticSpeech()
        XCTAssertEqual(reader.update([answer("原句。")], enabled: true).count, 1)
        XCTAssertTrue(reader.update([answer("改写中的句子。")], enabled: true).isEmpty)
        XCTAssertEqual(reader.update([answer("改写后的句子。", status: "SUCCEEDED")], enabled: true).map(\.text), ["改写后的句子。"])
        XCTAssertFalse(reader.suppressedMessages.contains("current:answer"))
        reader.suppress(["stopped"])
        XCTAssertTrue(reader.update([answer("停止后到达的回答。", status: "SUCCEEDED", id: "stopped")], enabled: true).isEmpty)
        XCTAssertEqual(reader.update([answer("本次查询失败，请稍后重试。", status: "FAILED", id: "failed")], enabled: true).map(\.text), ["本次查询失败，请稍后重试。"])
        var temporary = answer("临时授权内容。", status: "SUCCEEDED", id: "temporary")
        temporary.requires_online_revalidation = true
        let protected = reader.update([temporary], enabled: true)
        XCTAssertEqual(protected.map(\.text), ["临时授权内容。"])
        XCTAssertTrue(protected.first?.context.matches(temporary) == true)
    }

    func testAllVisibleTextPartsAndResourceCaptionsBecomeOneAnswerWithoutDuplicates() throws {
        var turn = answer("**主要答复**", status: "SUCCEEDED")
        var repeated = ChatPart(type: "text"); repeated.text = " **主要答复** "
        var extra = ChatPart(type: "text"); extra.text = "补充的说明。"
        var resource = ChatPart(type: "file"); resource.title = "行程表"; resource.summary = "可下载的文件。"
        var h5 = ChatPart(type: "h5"); h5.title = "费用图表"; h5.html = "<script>秘密内容</script>"
        var action = ChatPart(type: "approval"); action.title = "不要朗读的按钮"; action.action_id = "action"
        turn.assistant_parts = [repeated, extra, resource, h5, action]
        let message = try XCTUnwrap(ChatSpeechMessage.messages(in: turn).first)
        XCTAssertEqual(message.kind, .answer)
        XCTAssertEqual(message.text, "主要答复\n补充的说明。\n行程表\n可下载的文件。\n费用图表")
        XCTAssertFalse(message.rawText.contains("秘密内容"))
        XCTAssertFalse(message.rawText.contains("不要朗读"))
    }

    func testAcknowledgementAndFinalAnswerAreIndependentAndStoppedAcknowledgementDoesNotSilenceResult() {
        var reader = ChatAutomaticSpeech()
        var pending = answer("", status: "EXECUTING", kind: "task")
        pending.acknowledgement = "我来查一下。"
        let first = reader.update([pending], enabled: true)
        XCTAssertEqual(first.map(\.messageID), ["current:acknowledgement"])
        reader.suppressMessages(["current:acknowledgement"])
        var result = answer("资料已经找到了。", status: "SUCCEEDED", kind: "task")
        result.acknowledgement = pending.acknowledgement
        XCTAssertEqual(reader.update([result], enabled: true).map(\.text), ["资料已经找到了。"])
        XCTAssertTrue(reader.update([result], enabled: true).isEmpty)
    }

    func testWaitingPromptAndLaterAnswerAreBothReadExactlyOnce() {
        var reader = ChatAutomaticSpeech()
        let waiting = answer("想查询哪一天的天气？", status: "WAITING_CLIENT", kind: "task")
        XCTAssertEqual(reader.update([waiting], enabled: true).map(\.text), ["想查询哪一天的天气？"])
        XCTAssertTrue(reader.update([waiting], enabled: true).isEmpty)
        let result = answer("明天晴天。", status: "SUCCEEDED", kind: "task")
        XCTAssertEqual(reader.update([result], enabled: true).map(\.text), ["明天晴天。"])
        XCTAssertTrue(reader.update([waiting], enabled: true).isEmpty)
        XCTAssertTrue(reader.update([result], enabled: true).isEmpty)
    }

    func testPureCodeReplyHasSpeakableFallbackAndPartOnlyAnswerIsRead() throws {
        XCTAssertEqual(ChatAutomaticSpeech.spokenText("```swift\nprint(1)\n```"), "代码内容：\nprint(1)")
        var turn = answer("", status: "SUCCEEDED")
        var text = ChatPart(type: "text"); text.text = "只有分块的回复。"
        turn.assistant_parts = [text]
        XCTAssertEqual(ChatSpeechMessage.messages(in: turn).map(\.text), ["只有分块的回复。"])
        var reader = ChatAutomaticSpeech()
        XCTAssertEqual(reader.update([turn], enabled: true).map(\.text), ["只有分块的回复。"])
    }

    func testValidationChecksIdentityTextAndRevocationForCachedAudio() throws {
        let turn = answer("可朗读的答复。", status: "SUCCEEDED")
        let context = try XCTUnwrap(ChatSpeechMessage.messages(in: turn).first).validation
        XCTAssertTrue(context.matches(turn))
        XCTAssertFalse(context.matches(answer("被修改的答复。", status: "SUCCEEDED")))
        XCTAssertFalse(context.matches(answer("可朗读的答复。", status: "SUCCEEDED", id: "another")))
        var revoked = turn; revoked.result_redacted = true
        XCTAssertFalse(context.matches(revoked))
        var reader = ChatAutomaticSpeech()
        XCTAssertTrue(reader.update([revoked], enabled: true).isEmpty)
    }

    func testStreamingCanAppendSourcesButCannotChangeExistingSourceVersion() throws {
        let partial = answer("第一句话。下一段")
        let context = try XCTUnwrap(ChatSpeechMessage.messages(in: partial).first).validation
        var complete = answer("第一句话。下一段来了。", status: "SUCCEEDED")
        var attachment = ChatPart(type: "file"); attachment.record_id = "source"; attachment.version = 1; attachment.title = "资料"
        complete.assistant_parts = [attachment]
        XCTAssertTrue(context.matches(complete))
        var reader = ChatAutomaticSpeech()
        XCTAssertEqual(reader.update([partial], enabled: true).map(\.text), ["第一句话。"])
        XCTAssertEqual(reader.update([complete], enabled: true).map(\.text), ["下一段来了。\n资料"])
        let finalContext = try XCTUnwrap(ChatSpeechMessage.messages(in: complete).first).validation
        attachment.version = 2; complete.assistant_parts = [attachment]
        XCTAssertFalse(finalContext.matches(complete))
    }

    func testSentenceBoundaryKeepsDecimalsURLsAndCodeWhole() {
        XCTAssertEqual(ChatAutomaticSpeech.completePrefix("数值是 3.14，网址 https://example.com/path"), "")
        XCTAssertEqual(ChatAutomaticSpeech.completePrefix("第一句。```swift\nprint(\"这是代码。\")"), "第一句。")
        XCTAssertEqual(ChatAutomaticSpeech.spokenText("**回复**：见[说明](https://example.com)。\n\n```swift\nprint(1)\n```"), "回复：见说明。")
    }

    func testLongAnswerIsCompleteAndKeepsUnicodeCharacters() throws {
        let text = String(repeating: "这是完整回答，包含家庭 👨‍👩‍👧‍👦 和组合字符 e\u{301}。", count: 50)
        let parts = try SpeechPlayback.answerSegments(text)
        XCTAssertGreaterThan(parts.count, 1)
        XCTAssertLessThanOrEqual(parts[0].unicodeScalars.count, 40)
        XCTAssertEqual(parts.joined(), text)
        XCTAssertTrue(parts.allSatisfy { $0.unicodeScalars.count <= 120 })
        XCTAssertEqual(parts.flatMap(Array.init), Array(text))
    }

    func testInvalidAndWhitespaceOnlyInputCannotCreateSpeech() {
        XCTAssertThrowsError(try SpeechPlayback.answerSegments(" \n\t"))
        XCTAssertThrowsError(try SpeechPlayback.answerSegments(String(repeating: "字", count: 20001)))
        XCTAssertThrowsError(try SpeechPlayback.answerSegments("e" + String(repeating: "\u{301}", count: 251)))
        let parts = try? SpeechPlayback.answerSegments("开始" + String(repeating: " ", count: 700) + "结束")
        XCTAssertTrue(parts?.allSatisfy { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty } == true)
    }

    @MainActor func testStopDuringAutomaticPreparationCannotStartLatePlayback() async throws {
        let playback = SpeechPlayback(api: APIClient(persistConnection: false))
        playback.speakAnswer("公开测试", expectedNamespace: "unpaired")
        XCTAssertTrue(playback.busy)
        playback.stop(cancelServer: true)
        try await Task.sleep(for: .milliseconds(150))
        XCTAssertFalse(playback.busy)
        XCTAssertFalse(playback.playing)
        XCTAssertEqual(playback.remainingAnswerText, "公开测试")
        XCTAssertTrue(playback.voices.isEmpty)
    }

    @MainActor func testWrongIdentityDoesNotLoadVoiceOrStartPlayback() async throws {
        let playback = SpeechPlayback(api: APIClient(persistConnection: false))
        playback.speakAnswer("公开测试", expectedNamespace: "wrong-identity")
        for _ in 0..<20 where playback.busy { try await Task.sleep(for: .milliseconds(25)) }
        XCTAssertNotNil(playback.error)
        XCTAssertFalse(playback.playing)
        XCTAssertTrue(playback.voices.isEmpty)
    }

    @MainActor func testLiveChatSpeechProjectionKeepsAllReadableMessageKinds() async throws {
        let env = ProcessInfo.processInfo.environment
        guard env["TEST_RUNNER_HOMEAI_INLINE_SPEECH_LIVE"] == "1" || env["HOMEAI_INLINE_SPEECH_LIVE"] == "1" else {
            throw XCTSkip("需要已配对真机和真实聊天投影接口")
        }
        let api = AppServices.api
        await api.restoreConnectionIfNeeded()
        let namespace = try await api.syncNamespace()
        struct ConversationIdentity: Decodable { let id: String }
        let entries = try JSONDecoder().decode([ConversationIdentity].self, from: await api.request("GET", "/api/v1/conversations", expectedNamespace: namespace))
        guard let conversation = entries.first else { throw XCTSkip("没有可供只读验证的会话") }
        let page = try JSONDecoder().decode(StoredConversationPage.self, from: await api.request("GET", "/api/v1/conversations/" + conversation.id, expectedNamespace: namespace))
        var kinds = Set<String>(), inspected = 0
        for turn in page.turns.reversed().prefix(5) {
            let current = try JSONDecoder().decode(StoredChatTurn.self, from: await api.request("GET", "/api/v1/tasks/" + turn.task_id + "/chat-speech", expectedNamespace: namespace))
            XCTAssertEqual(current.task_id, turn.task_id)
            XCTAssertTrue(current.approvals.isEmpty)
            XCTAssertTrue(current.client_actions?.isEmpty != false)
            for message in ChatSpeechMessage.messages(in: current) {
                XCTAssertEqual(message.validation.matches(current), current.result_redacted != true)
                if current.result_redacted != true { kinds.insert(message.kind.rawValue); inspected += 1 }
            }
        }
        XCTAssertGreaterThan(inspected, 0)
        // 只记录检查数和类型，不输出、合成或播放真实私人聊天正文。
        print("CHAT_SPEECH_LIVE checked_messages=\(inspected) kinds=\(kinds.sorted().joined(separator: ",")) private_audio_generated=false")
    }

    @MainActor func testLiveOneTapPreparesVoiceAndPlaysPublicText() async throws {
        let env = ProcessInfo.processInfo.environment
        guard env["TEST_RUNNER_HOMEAI_INLINE_SPEECH_LIVE"] == "1" || env["HOMEAI_INLINE_SPEECH_LIVE"] == "1" else {
            throw XCTSkip("需要已配对真机和真实本地语音服务")
        }
        let api = AppServices.api
        await api.restoreConnectionIfNeeded()
        let namespace = try await api.syncNamespace()
        let playback = SpeechPlayback(api: api)
        XCTAssertTrue(playback.voices.isEmpty)
        playback.speakAnswer("这是聊天页直接朗读测试。", expectedNamespace: namespace)
        let deadline = Date().addingTimeInterval(120)
        var heard = false
        while playback.busy && Date() < deadline {
            if playback.playing { heard = true; break }
            try await Task.sleep(for: .milliseconds(100))
        }
        let failure = playback.error
        playback.stop(cancelServer: true)
        XCTAssertTrue(heard, failure ?? playback.status)
        XCTAssertNil(failure)
        XCTAssertFalse(playback.voices.isEmpty)
        try await Task.sleep(for: .milliseconds(250))
        XCTAssertFalse(playback.playing)
        XCTAssertFalse(playback.busy)
        let replayStart = Date()
        playback.speakAnswer("这是聊天页直接朗读测试。", expectedNamespace: namespace)
        while playback.busy && !playback.playing && Date().timeIntervalSince(replayStart) < 3 {
            try await Task.sleep(for: .milliseconds(25))
        }
        let replayed = playback.playing
        let delay = Date().timeIntervalSince(replayStart)
        playback.stop(cancelServer: true)
        XCTAssertTrue(replayed, playback.error ?? playback.status)
        XCTAssertLessThan(delay, 3)
        print("INLINE_SPEECH_CACHED_START_SECONDS=\(delay)")
    }

    @MainActor func testLiveAutomaticStreamPlaysTwoPublicSentencesOnceInOrder() async throws {
        let env = ProcessInfo.processInfo.environment
        guard env["TEST_RUNNER_HOMEAI_INLINE_SPEECH_LIVE"] == "1" || env["HOMEAI_INLINE_SPEECH_LIVE"] == "1" else {
            throw XCTSkip("需要已配对真机和真实本地语音服务")
        }
        guard VoiceCalls.shared.activeID == nil else { throw XCTSkip("当前正在通话，不打断真实通话") }
        let api = AppServices.api
        await api.restoreConnectionIfNeeded()
        let namespace = try await api.syncNamespace()
        let playback = SpeechPlayback(api: api)
        defer { playback.stop(cancelServer: true) }
        var reader = ChatAutomaticSpeech()
        reader.baseline([answer("历史不会自动朗读。", status: "SUCCEEDED", id: "history")])
        // 使用公开快照驱动真实朗读链路，不创建聊天、读取资料或触发业务工具。
        let partial = answer("这是自动朗读第一句。第二句还")
        XCTAssertTrue(partial.requires_online_revalidation == true)
        let first = try XCTUnwrap(reader.update([partial], enabled: true).first)
        XCTAssertEqual(first.text, "这是自动朗读第一句。")
        playback.speakAnswer(first.text, expectedNamespace: namespace, sources: first.sources)
        let final = answer("这是自动朗读第一句。第二句也已读完。", status: "SUCCEEDED")
        let remaining = reader.update([final], enabled: true)
        XCTAssertEqual(remaining.map(\.text), ["第二句也已读完。"])
        var completed = 0
        let started = Date()
        for segment in [first] + remaining {
            if completed > 0 {
                XCTAssertFalse(playback.busy)
                XCTAssertFalse(playback.playing)
                playback.speakAnswer(segment.text, expectedNamespace: namespace, sources: segment.sources)
            }
            var played = false
            let deadline = Date().addingTimeInterval(120)
            while playback.busy && Date() < deadline {
                played = played || playback.playing
                try await Task.sleep(for: .milliseconds(50))
            }
            XCTAssertTrue(played, playback.error ?? playback.status)
            XCTAssertFalse(playback.busy, "公开句子在时限内应完成朗读")
            XCTAssertFalse(playback.playing)
            XCTAssertNil(playback.error)
            XCTAssertEqual(playback.status, "朗读完成")
            XCTAssertEqual(playback.remainingAnswerText, "")
            guard played, !playback.busy, playback.error == nil else { return }
            completed += 1
        }
        XCTAssertEqual(completed, 2)
        XCTAssertTrue(reader.update([final], enabled: true).isEmpty)
        XCTAssertFalse(playback.busy)
        print("AUTOMATIC_SPEECH_PUBLIC_SEGMENTS_COMPLETED=\(completed) SECONDS=\(Date().timeIntervalSince(started))")
    }
}
