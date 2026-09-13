from __future__ import annotations

import os

import requests

API_KEY = os.environ.get("ELEVENLABS_API_KEY")
VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")
MODEL_ID = "eleven_flash_v2_5"  # lowest latency + cheapest tier
MAX_CHARS = 2000  # matches /chat's input cap

TTS_URL_TEMPLATE = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"


def is_configured() -> bool:
    """Whether an ElevenLabs API key is present -- voice output is optional."""
    return bool(API_KEY)


def synthesize_speech(text: str) -> bytes:
    """Convert text to speech (mp3 bytes) via ElevenLabs; raises on failure."""
    if not API_KEY:
        raise RuntimeError("ELEVENLABS_API_KEY is not set")

    response = requests.post(
        TTS_URL_TEMPLATE.format(voice_id=VOICE_ID),
        headers={"xi-api-key": API_KEY, "Content-Type": "application/json"},
        json={"text": text[:MAX_CHARS], "model_id": MODEL_ID},
        params={"output_format": "mp3_44100_128"},
        timeout=30,
    )
    if not response.ok:
        # ElevenLabs' error body (e.g. "quota_exceeded", "missing_permissions")
        # is the actual useful part -- raise_for_status() alone discards it.
        raise RuntimeError(f"ElevenLabs {response.status_code}: {response.text[:300]}")
    return response.content
