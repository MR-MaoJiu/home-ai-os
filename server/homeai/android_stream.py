"""安卓画面的独立纯 P2P 视频会话；控制操作仍走签名 Core API。"""
import asyncio
import ipaddress
import json
import time
import uuid
import logging
from aiortc import RTCPeerConnection, RTCConfiguration, RTCIceServer, RTCSessionDescription, RTCRtpSender
from aiortc.sdp import SessionDescription
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field
from typing import Literal
from .contracts import Contract
from .db import Device
from .security import authenticate
from .android_stream_media import LiveMedia, ScreenTrack, SystemAudioTrack

router = APIRouter(prefix='/api/v1/android/stream', tags=['安卓屏幕直连'])
log = logging.getLogger('homeai.android_stream')
TOUCH_HEARTBEAT_INTERVAL = 0.4
TOUCH_HEARTBEAT_TTL = 1.5


class StreamOffer(Contract):
    type: Literal['offer']
    sdp: str = Field(min_length=1, max_length=65536)
    audio_enabled: bool = False


class DisplayRequest(Contract):
    screen_off: bool
    expected_epoch: int = Field(ge=1)


def validate_screen_sdp(sdp, audio_enabled=False):
    """只接收屏幕与可选系统声音；拒绝客户端媒体上行、TURN或明文媒体。"""
    try:
        description = SessionDescription.parse(sdp)
        expected_kinds = ['application', 'audio', 'video'] if audio_enabled else ['application', 'video']
        if sorted(m.kind for m in description.media) != expected_kinds:
            raise ValueError()
        for media in description.media:
            expected = 'UDP/DTLS/SCTP' if media.kind == 'application' else 'UDP/TLS/RTP/SAVPF'
            if media.profile != expected or not media.dtls or not media.dtls.fingerprints:
                raise ValueError()
            if not media.ice_candidates_complete or not 1 <= len(media.ice_candidates) <= 32:
                raise ValueError()
            for candidate in media.ice_candidates:
                address = ipaddress.ip_address(candidate.ip)
                if candidate.type not in {'host', 'srflx'} or candidate.protocol.lower() != 'udp' or candidate.component != 1 or address.is_multicast or address.is_unspecified or not 1 <= candidate.port <= 65535:
                    raise ValueError()
        if any(m.direction != 'recvonly' for m in description.media if m.kind in ('video', 'audio')):
            raise ValueError()
    except Exception:
        raise HTTPException(422, '屏幕连接必须是完整的加密纯直连视频接收会话') from None


def stun_urls(app):
    from .remote import read_config
    from .connect_signalling import effective_stun
    config = read_config(app)
    return effective_stun(app, config) if config and config.get('enabled') else app.settings.direct_stun_urls


