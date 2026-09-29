import SwiftUI
import WebKit

struct IsolatedH5View: View {
    @Environment(AppState.self) private var state
    @Environment(\.scenePhase) private var phase
    let title: String
    let html: String
    var taskID: String? = nil
    var expiresAt: Double? = nil
    @State private var allowed = false
    @State private var error: String?
    @State private var run = UUID()
    var body: some View {
        Group {
            if taskID != nil && !allowed {
                if let error { ContentUnavailableView("需要确认内容授权", systemImage: "lock", description: Text(error)) }
                else { ProgressView("正在确认本次任务授权…") }
            } else if html.utf8.count <= 64 * 1024 { IsolatedHTML(html: html) }
            else { ContentUnavailableView("页面超过显示限制", systemImage: "doc.badge.ellipsis") }
        }.navigationTitle(taskID != nil && !allowed ? "交互内容" : title).navigationBarTitleDisplayMode(.inline)
            .task(id: state.connectionRevision) {
                repeat {
                    await validate()
                    guard taskID != nil else { return }
                    do { try await Task.sleep(for: .seconds(30)) } catch { return }
                } while !Task.isCancelled && phase == .active
            }
            .onChange(of: phase) { _, phase in
                if phase == .background { run = UUID(); allowed = false }
                else if phase == .active { Task { await validate() } }
            }
            .onChange(of: state.serverReachable) { _, reachable in if !reachable && taskID != nil { run = UUID(); allowed = false; error = "连接恢复后重新确认授权" } }
            .onChange(of: state.taskEventRevision) { _, _ in if taskID != nil { Task { await validate() } } }
            .onDisappear { run = UUID(); allowed = false }
    }
    private func validate() async {
        guard let taskID else { allowed = true; return }
        let identifier = UUID(); run = identifier
        guard UUID(uuidString: taskID) != nil, phase == .active, state.connected else { allowed = false; return }
        if let expiresAt, expiresAt <= Date().timeIntervalSince1970 { allowed = false; error = "本次授权已到期"; return }
        do {
            struct Access: Decodable { let allowed: Bool; let reason: String? }
            let namespace = try await state.api.syncNamespace()
            let result = try JSONDecoder().decode(Access.self, from: await state.api.request("GET", "/api/v1/tasks/" + taskID + "/content-access", expectedNamespace: namespace))
            guard run == identifier, !Task.isCancelled, phase == .active, await state.api.cachedNamespace() == namespace else { return }
            allowed = result.allowed; error = result.allowed ? nil : (result.reason ?? "本次资料授权已撤回或过期")
        } catch { if run == identifier { allowed = false; self.error = "暂时无法确认访问权限，请恢复连接后重试" } }
    }
}

