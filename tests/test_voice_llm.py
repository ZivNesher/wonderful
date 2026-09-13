"""Tests for the ElevenLabs signed-URL fetch (voice_llm.get_signed_url). No real
network calls -- requests.get is monkeypatched. The SSE/history-translation
logic in voice_llm.py is covered at the HTTP layer in test_server.py.
"""
import pytest

import voice_llm


def test_get_signed_url_raises_without_api_key(monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.setenv("ELEVENLABS_AGENT_ID", "agent_test")
    with pytest.raises(RuntimeError, match="must both be set"):
        voice_llm.get_signed_url()


def test_get_signed_url_raises_without_agent_id(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-fake-key")
    monkeypatch.delenv("ELEVENLABS_AGENT_ID", raising=False)
    with pytest.raises(RuntimeError, match="must both be set"):
        voice_llm.get_signed_url()


def test_get_signed_url_calls_documented_endpoint_shape(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-fake-key")
    monkeypatch.setenv("ELEVENLABS_AGENT_ID", "agent_test123")
    captured = {}

    class FakeResponse:
        ok = True

        def json(self):
            return {"signed_url": "wss://api.elevenlabs.io/v1/convai/conversation?fake=1"}

    def fake_get(url, params, headers, timeout):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        return FakeResponse()

    monkeypatch.setattr(voice_llm.requests, "get", fake_get)
    result = voice_llm.get_signed_url()

    assert result == "wss://api.elevenlabs.io/v1/convai/conversation?fake=1"
    assert captured["url"] == "https://api.elevenlabs.io/v1/convai/conversation/get-signed-url"
    assert captured["params"] == {"agent_id": "agent_test123"}
    assert captured["headers"]["xi-api-key"] == "sk-fake-key"


def test_get_signed_url_surfaces_upstream_error_detail(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-fake-key")
    monkeypatch.setenv("ELEVENLABS_AGENT_ID", "agent_test123")

    class FakeResponse:
        ok = False
        status_code = 404
        text = '{"detail":"agent not found"}'

    monkeypatch.setattr(voice_llm.requests, "get", lambda *a, **k: FakeResponse())
    with pytest.raises(RuntimeError, match="agent not found"):
        voice_llm.get_signed_url()