class ScreenSessions:
    def __init__(self, app):
        self.app = app
        self.entries = {}
        self.lock = asyncio.Lock()

    def send_metadata(self, identifier, metadata):
        entry = self.entries.get(identifier)
        channel = entry.get('channel') if entry else None
        if channel and channel.readyState == 'open' and channel.bufferedAmount < 65536:
            channel.send(json.dumps(metadata, ensure_ascii=False))

    async def negotiate(self, actor, offer):
        from .android_workspace import get_binding
        with self.app.db() as db:
            device = db.get(Device, actor.device_id)
            if not device or device.revoked or device.user_id != actor.user_id:
                raise HTTPException(403, '只允许成员已配对的客户端观看安卓画面')
            if get_binding(self.app, db, actor) is None:
                raise HTTPException(404, '尚未绑定个人 Android 执行设备')
        validate_screen_sdp(offer.sdp, offer.audio_enabled)
        description = SessionDescription.parse(offer.sdp)
        log.warning('安卓视频协商：%s', [(m.kind, m.direction, len(m.ice_candidates), sorted({c.type for c in m.ice_candidates})) for m in description.media])
        async with self.lock:
            old = [key for key, item in self.entries.items() if item['device_id'] == actor.device_id]
            for key in old:
                await self.close_session(key, reason='replaced_by_same_device')
            if len(self.entries) >= 8:
                raise HTTPException(429, '屏幕会话达到并发上限')
            identifier = str(uuid.uuid4())
            pc = RTCPeerConnection(RTCConfiguration(iceServers=[RTCIceServer(urls=url) for url in stun_urls(self.app)]))
            self.entries[identifier] = {'peer': pc, 'device_id': actor.device_id, 'owner_id': actor.user_id,
                'channel': None, 'touch_channel': None, 'gestures': None, 'touch_reader': None,
                'touch_warm_task': None, 'touch_ready': False, 'touch_generation': None, 'touch_queue': None,
                'touch_revision': 0, 'touch_reset_task': None, 'touch_heartbeat_task': None,
                'touch_live_until': 0.0, 'touch_initial_until': 0.0, 'touch_challenges': {}, 'touch_last_ping': 0.0,
                'touch_reason': None, 'touch_close_task': None, 'touch_closing': False,
                'created_at': time.monotonic(), 'track': None, 'audio_track': None, 'media': None, 'timer': None}
        @pc.on('datachannel')
        def channel_ready(channel):
            entry = self.entries.get(identifier)
            if entry and channel.label == 'homeai.android.touch' and entry.get('touch_channel') is not None:
                previous = entry['touch_channel']
                if entry.get('touch_closing') or previous.readyState in {'closing', 'closed'}:
                    if entry.get('touch_pending_channel'):
                        channel.close()
                        return
                    entry['touch_pending_channel'] = channel
                    async def replace_after_cleanup():
                        try:
                            await self.close_touch(identifier, '正在恢复触控通道')
                            if (self.entries.get(identifier) is entry and entry.get('touch_pending_channel') is channel
                                    and channel.readyState != 'closed'):
                                entry['touch_pending_channel'] = None
                                channel_ready(channel)
                            else:
                                channel.close()
                        finally:
                            if entry.get('touch_pending_channel') is channel:
                                entry['touch_pending_channel'] = None
                    entry['touch_replace_task'] = asyncio.create_task(replace_after_cleanup())
                    return
            if entry and channel.label == 'homeai.android.touch' and entry['touch_channel'] is None:
                from .android_gestures import GestureSession
                from .android_touch_bridge import create_bridge
                queue = asyncio.Queue(maxsize=32)
                def acknowledge(value):
                    if channel.readyState == 'open' and channel.bufferedAmount < 65536:
                        channel.send(json.dumps(value, ensure_ascii=False))
                def cancelled(value):
                    # 先同步废除代次和排队输入，再异步释放／恢复；不能依赖不存在的 ICE disconnected。
                    self.reset_touch(identifier, value.get('reason') or '触控状态已变化')
                    acknowledge(value)
                handler = GestureSession(self.app, actor, create_bridge, on_cancel=cancelled)
                entry['touch_channel'], entry['gestures'] = channel, handler
                entry['touch_closing'], entry['touch_reason'], entry['touch_close_task'] = False, None, None
                entry['touch_queue'] = queue
                async def read_touch():
                    try:
                        while True:
                            payload = await queue.get()
                            try:
                                packet = json.loads(payload)
                                if not isinstance(packet, dict):
                                    raise HTTPException(422, '触控消息格式无效')
                                channel_id = packet.pop('channel_id', None)
                                if not entry['touch_ready'] or channel_id != entry['touch_generation']:
                                    acknowledge({'status':'rejected', 'gesture_id':packet.get('gesture_id'),
                                        'sequence':packet.get('sequence'), 'reason':'触控连接已更新，旧事件已丢弃'})
                                    continue
                                if time.monotonic() >= entry['touch_live_until']:
                                    acknowledge({'status':'rejected', 'gesture_id':packet.get('gesture_id'),
                                        'sequence':packet.get('sequence'), 'reason':'触控连接尚未确认或已失活，旧事件已丢弃'})
                                    if time.monotonic() >= entry['touch_initial_until']:
                                        self.reset_touch(identifier, '触控连接心跳已失活')
                                    continue
                                result = await handler.handle(packet)
                                acknowledge(result)
                            except Exception as exc:
                                # 只回显有效关联标识；不把触点、输入内容或异常栈写入日志。
                                try:
                                    packet = json.loads(payload)
                                    gid, sequence = packet.get('gesture_id'), packet.get('sequence')
                                    uuid.UUID(gid)
                                    if type(sequence) is not int: raise ValueError()
                                except Exception:
                                    gid, sequence = None, None
                                acknowledge({'status':'rejected', 'gesture_id':gid, 'sequence':sequence,
                                    'reason':str(exc.detail) if isinstance(exc, HTTPException) else '触控输入未完成，请重新开始手势'})
                                if isinstance(exc, HTTPException) and exc.status_code in {409, 502, 503, 504}:
                                    self.reset_touch(identifier, '触控状态已变化，请重新开始手势')
                                    continue
                                await handler.close()
                                channel.close()
                                return
                    finally:
                        await handler.close()
                entry['touch_reader'] = asyncio.create_task(read_touch())
                entry['touch_heartbeat_task'] = asyncio.create_task(self.touch_heartbeat(identifier))
                self.reset_touch(identifier, '触控通道初始化')
                @channel.on('message')
                def touch_message(payload):
                    if not isinstance(payload, (str, bytes)) or len(payload) > 8192 or queue.full():
                        asyncio.create_task(self.close_touch(identifier, '触控数据无效或积压，画面保持只读')); return
                    try:
                        message = json.loads(payload)
                    except (ValueError, UnicodeError):
                        asyncio.create_task(self.close_touch(identifier, '触控数据格式无效，画面保持只读')); return
                    if isinstance(message, dict) and message.get('type') == 'heartbeat':
                        self.confirm_touch_heartbeat(identifier, message)
                        return
                    queue.put_nowait(payload)
                @channel.on('close')
                def touch_closed():
                    asyncio.create_task(self.close_touch(identifier, '触控通道已关闭，画面保持只读'))
                return
            if not entry or channel.label != 'homeai.android.screen' or entry['channel'] is not None:
                channel.close(); return
            entry['channel'] = channel
            # 画面通道无执行入口，恶意数据消息不会成为命令。
            @channel.on('message')
            def unexpected(_):
                asyncio.create_task(self.close_session(identifier, reason='metadata_protocol_violation'))
        @pc.on('connectionstatechange')
        async def state_changed():
            log.warning('安卓视频连接状态：%s', pc.connectionState)
            if pc.connectionState in {'failed', 'closed'}:
                await self.close_session(identifier, reason='peer_' + pc.connectionState)
            elif pc.connectionState == 'connecting':
                entry = self.entries.get(identifier)
                if entry and entry.get('touch_ready'):
                    self.reset_touch(identifier, '屏幕连接正在重新协商')
            elif pc.connectionState == 'connected':
                entry = self.entries.get(identifier)
                if entry and entry.get('touch_channel'):
                    await self.warm_touch(identifier)
        try:
            async with asyncio.timeout(25):
                transceiver = pc.addTransceiver('video', direction='sendonly')
                codecs = [c for c in RTCRtpSender.getCapabilities('video').codecs if c.mimeType.lower() == 'video/h264']
                transceiver.setCodecPreferences(codecs)
                if offer.audio_enabled:
                    audio_transceiver = pc.addTransceiver('audio', direction='sendonly')
                    audio_transceiver.setCodecPreferences([c for c in RTCRtpSender.getCapabilities('audio').codecs
                        if c.mimeType.lower() == 'audio/opus'])
                await pc.setRemoteDescription(RTCSessionDescription(sdp=offer.sdp, type='offer'))
                media = LiveMedia(self, identifier, actor, offer.audio_enabled)
                self.entries[identifier]['media'] = media
                track = ScreenTrack(media)
                self.entries[identifier]['track'] = track
                pc.addTrack(track)
                if offer.audio_enabled:
                    audio_track = SystemAudioTrack(media)
                    self.entries[identifier]['audio_track'] = audio_track
                    pc.addTrack(audio_track)
                await pc.setLocalDescription(await pc.createAnswer())
            async def expire():
                await asyncio.sleep(30)
                if pc.connectionState != 'connected':
                    await self.close_session(identifier, reason='establishment_timeout')
            self.entries[identifier]['timer'] = asyncio.create_task(expire())
            return {'session_id': identifier, 'type': 'answer', 'sdp': pc.localDescription.sdp}
        except BaseException:
            await self.close_session(identifier, reason='negotiation_failed')
            raise

    async def close_touch(self, identifier, reason):
        """触控不可用只释放输入资源；观看会话、音频和任务继续运行。"""
        entry = self.entries.get(identifier)
        if not entry:
            return
        existing = entry.get('touch_close_task')
        if existing:
            if existing is not asyncio.current_task():
                await asyncio.shield(existing)
            return
        async def cleanup():
            entry['touch_closing'] = True
            entry['touch_ready'], entry['touch_generation'] = False, None
            entry['touch_reason'] = reason
            entry['touch_live_until'], entry['touch_initial_until'] = 0.0, 0.0
            entry['touch_revision'] += 1
            entry['touch_challenges'].clear()
            for name in ('touch_heartbeat_task', 'touch_reset_task', 'touch_warm_task', 'touch_reader'):
                task = entry.get(name)
                if task and task is not asyncio.current_task():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            if entry.get('gestures'):
                await entry['gestures'].close()
            media = entry.get('media')
            if media and media.display_guard:
                await media.display_guard.restore_for(identifier)
            channel = entry.get('touch_channel')
            if channel and channel.readyState != 'closed':
                channel.close()
            entry['touch_channel'] = None
        entry['touch_close_task'] = asyncio.create_task(cleanup())
        await asyncio.shield(entry['touch_close_task'])

    def _touch_send(self, entry, value):
        channel = entry.get('touch_channel')
        if channel and channel.readyState == 'open' and channel.bufferedAmount < 65536:
            channel.send(json.dumps(value, ensure_ascii=False))
            return True
        return False

    def reset_touch(self, identifier, reason):
        """同步废除旧代次，异步取消当前手势；恢复只发新许可，绝不重放消息。"""
        entry = self.entries.get(identifier)
        if not entry or not entry.get('gestures') or entry.get('touch_closing'):
            return None
        entry['touch_ready'], entry['touch_generation'] = False, None
        entry['touch_revision'] += 1
        entry['touch_live_until'], entry['touch_initial_until'] = 0.0, 0.0
        entry['touch_challenges'].clear()
        media = entry.get('media')
        if media and getattr(media, 'display_guard', None):
            asyncio.create_task(media.display_guard.restore_for(identifier))
        queue = entry.get('touch_queue')
        if queue:
            while not queue.empty():
                queue.get_nowait()
        task = entry.get('touch_reset_task')
        if task and not task.done():
            return task
        async def recover():
            try:
                await entry['gestures'].cancel(reason)
                previous = entry.get('touch_warm_task')
                if previous:
                    await asyncio.gather(previous, return_exceptions=True)
                    if entry.get('touch_warm_task') is previous:
                        entry['touch_warm_task'] = None
                if self.entries.get(identifier) is entry:
                    return await self.warm_touch(identifier)
                return False
            finally:
                if entry.get('touch_reset_task') is asyncio.current_task():
                    entry['touch_reset_task'] = None
        task = asyncio.create_task(recover())
        entry['touch_reset_task'] = task
        return task

    def _ping_touch(self, entry):
        if not entry['touch_ready']:
            return
        clock = time.monotonic()
        nonce = uuid.uuid4().hex
        entry['touch_challenges'] = {key: value for key, value in entry['touch_challenges'].items() if value > clock}
        entry['touch_challenges'][nonce] = clock + TOUCH_HEARTBEAT_TTL
        entry['touch_last_ping'] = clock
        self._touch_send(entry, {'status': 'ping', 'channel_id': entry['touch_generation'], 'nonce': nonce})

    def confirm_touch_heartbeat(self, identifier, message):
        entry = self.entries.get(identifier)
        if (not entry or not entry['touch_ready'] or entry['peer'].connectionState != 'connected'
                or set(message) != {'type', 'channel_id', 'nonce'}
                or message.get('channel_id') != entry['touch_generation']):
            return False
        clock = time.monotonic()
        if (entry['touch_live_until'] or entry['touch_initial_until']) <= clock:
            self.reset_touch(identifier, '触控连接心跳已过期，旧响应不能续租')
            return False
        nonce = message.get('nonce')
        if not isinstance(nonce, str):
            return False
        deadline = entry['touch_challenges'].pop(nonce, 0.0)
        if deadline <= time.monotonic():
            return False
        # 以服务端发起挑战的时刻计期限；迟到的 pong 不能按到达时刻重新获得 1.5 秒。
        entry['touch_live_until'] = max(entry['touch_live_until'], deadline)
        self._touch_send(entry, {'status': 'alive', 'channel_id': entry['touch_generation'], 'nonce': nonce})
        return True

    async def touch_heartbeat(self, identifier):
        try:
            while entry := self.entries.get(identifier):
                await asyncio.sleep(0.1)
                if not entry['touch_ready']:
                    continue
                clock = time.monotonic()
                deadline = entry['touch_live_until'] or entry['touch_initial_until']
                if deadline <= clock:
                    self.reset_touch(identifier, '触控连接心跳超时，已释放触点')
                elif clock - entry['touch_last_ping'] >= TOUCH_HEARTBEAT_INTERVAL:
                    self._ping_touch(entry)
        except asyncio.CancelledError:
            return

    async def warm_touch(self, identifier):
        entry = self.entries.get(identifier)
        if not entry or not entry.get('gestures') or entry.get('touch_closing'):
            return False
        if entry['touch_ready']:
            return True
        task = entry.get('touch_warm_task')
        if task:
            return await asyncio.shield(task)
        revision = entry['touch_revision']
        async def warm():
            try:
                await entry['gestures'].warm()
                if (self.entries.get(identifier) is not entry or entry['peer'].connectionState != 'connected'
                        or entry['touch_revision'] != revision):
                    return False
                channel = entry['touch_channel']
                if channel.readyState != 'open':
                    return False
                entry['touch_generation'] = uuid.uuid4().hex
                entry['touch_ready'] = True
                entry['touch_reason'] = None
                entry['touch_live_until'] = 0.0
                entry['touch_initial_until'] = time.monotonic() + TOUCH_HEARTBEAT_TTL
                entry['touch_challenges'].clear()
                channel.send(json.dumps({'status':'ready', 'protocol':1, 'channel_id':entry['touch_generation'],
                    'heartbeat_required': True}))
                self._ping_touch(entry)
                return True
            except Exception as exc:
                channel = entry.get('touch_channel')
                if channel and channel.readyState == 'open':
                    channel.send(json.dumps({'status':'unavailable',
                        'reason':str(exc.detail) if isinstance(exc, HTTPException) else '安卓触控桥暂不可用'}))
                    channel.close()
                return False
        task = asyncio.create_task(warm())
        entry['touch_warm_task'] = task
        try:
            return await asyncio.shield(task)
        finally:
            if entry.get('touch_warm_task') is task:
                entry['touch_warm_task'] = None

    async def close_session(self, identifier, *, reason='client_closed'):
        entry = self.entries.pop(identifier, None)
        if not entry:
            return
        media = entry.get('media')
        log.warning('安卓视频关闭：原因=%s 存活秒=%.1f 已发送帧=%d', reason,
            max(0, time.monotonic() - entry.get('created_at', time.monotonic())),
            getattr(media, 'frames_sent', 0))
        closing_touch = entry.get('touch_close_task')
        if closing_touch and closing_touch is not asyncio.current_task():
            await asyncio.gather(closing_touch, return_exceptions=True)
        replacing_touch = entry.get('touch_replace_task')
        if replacing_touch and replacing_touch is not asyncio.current_task():
            replacing_touch.cancel()
            await asyncio.gather(replacing_touch, return_exceptions=True)
        pending_touch = entry.get('touch_pending_channel')
        if pending_touch:
            pending_touch.close()
        timer = entry.get('timer')
        if timer and timer is not asyncio.current_task():
            timer.cancel()
        if entry.get('track'):
            entry['track'].stop()
        if entry.get('audio_track'):
            entry['audio_track'].stop()
        for name in ('touch_heartbeat_task', 'touch_reset_task'):
            task = entry.get(name)
            if task and task is not asyncio.current_task():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        warm = entry.get('touch_warm_task')
        if warm and warm is not asyncio.current_task():
            warm.cancel()
            await asyncio.gather(warm, return_exceptions=True)
        reader = entry.get('touch_reader')
        if reader and reader is not asyncio.current_task():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        try:
            if entry.get('gestures'):
                await entry['gestures'].close()
        finally:
            try:
                if entry.get('media'):
                    await entry['media'].close()
            finally:
                await entry['peer'].close()

    async def close(self):
        for identifier in list(self.entries):
            await self.close_session(identifier, reason='service_shutdown')


