import json
from urllib.parse import urlparse
import httpx
from fastapi import HTTPException
from sqlalchemy import select
from .contracts import ProviderManifest
from .db import Provider, Secret


class Registry:
    def __init__(self, vault, transport=None):
        self.vault, self.transport = vault, transport

    def resolve(self, db, capability, cloud=False):
        matches = []
        for row in db.scalars(select(Provider).where(Provider.enabled.is_(True)).order_by(Provider.id)):
            manifest = ProviderManifest.model_validate_json(row.manifest)
            if capability in manifest.capabilities and manifest.cloud == cloud:
                matches.append(manifest)
        if not matches:
            raise HTTPException(503, f"能力尚未配置可用 Provider：{capability}")
        return matches[0]

    async def invoke(self, db, actor, manifest, capability, arguments, invocation_id):
        if capability not in manifest.capabilities:
            raise HTTPException(403, "能力未映射")
        host = urlparse(manifest.endpoint).hostname
        if host not in manifest.allowed_hosts:
            raise HTTPException(403, "端点不在网络许可清单")
        if manifest.cloud and urlparse(manifest.endpoint).scheme != "https":
            raise HTTPException(403, "云端必须使用 HTTPS")
        headers = {"Idempotency-Key": invocation_id}
        if manifest.secret_id:
            secret = db.get(Secret, manifest.secret_id)
            if not secret or secret.owner_id != actor.user_id or secret.provider_id != manifest.id:
                raise HTTPException(403, "Provider 无权使用此凭据")
            headers["Authorization"] = "Bearer " + self.vault.open(secret.value, actor.user_id + ":secret:" + secret.id)
        async with httpx.AsyncClient(timeout=manifest.timeout_seconds, transport=self.transport, follow_redirects=False, trust_env=False, headers=headers) as client:
            mapped = manifest.capabilities[capability]
            if manifest.adapter == "openai":
                if capability in {"model.generate@v1", "photo.analyze@v1"}:
                    path = "/chat/completions"
                    payload = {"model": manifest.model, "messages": arguments["messages"], "max_tokens": min(arguments.get("max_tokens", 1024), 4096)}
                    if arguments.get("tools"):
                        payload["tools"] = arguments["tools"]
                        payload["tool_choice"] = "auto"
                elif capability == "model.embed@v1":
                    path, payload = "/embeddings", {"model": manifest.model, "input": arguments["input"]}
                else:
                    path, payload = "/rerank", {"model": manifest.model, "query": arguments["query"], "documents": arguments["documents"]}
                r = await client.post(manifest.endpoint + path, json=payload, headers=headers)
            elif manifest.adapter == "homeassistant":
                if capability == "home.states@v1":
                    r = await client.get(manifest.endpoint + "/api/states", headers=headers)
                else:
                    domain, service = arguments.get("domain"), arguments.get("service")
                    if (domain, service) not in {(d, s) for d in ("light", "switch", "climate") for s in ("turn_on", "turn_off")} | {("climate", "set_temperature")}:
                        raise HTTPException(403, "未授权的家居操作")
                    entity = arguments.get("entity_id", "")
                    if not entity.startswith(domain + ".") or "/" in entity:
                        raise HTTPException(422, "设备标识无效")
                    payload = {"entity_id": entity}
                    if service == "set_temperature":
                        temperature = float(arguments["temperature"])
                        if not 16 <= temperature <= 30:
                            raise HTTPException(422, "温度超出允许范围")
                        payload["temperature"] = temperature
                    r = await client.post(manifest.endpoint + f"/api/services/{domain}/{service}", json=payload, headers=headers)
            elif manifest.adapter == "searxng":
                r = await client.get(manifest.endpoint + "/search", params={"q": arguments["query"], "format": "json"}, headers=headers)
            elif manifest.adapter == "mcp":
                # 官方 SDK 处理初始化、会话和流式 HTTP；工具名必须来自显式映射。
                from mcp import ClientSession
                from mcp.client.streamable_http import streamable_http_client
                async with streamable_http_client(manifest.endpoint, http_client=client) as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
                        await session.initialize()
                        result = await session.call_tool(mapped, arguments)
                        if result.isError:
                            raise HTTPException(502, "MCP 工具执行失败")
                        return result.model_dump(mode="json")
            else:
                if not mapped.startswith("/") or ".." in mapped or "://" in mapped:
                    raise HTTPException(422, "非法 Provider 路径")
                r = await client.post(manifest.endpoint + mapped, json={"arguments": arguments, "invocation_id": invocation_id, "subject_id": actor.user_id}, headers=headers)
            r.raise_for_status()
            if len(r.content) > 4 * 1024 * 1024:
                raise HTTPException(502, "Provider 返回过大")
            return r.json()
