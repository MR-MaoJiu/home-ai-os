"""对本地 whisper.cpp 的受控 WAV 转写桥接。"""
import io,os,wave
import httpx
from fastapi import HTTPException


def validate_audio(data):
    try:
        with wave.open(io.BytesIO(data),'rb') as audio:
            if audio.getframerate()!=16000 or audio.getnchannels()!=1 or audio.getsampwidth()!=2 or audio.getcomptype()!='NONE':
                raise HTTPException(422,'需要 16 kHz、16 位单声道 PCM WAV')
            frames=audio.getnframes()
            if not 1600<=frames<=960000:
                raise HTTPException(422,'音频时长必须在 0.1 到 60 秒之间')
            pcm=audio.readframes(frames)
            if len(pcm)!=frames*2:
                raise HTTPException(422,'音频数据不完整')
            if not any(pcm):
                raise HTTPException(422,'音频为全静音，未发起转写')
            return frames/16000
    except (wave.Error,EOFError):
        raise HTTPException(422,'无效 WAV 文件') from None


async def transcribe(call,data):
    if set(call.arguments)-{'content_base64','language'}:
        raise HTTPException(422,'转写参数不支持服务端路径或额外执行选项')
    duration=validate_audio(data)
    language=call.arguments.get('language','auto')
    if not isinstance(language,str) or language not in {'auto','zh','en'}:
        raise HTTPException(422,'当前开放自动识别、中文与英文')
    endpoint=os.environ.get('WHISPER_URL')
    if endpoint!='http://127.0.0.1:58085':
        raise HTTPException(503,'转写必须使用固定本地服务')
    async with httpx.AsyncClient(timeout=120,trust_env=False,follow_redirects=False) as client:
        response=await client.post(endpoint+'/inference',files={'file':('audio.wav',data,'audio/wav')},data={'response_format':'json','language':language,'temperature':'0.0','translate':'false'})
        response.raise_for_status()
        result=response.json()
    text=result.get('text')
    if not isinstance(text,str) or not text.strip():
        raise HTTPException(422,'未识别到清晰语音')
    return {'text':text.strip(),'duration_seconds':duration,'language':language,'provider':'whisper.cpp','model':'small-q5_1'}