struct IsolatedHTML: UIViewRepresentable {
    static let policy = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'none'; form-action 'none'; frame-src 'none'; media-src 'none'; object-src 'none'; base-uri 'none'"
    let html: String
    func makeCoordinator() -> Coordinator { Coordinator() }
    func makeUIView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        configuration.mediaTypesRequiringUserActionForPlayback = .all
        configuration.allowsAirPlayForMediaPlayback = false
        // 不安装任何脚本消息处理器，页面没有原生权限或凭据桥。
        let denial = """
        (() => {
          const deny = () => { throw new DOMException('Unavailable in isolated content', 'NotAllowedError'); };
          const geo = Object.freeze({getCurrentPosition:(_, failure)=>failure?.({code:1,message:'Unavailable in isolated content'}),watchPosition:(_,failure)=>{failure?.({code:1,message:'Unavailable in isolated content'});return 0;},clearWatch:()=>{}});
          try { Object.defineProperty(Navigator.prototype,'geolocation',{get:()=>geo,configurable:false}); } catch (_) {}
          try { Object.defineProperty(window,'open',{value:()=>null,writable:false,configurable:false}); } catch (_) {}
          try { Object.defineProperty(navigator,'sendBeacon',{value:()=>false,writable:false,configurable:false}); } catch (_) {}
        })();
        """
        configuration.userContentController.addUserScript(WKUserScript(source: denial, injectionTime: .atDocumentStart, forMainFrameOnly: false))
        let view = WKWebView(frame: .zero, configuration: configuration)
        view.navigationDelegate = context.coordinator; view.uiDelegate = context.coordinator
        context.coordinator.webView = view
        view.isOpaque = false; view.backgroundColor = .systemBackground
        Task { @MainActor [weak view, weak coordinator = context.coordinator] in
            do {
                let rules = try await WKContentRuleListStore.default().compileContentRuleList(forIdentifier: "homeai-isolated-network-v1", encodedContentRuleList: "[{\"trigger\":{\"url-filter\":\"^https?://\"},\"action\":{\"type\":\"block\"}},{\"trigger\":{\"url-filter\":\"^wss?://\"},\"action\":{\"type\":\"block\"}}]")
                guard let view, let coordinator, coordinator.webView === view else { return }
                view.configuration.userContentController.add(rules!)
                coordinator.ready = true; coordinator.render()
            } catch { view?.loadHTMLString("<html><body>页面隔离配置失败，请关闭后重试。</body></html>", baseURL: nil) }
        }
        return view
    }
    func updateUIView(_ uiView: WKWebView, context: Context) {
        context.coordinator.pending = html
        context.coordinator.render()
    }
    static func dismantleUIView(_ uiView: WKWebView, coordinator: Coordinator) {
        coordinator.webView = nil; coordinator.pending = nil; coordinator.ready = false
        uiView.stopLoading(); uiView.navigationDelegate = nil; uiView.uiDelegate = nil; uiView.loadHTMLString("", baseURL: nil)
    }
    @MainActor final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate {
        var loaded: String?
        var pending: String?
        var ready = false
        weak var webView: WKWebView?
        func render() {
            guard ready, let pending, loaded != pending, let webView else { return }
            loaded = pending
            let content = "<!doctype html><html><head><meta name='viewport' content='width=device-width, initial-scale=1'><meta http-equiv=\"Content-Security-Policy\" content=\"" + IsolatedHTML.policy + "\"></head><body>" + pending + "</body></html>"
            webView.loadHTMLString(content, baseURL: nil)
        }
        func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping @MainActor @Sendable (WKNavigationActionPolicy) -> Void) {
            guard let url = navigationAction.request.url else { decisionHandler(.cancel); return }
            decisionHandler(url.scheme == "about" && url.absoluteString.hasPrefix("about:blank") ? .allow : .cancel)
        }
        func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration, for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? { nil }
        // 未实现 alert 时 WebKit 默认直接确认且不展示面板；避免新旧 SDK 的空参数闭包标注差异。
        func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping @MainActor @Sendable (Bool) -> Void) { completionHandler(false) }
        func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String, defaultText: String?, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping @MainActor @Sendable (String?) -> Void) { completionHandler(nil) }
        func webView(_ webView: WKWebView, requestMediaCapturePermissionFor origin: WKSecurityOrigin, initiatedByFrame frame: WKFrameInfo, type: WKMediaCaptureType, decisionHandler: @escaping @MainActor @Sendable (WKPermissionDecision) -> Void) { decisionHandler(.deny) }
        func webView(_ webView: WKWebView, requestDeviceOrientationAndMotionPermissionFor origin: WKSecurityOrigin, initiatedByFrame frame: WKFrameInfo, decisionHandler: @escaping @MainActor @Sendable (WKPermissionDecision) -> Void) { decisionHandler(.deny) }
        @available(iOS 27.0, *)
        func webView(_ webView: WKWebView, requestGeolocationPermissionFor origin: WKSecurityOrigin, initiatedByFrame frame: WKFrameInfo, decisionHandler: @escaping @MainActor @Sendable (WKPermissionDecision) -> Void) { decisionHandler(.deny) }
    }
}
