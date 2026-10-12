"""固定官方 scrcpy 4.1 的连续硬件 H264 与设备输出音频；不解码重编码视频、不录制。"""
import asyncio
import hashlib
import os
import secrets
import stat
import struct
import tempfile
import time
from fractions import Fraction
from pathlib import Path

import av
from fastapi import HTTPException
from .android_adb import load_policy, command_prefix, _execute_argv

SCRCPY_VERSION = '4.1'
SCRCPY_SHA256 = 'deacb991ed2509715160ffdc7907e47b4160eb30d1566217e9047fd5b8850cae'
SCRCPY_URL = 'https://github.com/Genymobile/scrcpy/releases/download/v4.1/scrcpy-server-v4.1'
VIDEO_CODEC = 0x68323634
AUDIO_CODEC = 0x00726177
FLAG_SESSION, FLAG_CONFIG, FLAG_KEYFRAME = 1 << 63, 1 << 62, 1 << 61
PTS_MASK = (1 << 61) - 1
MAX_VIDEO_PACKET = 4 * 1024 * 1024
MAX_VIDEO_QUEUE_BYTES = 6 * 1024 * 1024
MAX_AUDIO_PACKET = 4096
MAX_CONFIG_PACKET = 256 * 1024



class AudioSetupUnavailable(HTTPException):
    """设备音频导致独立采集实例停止时，只允许显式重建一次纯视频实例。"""
    def __init__(self, reason):
        super().__init__(503, reason)

def server_path(settings):
    return Path(settings.state_dir) / 'android-media' / 'scrcpy-server-v4.1.jar'


def checked_server(settings):
    path = server_path(settings)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            metadata = os.fstat(stream.fileno())
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                    or metadata.st_mode & 0o022):
                raise ValueError()
            raw = stream.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != SCRCPY_SHA256:
            raise ValueError()
        return path
    except (OSError, ValueError):
        raise HTTPException(409, '连续画面组件未安装或校验失败，请安装固定版本官方 scrcpy 4.1 服务端') from None


def server_arguments(scid, *, audio_enabled):
    if not isinstance(scid, str) or len(scid) != 8 or any(c not in '0123456789abcdef' for c in scid) or int(scid, 16) >= 1 << 31:
        raise ValueError('scrcpy 会话标识无效')
    return [SCRCPY_VERSION, 'scid=' + scid, 'tunnel_forward=true', 'video=true', 'video_codec=h264',
        'max_size=1280', 'max_fps=30', 'video_bit_rate=4000000',
        'video_codec_options=i-frame-interval=1,profile=1,level=512,frame-rate=30',
        'audio=' + ('true' if audio_enabled else 'false'), 'audio_source=output', 'audio_codec=raw',
        'control=false', 'clipboard_autosync=false', 'cleanup=false', 'power_on=false',
        'send_device_meta=false', 'send_dummy_byte=true', 'send_stream_meta=true', 'send_frame_meta=true']


async def _read(reader, count, *, timeout=10):
    try:
        return await asyncio.wait_for(reader.readexactly(count), timeout)
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
        raise HTTPException(503, 'Android 连续媒体流中断或未及时返回数据') from None


def _video_header(raw):
    flags, second, third = struct.unpack('>III', raw)
    if flags & 0x80000000:
        if flags not in (0x80000000, 0x80000001) or not (1 <= second <= 4096 and 1 <= third <= 4096 and second * third <= 8_388_608):
            raise HTTPException(502, 'Android 视频会话尺寸或标记无效')
        return 'session', second, third
    packed_pts, size = struct.unpack('>QI', raw)
    if not 0 < size <= MAX_VIDEO_PACKET:
        raise HTTPException(502, 'Android 视频帧超过允许大小')
    return 'packet', packed_pts, size


def _h264_nals(data):
    """仅扫描 Annex-B NAL 标记，不解析屏幕内容或启动软件解码器。"""
    import re
    return [data[match.end()] & 0x1f for match in re.finditer(b'\x00\x00(?:\x00)?\x01', data) if match.end() < len(data)]


