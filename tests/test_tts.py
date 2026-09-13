"""Tests for the ElevenLabs TTS client (tts.py). No real network calls -- the
one live HTTP call (requests.post) is monkeypatched, since a real call needs a
paid API key this environment doesn't have.
"""
import pytest

import tts


def test_is_configured_reflects_api_key(monkeypatch):
    monkeypatch.setattr(tts, "API_KEY", None)
    assert tts.is_configured() is False
    monkeypatch.setattr(tts, "API_KEY", "sk-fake-key")
    assert tts.is_configured() is True


def test_synthesize_speech_raises_without_api_key(monkeypatch):
    monkeypatch.setattr(tts, "API_KEY", None)
    with pytest.raises(RuntimeError):
        tts.synthesize_speech("hello")


def test_synthesize_speech_calls_documented_endpoint_shape(monkeypatch):
    monkeypatch.setattr(tts, "API_KEY", "sk-fake-key")
    monkeypatch.setattr(tts, "VOICE_ID", "test-voice-id")
    captured = {}

    class FakeResponse:
        content = b"fake-mp3-bytes"

        def raise_for_status(self):
            pass

    def fake_post(url, headers, json, params, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["params"] = params
        return FakeResponse()

    monkeypatch.setattr(tts.requests, "post", fake_post)
    result = tts.synthesize_speech("hello world")

    assert result == b"fake-mp3-bytes"
    assert captured["url"] == "https://api.elevenlabs.io/v1/text-to-speech/test-voice-id"
    assert captured["headers"]["xi-api-key"] == "sk-fake-key"
    assert captured["json"]["text"] == "hello world"
    assert captured["json"]["model_id"] == "eleven_flash_v2_5"


def test_synthesize_speech_truncates_long_text(monkeypatch):
    monkeypatch.setattr(tts, "API_KEY", "sk-fake-key")
    captured = {}

    class FakeResponse:
        content = b"x"

        def raise_for_status(self):
            pass

    def fake_post(url, headers, json, params, timeout):
        captured["text"] = json["text"]
        return FakeResponse()

    monkeypatch.setattr(tts.requests, "post", fake_post)
    tts.synthesize_speech("x" * 5000)
    assert len(captured["text"]) == tts.MAX_CHARS
