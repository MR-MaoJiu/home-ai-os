"""使用 SDK 内置 SenseVoiceSmall，禁止远程代码及运行时联网。"""
import io,os,wave
from functools import lru_cache
from importlib.metadata import version
from pathlib import Path
from fastapi import HTTPException
from .whisper_adapter import validate_audio


@lru_cache
def model():
    if version('funasr')!='1.4.14':raise RuntimeError('FunASR 版本不符')
    from funasr import AutoModel
    root=Path(os.environ['FUNASR_MODEL_PATH'])
    if not root.is_dir():raise RuntimeError('需要已校验的本地模型')
    return AutoModel(model=str(root),device='cpu',ncpu=4,disable_update=True,trust_remote_code=False,disable_pbar=True)


def transcribe(call,data):
    if set(call.arguments)-{'content_base64','language'}:raise HTTPException(422,'不支持额外转写参数')
    duration=validate_audio(data)
    language=call.arguments.get('language','auto')
    if not isinstance(language,str) or language not in {'auto','zh','en','yue','ja','ko'}:raise HTTPException(422,'模型不支持该语言')
    import numpy as np
    from funasr.utils.postprocess_utils import rich_transcription_postprocess
    with wave.open(io.BytesIO(data),'rb') as audio:
        samples=np.frombuffer(audio.readframes(audio.getnframes()),dtype='<i2').astype(np.float32)/32768.0
    result=model().generate(input=samples,fs=16000,cache={},language=language,use_itn=True,batch_size_s=60)
    text='\n'.join(rich_transcription_postprocess(row.get('text','')) for row in result).strip()
    if not text:raise HTTPException(422,'未识别到清晰语音')
    return {'text':text,'duration_seconds':duration,'language':language,'provider':'funasr','model':'SenseVoiceSmall'}
