from __future__ import annotations

import json
import os
import threading
import time
import uuid

import requests

import agent

MODEL_NAME = "airport-investment-agent"
FILLER_TEXT = "Let me check on that."
KEEPALIVE_INTERVAL_SECONDS = 1

SIGNED_URL_ENDPOINT = "https://api.elevenlabs.io/v1/convai/conversation/get-signed-url"


def get_signed_url() -> str:
    """Ask ElevenLabs for a short-lived (15-min) signed WebRTC URL for our
    voice-call Agent, using our API key server-side -- the browser gets only
    this one-time URL, never the real key. Raises on failure; the caller
    turns that into a clean HTTP error."""
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    agent_id = os.environ.get("ELEVENLABS_AGENT_ID")
    if not api_key or not agent_id:
        raise RuntimeError("ELEVENLABS_API_KEY and ELEVENLABS_AGENT_ID must both be set")

    response = requests.get(
        SIGNED_URL_ENDPOINT,
        params={"agent_id": agent_id},
        headers={"xi-api-key": api_key},
        timeout=15,
    )
    if not response.ok:
        raise RuntimeError(f"ElevenLabs {response.status_code}: {response.text[:300]}")
    return response.json()["signed_url"]


def openai_messages_to_history(messages: list[dict]) -> tuple[list[dict], str]:
    """Convert an OpenAI-format message list (as ElevenLabs' Custom LLM integration
    sends it) into our internal history + latest user message. Any 'system' role
    message ElevenLabs sends is ignored -- guardrails.SYSTEM_PROMPT is our one
    system prompt, never duplicated or overridden by the voice platform's config."""
    turns = [m for m in messages if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)]
    if not turns or turns[-1]["role"] != "user":
        raise ValueError("expected the latest message to be from the user")
    latest_user_message = turns[-1]["content"]
    history = [{"role": t["role"], "content": t["content"]} for t in turns[:-1]]
    return history, latest_user_message


def _sse_chunk(chunk_id: str, content: str | None = None, finish_reason: str | None = None) -> str:
    """One OpenAI-compatible chat-completion-chunk SSE frame."""
    delta = {"role": "assistant", "content": content} if content is not None else {}
    payload = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": MODEL_NAME,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload)}\n\n"


def stream_reply(client, history: list[dict], user_message: str):
    """Run one voice-call turn and yield it as an OpenAI-style SSE stream.

    Sends a short filler chunk immediately (agent.run_turn can take several
    seconds when it calls a tool -- a live BTS lookup, a score, etc.).
    agent.run_turn then runs in a background thread while this generator
    yields a keep-alive chunk every second.

    That keep-alive carries a single real (if inaudible-ish) content
    character, not an empty delta: a real call was observed getting resent
    by ElevenLabs mid-turn with our filler already baked in as a *completed*
    assistant message -- i.e. a pause after real content, even with
    technically-valid empty-delta chunks in between, was read as "the
    assistant is done," not "still thinking." Continuously growing content
    is the more convincing signal that generation is still in progress."""
    chunk_id = f"chatcmpl-{uuid.uuid4().hex}"
    yield _sse_chunk(chunk_id, content=FILLER_TEXT + " ")

    result: dict = {}

    def _run():
        result["reply_text"], _, _ = agent.run_turn(client, history, user_message)

    worker = threading.Thread(target=_run)
    worker.start()
    while worker.is_alive():
        worker.join(timeout=KEEPALIVE_INTERVAL_SECONDS)
        if worker.is_alive():
            yield _sse_chunk(chunk_id, content=" ")

    yield _sse_chunk(chunk_id, content=result["reply_text"])
    yield _sse_chunk(chunk_id, finish_reason="stop")
    yield "data: [DONE]\n\n"
