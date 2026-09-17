import asyncio
import base64
import io
import json
import os
import secrets
import tempfile
from pathlib import Path
from functools import lru_cache
from fastapi import FastAPI, HTTPException, Header, Depends
from pydantic import BaseModel, Field, ConfigDict


class Call(BaseModel):
    model_config = ConfigDict(extra="forbid")
    arguments: dict
    invocation_id: str = Field(min_length=1, max_length=200)
    subject_id: str = Field(min_length=1, max_length=100)


def authenticate(authorization: str = Header(default="")):
    expected = os.environ.get("PROVIDER_SERVICE_TOKEN")
    if not expected or not secrets.compare_digest(authorization, "Bearer " + expected):
        raise HTTPException(401, "Provider 身份验证失败")


def binary(arguments, key="content_base64"):
    try:
        data = base64.b64decode(arguments[key], validate=True)
    except Exception:
        raise HTTPException(422, "需要有效 Base64 内容") from None
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(413, "输入超过 20 MB")
    return data


@lru_cache
def funasr_model():
    from funasr import AutoModel
    path = Path(os.environ["FUNASR_MODEL_PATH"])
    if not path.is_dir():
        raise RuntimeError("需要预置本地模型")
    return AutoModel(model=str(path), disable_update=True, trust_remote_code=False, device=os.environ.get("MODEL_DEVICE", "cpu"))


@lru_cache
def cosy_model():
    from cosyvoice.cli.cosyvoice import AutoModel
    path = Path(os.environ["COSYVOICE_MODEL_PATH"])
    if not path.is_dir():
        raise RuntimeError("需要预置本地模型")
    return AutoModel(model_dir=str(path))




def parse_document(call):
    from .docling_adapter import parse
    return parse(call, binary(call.arguments))


def transcribe_funasr(call):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "audio.wav"
        path.write_bytes(binary(call.arguments))
        result = funasr_model().generate(input=str(path))
        return {"text": "\n".join(item.get("text", "") for item in result)}


def synthesize_cosy(call):
    import torch
    import torchaudio
    model = cosy_model()
    speaker = call.arguments.get("speaker")
    speakers = model.list_available_spks()
    if speaker not in speakers:
        raise HTTPException(422, "仅允许模型内置声音，不开放声音克隆")
    text = str(call.arguments.get("text", ""))
    if not 1 <= len(text) <= 2000:
        raise HTTPException(422, "语音文本长度无效")
    chunks = [item["tts_speech"] for item in model.inference_sft(text, speaker, stream=False)]
    if not chunks:
        raise RuntimeError("没有生成音频")
    buffer = io.BytesIO()
    torchaudio.save(buffer, torch.cat(chunks, dim=1).cpu(), model.sample_rate, format="wav")
    return {"audio_base64": base64.b64encode(buffer.getvalue()).decode(), "sample_rate": model.sample_rate, "format": "wav"}


if os.environ.get("HOMEAI_ADAPTER") == "mem0":
    from .egress_guard import install_mem0_guard
    install_mem0_guard()

app = FastAPI(title="Home AI Provider bridge", dependencies=[Depends(authenticate)])
docling_lock = asyncio.Semaphore(1)
memory_lock = asyncio.Semaphore(1)


@app.get("/health")
def health():
    result = {"status": "alive", "adapter": os.environ.get("HOMEAI_ADAPTER", "unconfigured")}
    if result["adapter"] == "mem0":
        from .egress_guard import stats
        result["egress"] = dict(stats)
    return result


@app.post("/invoke/{operation}")
async def invoke(operation: str, call: Call):
    adapter = os.environ.get("HOMEAI_ADAPTER")
    try:
        if adapter == "docling" and operation == "parse":
            async with docling_lock:
                return await asyncio.to_thread(parse_document, call)
        if adapter == "funasr" and operation == "transcribe":
            return await asyncio.to_thread(transcribe_funasr, call)
        if adapter == "cosyvoice" and operation == "synthesize":
            return await asyncio.to_thread(synthesize_cosy, call)
        if adapter == "whisper" and operation == "transcribe":
            import httpx
            async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
                result = await client.post(os.environ["WHISPER_URL"].rstrip("/") + "/inference", files={"file": ("audio.wav", binary(call.arguments), "audio/wav")}, data={"response_format": "json"})
                result.raise_for_status()
                return result.json()
        if adapter == "mem0":
            async with memory_lock:
                return await asyncio.to_thread(memory_operation, operation, call)
        if adapter == "graphiti":
            from .graphiti_adapter import operation as graph_operation
            return await graph_operation(operation, call)
        if adapter == "mail":
            from .mail_adapter import operation as mail_operation
            return await asyncio.to_thread(mail_operation, operation, call)
        if adapter == "mcp_stdio":
            from .mcp_adapter import invoke_stdio
            return await invoke_stdio(operation, call)
        raise HTTPException(404, "该适配器不支持此操作")
    except HTTPException:
        raise
    except Exception as exc:
        # SDK 的原始异常可能包含服务地址、API Key 和个人内容。
        raise HTTPException(502, {"error": "Provider 执行失败", "type": type(exc).__name__}) from None


def memory_operation(operation, call):
    from .mem0_adapter import operation as invoke_memory
    return invoke_memory(operation, call)
