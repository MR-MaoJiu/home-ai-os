import json
from urllib.parse import urlparse
import httpx
from fastapi import HTTPException
from sqlalchemy import select
from .contracts import ProviderManifest
from .db import Provider, Secret


def ensure_local_model_endpoint(manifest):
    """本地模型不能只凭网页提交的cloud=false把公网接口伪装成本地检测器。"""
    import ipaddress
    host=urlparse(manifest.endpoint).hostname
    if host=='localhost':return
    try:address=ipaddress.ip_address(host)
    except ValueError:raise HTTPException(403,'本地模型请使用 localhost 或明确的局域网 IP；公网模型必须标记为云端') from None
    networks=('127.0.0.0/8','10.0.0.0/8','172.16.0.0/12','192.168.0.0/16','::1/128','fc00::/7')
    if not any(address in ipaddress.ip_network(network) for network in networks):
        raise HTTPException(403,'公网模型不能声明为本地模型')


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
                        from .model_routing import shared_key
                        from .security import Actor
                        actor = Actor(db.info.get('user_id',''),db.info.get('household_id',''),'model-resolution','service')
                        if capability != 'model.generate@v1' or not shared_key(self.vault,db,actor,manifest):
                            continue
                matches.append(manifest)
        if not matches:
            raise HTTPException(503, f"能力尚未配置可用 Provider：{capability}")
        return matches[0]

    async def invoke(self, db, actor, manifest, capability, arguments, invocation_id, *, cloud_permit=None):
        if capability not in manifest.capabilities:
            raise HTTPException(403, "能力未映射")
        if not manifest.cloud and capability.startswith(('model.','photo.analyze')) and self.transport is None:
            # 注入的传输用于离线契约测试；实际网络出口必须使用明确的本地地址。
            ensure_local_model_endpoint(manifest)
        host = urlparse(manifest.endpoint).hostname
        if host not in manifest.allowed_hosts:
            raise HTTPException(403, "端点不在网络许可清单")
        if manifest.cloud and urlparse(manifest.endpoint).scheme != "https":
            raise HTTPException(403, "云端必须使用 HTTPS")
        if manifest.cloud:
            from .cloud_gateway import check_permit
            check_permit(cloud_permit, actor, manifest, arguments)
        headers = {"Idempotency-Key": invocation_id}
        if manifest.secret_id:
            secret = db.get(Secret, manifest.secret_id)
            if secret and secret.owner_id == actor.user_id and secret.provider_id == manifest.id:
                key = self.vault.open(secret.value, actor.user_id + ":secret:" + secret.id)
            else:
                from .model_routing import shared_key
                key = shared_key(self.vault, db, actor, manifest) if capability == 'model.generate@v1' else None
                if not key:
                    raise HTTPException(403, "Provider 无权使用此凭据")
            headers["Authorization"] = "Bearer " + key
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
                    if 'response_format' in arguments:
                        payload['response_format'] = arguments['response_format']
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
                from .search_results import unavailable,normalize
                try:
                    r = await client.post(manifest.endpoint.rstrip('/') + "/search", data={"q": query, "format": "json", "safesearch": "2"}, headers=headers)
                except httpx.TransportError as exc:
                    return unavailable('timeout' if isinstance(exc,httpx.TimeoutException) else 'network_error')
                if not r.is_success:return unavailable('http_'+str(r.status_code))
                if len(r.content)>4*1024*1024:return unavailable('response_too_large')
                try:return normalize(r.json())
                except ValueError:return unavailable('invalid_json')
            elif manifest.adapter == "mcp":
                # 官方 SDK 处理初始化、会话和流式 HTTP；工具名必须来自显式映射。
                from mcp import ClientSession
                from mcp.client.streamable_http import streamable_http_client
                from homeai_providers.mcp_contract import protocol_errors
                async with protocol_errors(), streamable_http_client(manifest.endpoint, http_client=client) as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
                        await session.initialize()
                        from homeai_providers.mcp_contract import checked_call
                        return await checked_call(session, mapped, arguments, manifest.mcp_catalog_sha256, structured=True)
            else:
                if not mapped.startswith("/") or ".." in mapped or "://" in mapped:
                    raise HTTPException(422, "非法 Provider 路径")
                r = await client.post(manifest.endpoint + mapped, json={"arguments": arguments, "invocation_id": invocation_id, "subject_id": actor.user_id}, headers=headers)
            r.raise_for_status()
            if len(r.content) > 4 * 1024 * 1024:
                raise HTTPException(502, "Provider 返回过大")
            result = r.json()
            return result
