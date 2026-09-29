"""受资源与时间限制的媒体检查子进程；原件仅在内存中解密，不写明文临时文件。"""
import base64
import io
import json
import math
import sys

IMAGE_TYPES = ((b'\xff\xd8\xff', 'image/jpeg'), (b'\x89PNG\r\n\x1a\n', 'image/png'),
               (b'GIF87a', 'image/gif'), (b'GIF89a', 'image/gif'))


def image_type(data):
    for prefix, value in IMAGE_TYPES:
        if data.startswith(prefix):
            return value
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return 'image/webp'
    return None


def jpeg(frame, limit=1280, max_bytes=None):
    import av
    width = max(2, min(frame.width, limit) // 2 * 2)
    height = max(2, int(frame.height * width / frame.width) // 2 * 2)
    if height > limit:
        width = max(2, int(width * limit / height) // 2 * 2)
        height = limit
    while True:
        converted = frame.reformat(width=width, height=height, format='yuvj420p')
        output = io.BytesIO()
        with av.open(output, mode='w', format='image2pipe') as target:
            stream = target.add_stream('mjpeg', rate=1)
            stream.width, stream.height, stream.pix_fmt = width, height, 'yuvj420p'
            for packet in stream.encode(converted):target.mux(packet)
            for packet in stream.encode():target.mux(packet)
        encoded=output.getvalue()
        if not encoded:raise ValueError('无法生成媒体预览')
        if max_bytes is None or len(encoded)<=max_bytes:return base64.b64encode(encoded).decode()
        if min(width,height)<=2:raise ValueError('媒体预览超过大小限制')
        width=max(2,int(width*0.75)//2*2);height=max(2,int(height*0.75)//2*2)


def inspect(data, kind, frames=False, max_seconds=600, max_frames=6, frame_limit=1280, frame_max_bytes=None):
    import av
    mime = image_type(data)
    if kind == 'image' and not mime:
        raise ValueError('仅支持 JPEG、PNG、WebP 或 GIF 图片，请先转换格式')
    with av.open(io.BytesIO(data), options={'protocol_whitelist': 'pipe', 'enable_drefs': '0'}) as source:
        videos = list(source.streams.video)
        if not videos:
            raise ValueError('媒体中没有可解码的视频或图片内容')
        stream = videos[0]
        width, height = stream.codec_context.width, stream.codec_context.height
        if width <= 0 or height <= 0 or width * height > 32_000_000 or max(width, height) > 16384:
            raise ValueError('媒体尺寸超过限制')
        value = {'mime_type': mime, 'width': width, 'height': height}
        if kind == 'video':
            formats = set(source.format.name.split(','))
            if not formats.intersection({'mov', 'mp4', 'matroska', 'webm'}):
                raise ValueError('仅支持 MP4、MOV 或 WebM 视频')
            duration = source.duration / av.time_base if source.duration is not None else float(stream.duration * stream.time_base) if stream.duration is not None else None
            if duration is None or not math.isfinite(duration) or not 0 < duration <= max_seconds:
                raise ValueError(f'视频时长必须可确定且不超过 {max_seconds / 60:g} 分钟')
            value.update(mime_type='video/webm' if formats.intersection({'matroska', 'webm'}) else 'video/mp4', duration_seconds=duration)
        first = next(source.decode(stream), None)
        if first is None:
            raise ValueError('媒体内容不完整或无法解码')
        value['thumbnail'] = jpeg(first,frame_limit,frame_max_bytes)
        if frames:
            selections = [(0.0, first)]
            if kind == 'video':
                for timestamp in [min(value['duration_seconds'] * part / (max_frames-1),max(0,value['duration_seconds']-0.05)) for part in range(1,max_frames)]:
                    source.seek(int(timestamp * av.time_base), backward=True)
                    for index, frame in enumerate(source.decode(stream)):
                        if index > 300:
                            raise ValueError('视频关键帧间隔过大')
                        if frame.time is not None and frame.time > max_seconds:
                            raise ValueError('视频实际时间戳超过配置限制')
                        if frame.time is None or frame.time >= timestamp:
                            actual = float(frame.time) if frame.time is not None else timestamp
                            if not any(abs(t-actual)<0.01 for t,_ in selections):selections.append((actual, frame))
                            break
            value['frames'] = [{'time': round(timestamp, 3), 'content_base64': jpeg(frame,frame_limit,frame_max_bytes)} for timestamp, frame in selections]
        return value


if __name__ == '__main__':
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (40, 40))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        maximum = int(sys.argv[3]) if len(sys.argv)>3 else 200*1024**2
        max_seconds = int(sys.argv[4]) if len(sys.argv)>4 else 600
        if not 1<=maximum<=1024**3 or not 1<=max_seconds<=7200:raise ValueError('媒体限制参数无效')
        if sys.platform == 'linux':
            memory=max(2*1024**3,3*maximum+512*1024**2)
            resource.setrlimit(resource.RLIMIT_AS, (memory,memory))
        data = sys.stdin.buffer.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError('媒体超过限制')
        cloud=len(sys.argv)>2 and sys.argv[2]=='cloud'
        print(json.dumps(inspect(data,sys.argv[1],len(sys.argv)>2 and sys.argv[2] in {'frames','cloud'},max_seconds,4 if cloud else 6,1024 if cloud else 1280,512*1024 if cloud else None)))
    except Exception as exc:
        print(json.dumps({'error': str(exc) if isinstance(exc, ValueError) else '媒体无法解码或格式不受支持'}))
        sys.exit(1)
