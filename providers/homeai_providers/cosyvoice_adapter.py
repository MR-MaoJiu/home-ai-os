"""仅使用固定模型内置音色合成，不接受参考音频和声音克隆参数。"""
import base64
import io
import logging
import os
import wave
from functools import lru_cache
from pathlib import Path
from fastapi import HTTPException


@lru_cache
def runtime():
    # 上游默认会记录原始文本；在导入及推理前关闭其 Python 日志。
    logging.disable(logging.CRITICAL)
    import torch
    import wetext.wetext as wetext_runtime
    normalization = Path(os.environ['COSYVOICE_NORMALIZER_PATH'])
    def local_normalizer(repo_id):
        if repo_id != 'pengzhendong/wetext' or not normalization.is_dir():
            raise RuntimeError('文本规范化资源未配置')
        return str(normalization)
    # 固定 SDK 的资源解析钩子；实际 FST 计算仍由官方 Normalizer 执行。
    wetext_runtime.snapshot_download = local_normalizer
    from cosyvoice.cli.cosyvoice import CosyVoice
    torch.set_num_threads(4)
    path = Path(os.environ['COSYVOICE_MODEL_PATH'])
    if not path.is_dir():
        raise RuntimeError('需要固定本地 CosyVoice 模型')
    model = CosyVoice(str(path), load_jit=False, load_trt=False, fp16=False)
    if model.frontend.text_frontend != 'wetext':
        raise RuntimeError('离线文本规范化未正常加载')
    return model


def voices():
    return {'voices': runtime().list_available_spks(), 'voice_cloning': False}


def synthesize(call):
    args = call.arguments
    if set(args) - {'text', 'speaker'}:
        raise HTTPException(422, '只接受朗读文本和内置音色，不支持声音克隆')
    text, speaker = args.get('text'), args.get('speaker')
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 300:
        raise HTTPException(422, '朗读文本长度须为 1 至 300 字符')
    if not isinstance(speaker, str) or len(speaker) > 100:
        raise HTTPException(422, '需要有效的内置音色')
    # 特殊控制 Token 不能通过普通朗读入口改变模型指令。
    if '<|' in text or '|>' in text:
        raise HTTPException(422, '朗读文本不允许包含模型控制标记')
    model = runtime()
    if speaker not in model.list_available_spks():
        raise HTTPException(422, '该音色不是固定模型内置音色')
    import torch
    parts, samples = [], 0
    with torch.inference_mode():
        for item in model.inference_sft(text, speaker, stream=False):
            audio = item['tts_speech'].detach().cpu().float().reshape(-1)
            if not torch.isfinite(audio).all():
                raise HTTPException(502, '合成音频包含无效采样')
            samples += audio.numel()
            if samples > model.sample_rate * 45:
                raise HTTPException(413, '合成音频超过 45 秒，请缩短文本')
            parts.append(audio)
    if not parts or not samples:
        raise HTTPException(502, '模型未生成音频')
    pcm = (torch.cat(parts).clamp(-1, 1) * 32767).to(torch.int16).numpy().astype('<i2').tobytes()
    output = io.BytesIO()
    with wave.open(output, 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(model.sample_rate)
        stream.writeframes(pcm)
    return {'audio_base64': base64.b64encode(output.getvalue()).decode(),
            'sample_rate': model.sample_rate, 'format': 'wav', 'channels': 1,
            'duration_seconds': samples / model.sample_rate, 'speaker': speaker}