def _validate_config(data):
    import re
    starts = list(re.finditer(b'\x00\x00(?:\x00)?\x01', data))
    types = _h264_nals(data)
    if not {7, 8}.issubset(types):
        raise HTTPException(502, 'Android 视频缺少 SPS/PPS 配置')
    for start in starts:
        offset = start.end()
        if offset < len(data) and data[offset] & 0x1f == 7:
            if offset + 3 >= len(data) or data[offset + 1] != 66 or not 1 <= data[offset + 3] <= 31:
                raise HTTPException(503, '设备未遵守 H264 Baseline 3.1 编码要求，不会切换为更高规格')


class AndroidMediaCapture:
    def __init__(self, settings, serial, *, audio_enabled=False):
        self.settings, self.serial = settings, serial
        self.audio_requested = bool(audio_enabled)
        self.audio_available = False
        self.audio_disabled_stream = False
        self.audio_reason = '未启用设备声音' if not audio_enabled else '设备声音尚未就绪'
        self.width = self.height = 0
        self.sdk = 0
        self.process = None
        self.policy = None
        self.prefix = None
        self.transport_serial = serial
        self.local_spec = self.remote_spec = None
        self.socket_dir = self.socket_path = None
        self.video_reader = self.audio_reader = None
        self.audio_writer = None
        self.writers, self.tasks = [], []
        self.video_queue = asyncio.Queue(maxsize=8)
        self.audio_queue = asyncio.Queue(maxsize=8)
        self.video_bytes = 0
        self.config = b''
        self.waiting_keyframe = True
        self.last_video_pts = -1
        self.last_audio_pts = -1
        self.video_error = self.audio_error = None
        self.video_ready = asyncio.Event()
        self.closed = False
        self.started = False
        self.start_lock = asyncio.Lock()
        self.close_task = None
        self.failure_cleanup = None

    async def start(self):
        async with self.start_lock:
            if self.closed:
                raise HTTPException(409, '连续画面采集已经关闭')
            if self.started:
                return self
            self.policy = load_policy(self.settings)
            if not self.policy.enabled or self.serial not in self.policy.serials:
                raise HTTPException(403, '设备未获得本机 Android 采集授权')
            path = checked_server(self.settings)
            try:
                async with asyncio.timeout(20):
                    self.prefix = await command_prefix(self.policy, self.serial)
                    self.transport_serial = (await self._adb(['get-serialno'], limit=256)).decode().strip()
                    if not self.transport_serial or self.transport_serial == 'unknown':
                        raise HTTPException(503, '媒体连接未返回设备传输身份')
                    value = await self._adb(['shell', 'getprop', 'ro.build.version.sdk'], limit=64)
                    try:
                        self.sdk = int(value.decode('ascii').strip())
                    except (ValueError, UnicodeError):
                        raise HTTPException(503, '无法确认 Android 系统版本') from None
                    if not 21 <= self.sdk <= 100:
                        raise HTTPException(409, 'Android 系统版本不支持连续画面采集')
                    include_audio = self.audio_requested and self.sdk >= 30
                    if self.sdk < 30:
                        self.audio_reason = '设备声音需要 Android 11 或更高版本；当前设备仅提供画面'
                    remote_jar = '/data/local/tmp/homeai-scrcpy-' + SCRCPY_SHA256 + '.jar'
                    await self._adb(['push', str(path), remote_jar], timeout=15)
                    checksum = await self._adb(['shell', 'sha256sum', remote_jar], limit=1024)
                    checksum_parts = checksum.decode('ascii', 'replace').split()
                    if not checksum_parts or checksum_parts[0] != SCRCPY_SHA256:
                        raise HTTPException(409, '设备端连续画面组件校验失败')
                    scid = f'{secrets.randbelow(1 << 31):08x}'
                    self.remote_spec = 'localabstract:scrcpy_' + scid
                    # Unix socket 位于本账户 0700 目录；不受既有 adb daemon 的 -a 公网监听选项影响。
                    self.socket_dir = Path(tempfile.mkdtemp(prefix='homeai-media-', dir='/private/tmp' if Path('/private/tmp').is_dir() else '/tmp'))
                    os.chmod(self.socket_dir, 0o700)
                    self.socket_path = self.socket_dir / 'capture.sock'
                    self.local_spec = 'localfilesystem:' + str(self.socket_path)
                    await self._adb(['forward', '--no-rebind', self.local_spec, self.remote_spec])
                    environment = {key: os.environ[key] for key in ('HOME', 'USER', 'TMPDIR') if key in os.environ}
                    environment['PATH'] = '/usr/bin:/bin'
                    self.process = await asyncio.create_subprocess_exec(*self.prefix,
                        'shell', 'CLASSPATH=' + remote_jar, 'app_process', '/', 'com.genymobile.scrcpy.Server',
                        *server_arguments(scid, audio_enabled=include_audio),
                        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE, env=environment)
                    self.tasks += [asyncio.create_task(self._drain(self.process.stdout)), asyncio.create_task(self._drain(self.process.stderr))]
                    self.video_reader = await self._connect_video()
                    # 先把所有 socket 接齐，scrcpy 才发送 codec 头；否则音频开启时会互等。
                    if include_audio:
                        self.audio_reader, writer = await asyncio.open_unix_connection(str(self.socket_path), limit=65536)
                        self.audio_writer = writer
                        self.writers.append(writer)
                    video_codec = int.from_bytes(await _read(self.video_reader, 4), 'big')
                    if video_codec != VIDEO_CODEC:
                        raise HTTPException(503, 'Android 未建立要求的 H264 Baseline 视频流')
                    if include_audio:
                        audio_codec = int.from_bytes(await _read(self.audio_reader, 4), 'big')
                        if audio_codec == 0:
                            self.audio_disabled_stream = True
                            self.audio_reason = '设备或当前应用未允许输出声音采集，画面仍可使用'
                        elif audio_codec == 1:
                            raise AudioSetupUnavailable('设备拒绝所配置的输出声音格式')
                        elif audio_codec != AUDIO_CODEC:
                            raise HTTPException(502, 'Android 返回了非约定的输出音频格式')
                        else:
                            self.audio_available, self.audio_reason = True, ''
                    self.tasks.append(asyncio.create_task(self._read_video()))
                    if self.audio_available:
                        self.tasks.append(asyncio.create_task(self._read_audio()))
                    try:
                        await asyncio.wait_for(self.video_ready.wait(), 8)
                    except asyncio.TimeoutError:
                        if self.audio_disabled_stream:
                            raise AudioSetupUnavailable('设备关闭声音采集时同时停止了媒体实例') from None
                        raise
                    if self.video_error:
                        if self.audio_disabled_stream:
                            raise AudioSetupUnavailable('设备关闭声音采集时同时停止了媒体实例')
                        raise self.video_error
                    self.started = True
                    return self
            except BaseException:
                await self.close()
                raise

    async def _adb(self, args, **kwargs):
        if self.prefix is None:
            raise HTTPException(409, '媒体采集尚未绑定设备连接')
        return await _execute_argv(self.prefix + list(args), **kwargs)

    async def _connect_video(self):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            writer = None
            try:
                if self.process.returncode is not None:
                    raise HTTPException(503, '设备视频编码器未能启动，请检查 H264 Baseline 兼容性')
                reader, writer = await asyncio.open_unix_connection(str(self.socket_path), limit=262144)
                if await _read(reader, 1, timeout=.5) != b'\x00':
                    raise HTTPException(502, 'Android 连续画面握手无效')
                self.writers.append(writer)
                return reader
            except (OSError, HTTPException):
                if writer:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except OSError:
                        pass
                await asyncio.sleep(.05)
        raise HTTPException(504, 'Android 连续画面握手超时')

    async def _drain(self, stream):
        # 不记录 adb / scrcpy 设备正文或原始诊断，只排空管道避免进程阻塞。
        while await stream.read(4096):
            pass

    def _clear_video(self):
        while not self.video_queue.empty():
            self.video_queue.get_nowait()
        self.video_bytes = 0

    def _push_video(self, data, pts, keyframe):
        if self.video_queue.full() or self.video_bytes + len(data) > MAX_VIDEO_QUEUE_BYTES:
            self._clear_video()
            self.waiting_keyframe = True
        if self.waiting_keyframe and not keyframe:
            return
        if keyframe:
            self.waiting_keyframe = False
        self.video_queue.put_nowait((data, pts, keyframe))
        self.video_bytes += len(data)

    async def _read_video(self):
        try:
            while not self.closed:
                # 屏幕静止时编码器可以不产生新包；连接健康由独立探测判断，不能按无帧超时断开。
                header = _video_header(await _read(self.video_reader, 12, timeout=None))
                if header[0] == 'session':
                    self.width, self.height = header[1:]
                    self._clear_video()
                    self.waiting_keyframe, self.config = True, b''
                    continue
                flags, size = header[1:]
                data = await _read(self.video_reader, size)
                if not self.width or not self.height:
                    raise HTTPException(502, 'Android 视频数据缺少会话尺寸')
                nals = _h264_nals(data)
                if not nals:
                    raise HTTPException(502, 'Android 视频不是 Annex-B H264 格式')
                if flags & FLAG_CONFIG:
                    if size > MAX_CONFIG_PACKET or flags != FLAG_CONFIG:
                        raise HTTPException(502, 'Android 视频缺少有效的 H264 配置')
                    _validate_config(data)
                    self.config = data
                    continue
                pts = flags & PTS_MASK
                if pts < self.last_video_pts:
                    raise HTTPException(502, 'Android 视频时间戳逆序')
                self.last_video_pts = pts
                keyframe = bool(flags & FLAG_KEYFRAME) or 5 in nals
                if keyframe:
                    if {7, 8}.issubset(nals):
                        _validate_config(data)
                    else:
                        if not self.config:
                            continue
                        data = self.config + data
                self._push_video(data, pts, keyframe)
                if not self.video_queue.empty():
                    self.video_ready.set()
        except asyncio.CancelledError:
            return
        except Exception as error:
            self.video_error = error if isinstance(error, HTTPException) else HTTPException(503, 'Android 视频采集已中断')
            self._clear_video()
            self.video_queue.put_nowait(None)
            self.video_ready.set()
            if not self.closed and self.failure_cleanup is None:
                self.failure_cleanup = asyncio.create_task(self.close())

    async def _read_audio(self):
        try:
            while not self.closed:
                flags, size = struct.unpack('>QI', await _read(self.audio_reader, 12))
                if flags & (FLAG_SESSION | FLAG_CONFIG | FLAG_KEYFRAME) or not 0 < size <= MAX_AUDIO_PACKET or size % 4:
                    raise HTTPException(502, 'Android 输出音频包格式无效')
                data = await _read(self.audio_reader, size)
                if flags < self.last_audio_pts:
                    raise HTTPException(502, 'Android 音频时间戳逆序')
                self.last_audio_pts = flags
                if self.audio_queue.full():
                    self.audio_queue.get_nowait()
                self.audio_queue.put_nowait((data, flags))
        except asyncio.CancelledError:
            return
        except Exception as error:
            self.audio_available = False
            self.audio_reason = '设备输出声音采集中断；不会切换到麦克风'
            self.audio_error = error if isinstance(error, HTTPException) else HTTPException(503, self.audio_reason)
            if self.audio_writer:
                self.audio_writer.close()
            while not self.audio_queue.empty():
                self.audio_queue.get_nowait()
            self.audio_queue.put_nowait(None)

    async def video(self):
        if self.video_error:
            raise self.video_error
        if self.closed:
            raise HTTPException(409, '连续画面采集已经关闭')
        item = await self.video_queue.get()
        if item is None:
            raise self.video_error or HTTPException(503, 'Android 视频采集已结束')
        data, pts, keyframe = item
        self.video_bytes -= len(data)
        packet = av.Packet(data)
        packet.pts = packet.dts = pts
        packet.time_base = Fraction(1, 1_000_000)
        packet.is_keyframe = keyframe
        return packet

    async def check_health(self):
        if self.closed or self.video_error or not self.process or self.process.returncode is not None:
            raise self.video_error or HTTPException(503, '安卓采集进程已结束')
        # 固定传输上的只读探测区分“没有新画面”和“设备已经断开”，不切换设备重放命令。
        value = await self._adb(['shell', 'getprop', 'sys.boot_completed'], timeout=2, limit=64)
        if value.strip() != b'1':
            raise HTTPException(503, '安卓设备尚未完成启动或连接已中断')
        return True

    async def audio(self):
        if not self.audio_available:
            raise HTTPException(409, self.audio_reason)
        item = await self.audio_queue.get()
        if item is None:
            raise self.audio_error or HTTPException(503, 'Android 输出声音已结束')
        data, pts = item
        frame = av.AudioFrame(format='s16', layout='stereo', samples=len(data) // 4)
        frame.sample_rate = 48000
        frame.pts, frame.time_base = pts, Fraction(1, 1_000_000)
        frame.planes[0].update(data + b'\x00' * (frame.planes[0].buffer_size - len(data)))
        return frame

    async def close(self):
        # 多个调用者共享一次清理；取消其中一个等待者不能遗留 adb 进程或 forward。
        if self.close_task is None:
            self.closed = True
            self.close_task = asyncio.create_task(self._close_resources())
        await asyncio.shield(self.close_task)

    async def _close_resources(self):
        errors = []
        try:
            for writer in self.writers:
                writer.close()
            for writer in self.writers:
                try:
                    await asyncio.wait_for(writer.wait_closed(), .5)
                except (OSError, asyncio.TimeoutError):
                    pass
            self.writers.clear()
            for task in self.tasks:
                if task is not asyncio.current_task():
                    task.cancel()
            await asyncio.gather(*(task for task in self.tasks if task is not asyncio.current_task()), return_exceptions=True)
            self.tasks.clear()
            if self.process and self.process.returncode is None:
                try:
                    await asyncio.wait_for(self.process.wait(), .5)
                except asyncio.TimeoutError:
                    self.process.terminate()
                    try:
                        await asyncio.wait_for(self.process.wait(), 1)
                    except asyncio.TimeoutError:
                        self.process.kill()
                        await self.process.wait()
        except Exception as error:
            errors.append(type(error).__name__)
        finally:
            if self.local_spec and self.policy:
                try:
                    listing = await self._adb(['forward', '--list'], limit=65536, timeout=3)
                    # 无线传输身份不同于成员绑定标识，仍只移除本实例创建的完整转发映射。
                    if any(line.split() == [self.transport_serial, self.local_spec, self.remote_spec]
                           for line in listing.decode('utf-8', 'replace').splitlines()):
                        await self._adb(['forward', '--remove', self.local_spec], timeout=3)
                except (HTTPException, OSError) as error:
                    errors.append(type(error).__name__)
            try:
                if self.socket_path:
                    self.socket_path.unlink(missing_ok=True)
                if self.socket_dir:
                    self.socket_dir.rmdir()
            except FileNotFoundError:
                pass
            except OSError as error:
                errors.append(type(error).__name__)
            self._clear_video()
            self.video_queue.put_nowait(None)
            while not self.audio_queue.empty():
                self.audio_queue.get_nowait()
            self.audio_queue.put_nowait(None)
            self.video_ready.set()
        if errors:
            raise HTTPException(503, '连续媒体资源未全部确认清理，请检查本机 ADB 状态')


async def create_capture(settings, serial, *, audio_enabled=False):
    try:
        return await AndroidMediaCapture(settings, serial, audio_enabled=audio_enabled).start()
    except AudioSetupUnavailable as error:
        if not audio_enabled:
            raise
        # scrcpy 音频致命配置错误会终止全部处理器，不能只忽略音频头就假报视频可用。
        # start 的异常路径先完整清理旧实例，随后只关闭音频，不改变视频规格或改用麦克风。
        capture = await AndroidMediaCapture(settings, serial, audio_enabled=False).start()
        capture.audio_requested = True
        capture.audio_available = False
        capture.audio_reason = str(error.detail) + '；已重新建立仅画面连接'
        return capture