def sessions(app):
    if not getattr(app, 'android_screen_sessions', None):
        app.android_screen_sessions = ScreenSessions(app)
    return app.android_screen_sessions


@router.get('/config')
async def configuration(request: Request, actor=Depends(authenticate)):
    from . import android_adb, android_workspace
    app = request.app.state
    with app.db() as db:
        android_workspace._active_device(db, actor)
        row = android_workspace._required(app, db, actor)
        serial = row.serial
    sdk = await android_adb.read_sdk(app.settings, serial)
    return {'transport_policy': 'direct_only', 'stun_urls': stun_urls(app), 'target_fps': 30,
            'audio_supported': sdk >= 30,
            'audio_unavailable_reason': None if sdk >= 30 else '此设备系统声音转发需要 Android 11 及以上，当前设备仅提供画面'}


@router.post('/offer')
async def negotiate(body: StreamOffer, request: Request, actor=Depends(authenticate)):
    return await sessions(request.app.state).negotiate(actor, body)


@router.post('/{session_id}/close')
async def close(session_id: str, request: Request, actor=Depends(authenticate)):
    manager = sessions(request.app.state)
    entry = manager.entries.get(session_id)
    if entry and (entry['owner_id'] != actor.user_id or entry['device_id'] != actor.device_id):
        raise HTTPException(404, '屏幕会话不存在')
    await manager.close_session(session_id)
    return {'closed': True}


