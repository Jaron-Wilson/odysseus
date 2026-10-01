import os
import tempfile

from services.stt.stt_service import STTService


def test_stt_local_transcribe_leak_on_error(monkeypatch):
    """A failing local transcription returns None and leaves no temp files
    (audio is decoded in memory now; it used to go through a temp file)."""
    service = STTService()
    monkeypatch.setattr(service, "_load_settings", lambda: {
        "stt_enabled": True, "stt_provider": "local", "stt_model": "base.en",
        "stt_parakeet_model": "", "stt_language": "",
    })
    monkeypatch.setattr(service, "readiness", lambda engine, model: None)

    class MockWhisper:
        def transcribe(self, *args, **kwargs):
            raise ValueError("Simulated transcribe error")

    monkeypatch.setattr(service, "_load", lambda engine, model: MockWhisper())

    temp_dir = tempfile.gettempdir()
    before = set(os.listdir(temp_dir))
    import io, wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(b"\0\0" * 8000)
    result = service._transcribe_local(buf.getvalue())
    after = set(os.listdir(temp_dir))

    assert result is None
    leaked = {f for f in after - before if f.endswith((".webm", ".wav", ".ogg", ".mp4", ".mp3"))}
    assert not leaked, f"Leaked files: {leaked}"
