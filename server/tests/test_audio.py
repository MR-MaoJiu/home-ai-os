import io,wave
import pytest
from fastapi import HTTPException
from homeai_providers.whisper_adapter import validate_audio


def wav(rate=16000,channels=1,width=2,frames=3200,silent=False):
    stream=io.BytesIO()
    with wave.open(stream,'wb') as audio:
        audio.setnchannels(channels);audio.setsampwidth(width);audio.setframerate(rate)
        audio.writeframes((b'\0' if silent else b'\1')*(frames*width*channels))
    return stream.getvalue()


def test_pcm_format_and_duration():
    assert validate_audio(wav())==0.2
    for content in [b'not-wave',wav(rate=44100),wav(channels=2),wav(width=1),wav(frames=1),wav(silent=True),wav()[:-100]]:
        with pytest.raises(HTTPException):validate_audio(content)