async def display_session(request, actor, session_id):
    manager = sessions(request.app.state)
    entry = manager.entries.get(session_id)
    if not entry or entry['owner_id'] != actor.user_id or entry['device_id'] != actor.device_id:
        raise HTTPException(404, '屏幕会话不存在')
    media = entry.get('media')
    if not media:
        raise HTTPException(409, '连续画面尚未就绪')
    await media.ensure()
    if media.display_task:
        await asyncio.shield(media.display_task)
    if manager.entries.get(session_id) is not entry or media.closed:
        raise HTTPException(409, '屏幕会话已结束')
    return media


@router.get('/{session_id}/display')
async def display_status(session_id: str, request: Request, actor=Depends(authenticate)):
    media = await display_session(request, actor, session_id)
    media.binding()
    return await media.display_guard.tick()


@router.post('/{session_id}/display')
async def display_control(session_id: str, body: DisplayRequest, request: Request, actor=Depends(authenticate)):
    from .android_gestures import gesture_barrier
    from .data import audit
    from .db import scope
    media = await display_session(request, actor, session_id)
    # 开关屏幕前释放现有触点，避免把同一次触摸送到改变后的显示状态。
    async with gesture_barrier(request.app.state, actor):
        result = await media.display_guard.set_off(session_id, actor, body.expected_epoch, body.screen_off)
    with request.app.state.db() as db:
        scope(db, actor.user_id, actor.household_id)
        audit(db, actor, 'android.display.updated', media.workspace_id,
            {'screen_off': body.screen_off, 'epoch': body.expected_epoch, 'verified': result['display_verified']})
        db.commit()
    await media.publish()
    return result
