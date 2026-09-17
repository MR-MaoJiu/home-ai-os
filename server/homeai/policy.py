import httpx
from fastapi import HTTPException

# 风险完全由核心注册表定义，Provider 不得通过 Manifest 降级。
CAPABILITIES = {
    "knowledge.search@v1": (1, False),
    "memory.search@v1": (1, False), "memory.commit@v1": (2, True),
    "model.generate@v1": (1, False), "model.embed@v1": (1, False), "model.rerank@v1": (1, False),
    "document.parse@v1": (1, False), "speech.transcribe@v1": (1, False), "speech.synthesize@v1": (1, False),
    "memory.semantic.search@v1": (1, False), "memory.semantic.index@v1": (2, True), "memory.semantic.purge@v1": (2, True),
    "memory.graph.search@v1": (1, False), "memory.graph.index@v1": (2, True), "memory.graph.purge@v1": (2, True),
    "home.states@v1": (1, False), "home.execute@v1": (3, True),
    "calendar.search@v1": (1, False), "calendar.create@v1": (2, True), "reminder.create@v1": (2, True),
    "mail.search@v1": (1, False), "mail.read@v1": (1, False), "mail.send@v1": (3, True),
    "web.search@v1": (3, False), "photo.analyze@v1": (1, False),
}


class Policy:
    def __init__(self, url: str, transport=None):
        self.url, self.transport = url, transport

    async def check(self, actor, capability: str, approved: bool = False):
        if capability not in CAPABILITIES:
            raise HTTPException(403, "未注册的能力")
        risk, effect = CAPABILITIES[capability]
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
