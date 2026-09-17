"""按授权实体订阅 Home Assistant；重连重新快照，不冒充完整历史重放。"""
import asyncio
import json
from urllib.parse import urlsplit, urlunsplit
import httpx
from websockets.asyncio.client import connect
from fastapi import HTTPException
from .home_control import project_state


async def events(manifest, token):
    if not manifest.home_entities or urlsplit(manifest.endpoint).hostname not in manifest.allowed_hosts:
        raise HTTPException(403, '家居事件没有有效实体或网络授权')
    parts = urlsplit(manifest.endpoint)
    endpoint = urlunsplit(('wss' if parts.scheme == 'https' else 'ws', parts.netloc, parts.path + '/api/websocket', '', ''))
    async with connect(endpoint, proxy=None, open_timeout=10, close_timeout=3, max_size=512 * 1024, max_queue=64, ping_interval=20, ping_timeout=20) as socket:
        async with asyncio.timeout(10):
            if json.loads(await socket.recv()).get('type') != 'auth_required':
                raise HTTPException(502, '家居事件握手无效')
            await socket.send(json.dumps({'type': 'auth', 'access_token': token}))
            if json.loads(await socket.recv()).get('type') != 'auth_ok':
                raise HTTPException(401, '家居事件认证失败')
            await socket.send(json.dumps({'id': 1, 'type': 'subscribe_trigger', 'trigger': {'platform': 'state', 'entity_id': manifest.home_entities}}))
            acknowledgement = json.loads(await socket.recv())
            if acknowledgement.get('id') != 1 or acknowledgement.get('type') != 'result' or acknowledgement.get('success') is not True:
                raise HTTPException(502, '家居事件订阅被拒绝')
        # 先订阅再取快照，期间事件由有界 WebSocket 队列保留。
        async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False, headers={'Authorization': 'Bearer ' + token}) as client:
            async def read(entity):
                response = await client.get(manifest.endpoint + "/api/states/" + entity)
                if response.status_code == 404:
                    return {'entity_id': entity, 'missing': True}
                response.raise_for_status()
                if len(response.content) > 256 * 1024:
                    raise HTTPException(502, '家居状态响应过大')
                return project_state(response.json(), {entity})
            states = [await read(entity) for entity in manifest.home_entities]
            yield {'type': 'snapshot', 'states': states}
            async for raw in socket:
                message = json.loads(raw)
                if message.get('type') != 'event' or message.get('id') != 1:
                    continue
                trigger = message.get('event', {}).get('variables', {}).get('trigger', {})
                entity = trigger.get('entity_id')
                if entity not in manifest.home_entities:
                    raise HTTPException(502, '家居订阅返回未授权实体')
                # 事件是失效通知；回读当前状态，避免积压旧事件覆盖重连后的新快照。
                yield {'type': 'change', 'states': [await read(entity)]}
