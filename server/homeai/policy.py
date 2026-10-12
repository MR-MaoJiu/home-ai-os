import httpx
from fastapi import HTTPException

# 风险完全由核心注册表定义，Provider 不得通过 Manifest 降级。
CAPABILITIES = {
    "voice.call@v1": (2, True),
    "android.launch@v1": (2, True), "android.observe@v1": (1, False), "android.action@v1": (3, True),
    "host.inspect@v1": (3, False),
    "shell.run@v1": (2, True), "host.execute@v1": (3, True),
    "client.request@v1": (2, True), "member.read@v1": (2, True), "member.notify@v1": (3, True),
    "presentation.render@v1": (1, False),
    "resource.send@v1": (1, False), "resource.fetch@v1": (1, False), "resource.script@v1": (1, False),
    "image.generate@v1": (2, True), "image.edit@v1": (2, True),
    "automation.list@v1": (1, False),
    "automation.create@v1": (2, True), "automation.stop@v1": (2, True),
    "knowledge.search@v1": (1, False),
    "memory.search@v1": (1, False), "memory.commit@v1": (2, True),
    "model.generate@v1": (1, False), "model.embed@v1": (1, False), "model.rerank@v1": (1, False),
    "document.parse@v1": (1, False), "speech.transcribe@v1": (1, False), "speech.synthesize@v1": (1, False),
    "speech.voices@v1": (1, False),
    "memory.semantic.search@v1": (1, False), "memory.semantic.index@v1": (2, True), "memory.semantic.purge@v1": (2, True),
    "memory.graph.search@v1": (1, False), "memory.graph.index@v1": (2, True), "memory.graph.purge@v1": (2, True),
    "home.states@v1": (1, False), "home.execute@v1": (3, True),
    "calendar.search@v1": (1, False), "calendar.create@v1": (2, True), "reminder.create@v1": (2, True),
    "mail.search@v1": (1, False), "mail.read@v1": (1, False), "mail.send@v1": (3, True),
    "web.search@v1": (1, False), "photo.analyze@v1": (1, False),
}


def capability_risk(capability: str, arguments=None, *, assessment=None) -> int:
    """按 Core 已校验的范围细分风险；客户端和模型不能提交风险等级。"""
    if capability not in CAPABILITIES:
        raise HTTPException(403, "未注册的能力")
    if capability == 'android.action@v1' and isinstance(arguments, dict):
        from .android_action_guard import ActionAssessment
        if isinstance(assessment, ActionAssessment) and assessment.permits(arguments):
            return 2
        from .android_tools import AndroidAction
        try:
            action = AndroidAction.model_validate(arguments)
            # 未适配 App 的滑动可能删除条目或提交订单，返回也可能放弃有影响的流程。
            if action.input.action in {'home', 'launch'}:
                return 2
        except ValueError:
            pass
    if capability == 'host.inspect@v1' and isinstance(arguments, dict):
        # 仅固定性能采集器无需确认；缺参、异常参数与截屏沿用保守等级。
        from .host_inspection_tools import HostInspectionRequest
        try:
            request = HostInspectionRequest.model_validate({key: value for key, value in arguments.items()
                if key not in {'execution', 'approval_context'}})
            if set(request.targets) <= {'cpu', 'memory'}:
                return 1
        except ValueError:
            pass
    return CAPABILITIES[capability][0]


class Policy:
    def __init__(self, url: str, transport=None):
        self.url, self.transport = url, transport

    async def check(self, actor, capability: str, approved: bool = False, *, arguments=None, assessment=None):
        if assessment is not None:
            from .android_action_guard import ActionAssessment
            if capability != 'android.action@v1' or not isinstance(assessment, ActionAssessment) or not assessment.permits(arguments, actor):
                raise HTTPException(403, '安卓动作核验证据与执行身份不匹配')
        risk = capability_risk(capability, arguments, assessment=assessment)
        if risk >= 4 or (risk >= 3 and not approved):
            raise HTTPException(403, "该操作需要有效审批")
        try:
            async with httpx.AsyncClient(timeout=5, transport=self.transport, trust_env=False) as client:
                r = await client.post(self.url + "/v1/data/homeai/allow", json={"input": {"actor": actor.__dict__, "capability": capability, "risk": risk, "approved": approved}})
                r.raise_for_status()
                allowed = r.json().get("result") is True
        except Exception:
            raise HTTPException(503, "策略服务不可用，操作已阻止") from None
        if not allowed:
            raise HTTPException(403, "策略拒绝执行")
