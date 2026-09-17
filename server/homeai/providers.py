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
        for row in db.scalars(select(Provider).where(Provider.enabled.is_(True)).order_by(Provider.id).execution_options(populate_existing=True)):
            manifest = ProviderManifest.model_validate_json(row.manifest)
            if capability in manifest.capabilities and manifest.cloud == cloud:
                if manifest.secret_id:
                    secret = db.get(Secret, manifest.secret_id)
                    if not secret or secret.owner_id != db.info.get("user_id") or secret.provider_id != manifest.id:
                        continue
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
                    if "temperature" in arguments:
                        temperature = arguments["temperature"]
                        if type(temperature) not in (int, float) or not 0 <= temperature <= 2:
                            raise HTTPException(422, "采样温度无效")
                        payload["temperature"] = temperature
                    if arguments.get("tools"):
                        payload["tools"] = arguments["tools"]
                        payload["tool_choice"] = "auto"
                elif capability == "model.embed@v1":
                    path, payload = "/embeddings", {"model": manifest.model, "input": arguments["input"]}
                else:
                    path, payload = "/rerank", {"model": manifest.model, "query": arguments["query"], "documents": arguments["documents"]}
                r = await client.post(manifest.endpoint + path, json=payload, headers=headers)
            elif manifest.adapter == "homeassistant":
                from .home_control import validate, project_state
                validated = validate(manifest, capability, arguments)
                if capability == "home.states@v1":
                    states = []
                    for entity in validated:
                        response = await client.get(manifest.endpoint + "/api/states/" + entity)
                        response.raise_for_status()
                        if len(response.content) > 256 * 1024:
                            raise HTTPException(502, "家居状态返回过大")
                        states.append(project_state(response.json(), {entity}))
                    return states
                domain, service, payload = validated
                response = await client.post(manifest.endpoint + f"/api/services/{domain}/{service}", json=payload)
                response.raise_for_status()
                if len(response.content) > 256 * 1024 or not isinstance(response.json(), list):
                    raise HTTPException(502, "家居操作响应无效")
                # 服务调用可能返回其他联动实体；不向模型披露未授权结果。
                return {'status': 'service_completed', 'entity_id': payload['entity_id'],
                    'states': [project_state(item, set(manifest.home_entities)) for item in response.json()
                               if isinstance(item, dict) and item.get('entity_id') in manifest.home_entities]}
            elif manifest.adapter == "searxng":
                from .privacy import validate_search
                query = validate_search(arguments)['query']
                # 查询使用请求体，避免进入本机 URL 访问日志；上游仍会收到查询词。
                r = await client.post(manifest.endpoint.rstrip('/') + "/search", data={"q": query, "format": "json", "safesearch": "2"}, headers=headers)
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
            result = r.json()
            if manifest.adapter == "searxng":
                if not isinstance(result, dict) or not isinstance(result.get('results'), list):
                    raise HTTPException(502, "搜索服务返回结构无效")
                entries = []
                for item in result['results'][:50]:
                    if not isinstance(item, dict):
                        continue
                    url = item.get('url')
                    parsed = urlparse(url) if isinstance(url, str) else None
                    if not parsed or parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username is not None or parsed.password is not None or len(url) > 4000:
                        continue
                    entries.append({'title': str(item.get('title', ''))[:500], 'url': url[:4000],
                        'content': str(item.get('content', ''))[:2000], 'engine': str(item.get('engine', ''))[:100]})
                failed = result.get('unresponsive_engines', [])
                failures = [{'engine': str(item[0])[:100], 'reason': str(item[1])[:200]} for item in failed if isinstance(item, list) and len(item) >= 2]
                if failures and not entries:
                    raise HTTPException(503, '搜索未返回可用结果且上游引擎存在故障')
                return {'results': entries[:10], 'unresponsive_engines': failures,
                        'status': 'partial' if entries and failures else 'ok',
                        'content_trust': 'untrusted_web'}
            return result
