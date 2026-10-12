"""连续编码媒体与成员权限绑定；媒体不落盘，画面凭据与 AI 截图分离。"""
import asyncio
import secrets
import time

from aiortc import VideoStreamTrack, AudioStreamTrack
from aiortc.mediastreams import MediaStreamError
from fastapi import HTTPException

from . import android_adb, android_workspace


class LiveMedia:
    def __init__(self, manager, identifier, actor, audio_enabled):
        self.manager, self.identifier, self.actor = manager, identifier, actor
        self.audio_enabled = audio_enabled
        self.capture = None
        self.start_task = None
        self.monitor = None
        self.health_monitor = None
        self.closed = False
        self.close_task = None
        self.serial = self.workspace_id = None
        self.geometry = None
        self.frame_id = secrets.token_urlsafe(24)
        self.last_video = 0.0
        self.frames_sent = 0
        self.authorized_at = 0.0
        self.first_video = asyncio.Event()
        self.metadata_lock = asyncio.Lock()
        self.display_guard = None
        self.display_task = None

    def binding(self):
        app = self.manager.app
        with app.db() as db:
            android_workspace._active_device(db, self.actor)
            row = android_workspace._required(app, db, self.actor)
            if self.workspace_id is not None and (row.id != self.workspace_id or row.serial != self.serial):
                raise HTTPException(409, '安卓绑定已变化，画面已停止')
            return row.id, row.serial

    async def ensure(self):
        if self.closed:
            raise MediaStreamError()
        if self.start_task is None:
            self.start_task = asyncio.create_task(self._start())
        return await asyncio.shield(self.start_task)

    async def _start(self):
        from .android_media import create_capture
        self.workspace_id, self.serial = self.binding()
        self.capture = await create_capture(self.manager.app.settings, self.serial, audio_enabled=self.audio_enabled)
        try:
            self.binding()  # 采集器启动期间仍可能解绑或撤销设备。
            if self.closed:
                raise MediaStreamError()
            self.authorized_at = time.monotonic()
            from .android_display import guard_for
            self.display_guard = guard_for(self.manager, self.actor, self.workspace_id, self.serial)
            # 电源能力检测不阻塞连续视频首帧，也不因设备不支持而关闭画面。
            self.display_task = asyncio.create_task(self.display_guard.attach(self.identifier))
            self.monitor = asyncio.create_task(self._monitor())
            self.health_monitor = asyncio.create_task(self._check_health())
            return self.capture
        except BaseException:
            await self.capture.close()
            raise

    async def publish(self):
        async with self.metadata_lock:
            if self.closed or not self.capture or not self.first_video.is_set():
                return
            self.binding()
            width, height = self.capture.width, self.capture.height
            if not 1 <= width <= 8192 or not 1 <= height <= 8192:
                raise HTTPException(502, '连续视频的尺寸无效')
            if self.geometry is None or self.geometry[:2] != (width, height):
                actual_width, actual_height = await android_adb.display_size(self.manager.app.settings, self.serial)
                if (width > height) != (actual_width > actual_height):
                    actual_width, actual_height = actual_height, actual_width
                if abs(width / height - actual_width / actual_height) > 0.04:
                    raise HTTPException(409, '视频与设备显示比例不一致，触控已停止')
                self.geometry = (width, height, actual_width, actual_height)
                self.frame_id = secrets.token_urlsafe(24)
            metadata = await android_workspace.publish_stream_frame(self.manager.app, self.actor,
                self.workspace_id, self.serial, self.geometry[2], self.geometry[3], self.frame_id)
            self.authorized_at = time.monotonic()
            display = self.display_guard.snapshot() if self.display_guard else {}
            entry = self.manager.entries.get(self.identifier, {})
            self.manager.send_metadata(self.identifier, {**metadata, 'width': width, 'height': height,
                'video_mode': 'continuous', 'target_fps': 30,
                'audio_available': bool(self.capture.audio_available), 'audio_reason': self.capture.audio_reason,
                'touch_available': bool(entry.get('touch_ready') and entry.get('touch_live_until', 0) > time.monotonic()),
                'touch_reason': entry.get('touch_reason'), **display})

    async def _monitor(self):
        try:
            await asyncio.wait_for(self.first_video.wait(), 10)
            while not self.closed:
                await self.publish()
                if self.display_guard and self.display_task and self.display_task.done():
                    await self.display_guard.tick()
                await asyncio.sleep(0.4)
        except asyncio.CancelledError:
            return
        except Exception as error:
            self.fail(error)

    async def _check_health(self):
        try:
            while not self.closed:
                self.binding()
                self.authorized_at = time.monotonic()
                if not self.last_video or time.monotonic() - self.last_video >= 2:
                    checked_at = time.monotonic()
                    try:
                        await self.capture.check_health()
                    except Exception:
                        # 探测期间若已有真实视频包到达，不能因一次慢 ADB 响应误杀正在播放的画面。
                        if self.last_video <= checked_at:
                            raise
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            return
        except Exception as error:
            self.fail(error)

    def fail(self, error):
        message = str(error.detail) if isinstance(error, HTTPException) else '连续画面采集已中断，请重新连接'
        self.manager.send_metadata(self.identifier, {'blocked_reason': message})
        asyncio.create_task(self.manager.close_session(self.identifier, reason='media_' + type(error).__name__))

    async def video(self):
        try:
            capture = await self.ensure()
            packet = await capture.video()
            if self.closed:
                raise MediaStreamError()
            self.last_video = time.monotonic()
            self.frames_sent += 1
            if not self.first_video.is_set():
                self.first_video.set()
                await self.publish()
            elif time.monotonic() - self.authorized_at > 1.5:
                # 视频包发送不等待界面元数据/ADB操作锁，仍独立复核配对和绑定。
                self.binding()
                self.authorized_at = time.monotonic()
            return packet
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.fail(error)
            raise MediaStreamError() from None

    async def audio(self):
        capture = await self.ensure()
        try:
            frame = await capture.audio()
            if self.closed:
                raise MediaStreamError()
            if time.monotonic() - self.authorized_at > 1.5:
                # 音频不能绕过共同的撤权检查，即便视频接收暂时阻塞。
                self.binding()
            return frame
        except asyncio.CancelledError:
            raise
        except Exception:
            # 音频不受设备支持时保留视频，但要明确下发原因，不能用静音冒充成功。
            await self.publish()
            raise MediaStreamError() from None

    async def close(self):
        if self.close_task is None:
            self.closed = True
            self.close_task = asyncio.create_task(self._close())
        await asyncio.shield(self.close_task)

    async def _close(self):
        if self.health_monitor:
            self.health_monitor.cancel()
            await asyncio.gather(self.health_monitor, return_exceptions=True)
        if self.monitor:
            self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
        if self.start_task and not self.start_task.done():
            self.start_task.cancel()
            await asyncio.gather(self.start_task, return_exceptions=True)
        if self.display_task:
            # 先等待只读检测完成，再在同一控制锁下恢复/释放，避免迟到 attach 泄漏进程。
            await asyncio.gather(self.display_task, return_exceptions=True)
        if self.display_guard:
            await self.display_guard.detach(self.identifier)
        if self.capture:
            await self.capture.close()


class ScreenTrack(VideoStreamTrack):
    def __init__(self, media):
        super().__init__()
        self.media = media

    async def recv(self):
        # 直接打包安卓硬件编码的 H264，不再抓 PNG 或在家庭服务器二次编码。
        return await self.media.video()


class SystemAudioTrack(AudioStreamTrack):
    def __init__(self, media):
        super().__init__()
        self.media = media

    async def recv(self):
        return await self.media.audio()
