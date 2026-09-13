from __future__ import annotations
import os
from dotenv import load_dotenv
load_dotenv()
import uuid
from pathlib import Path
import anthropic
from fastapi import FastAPI, Header
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel
import agent
import sessions
import voice_llm

if not os.environ.get("ANTHROPIC_API_KEY"):
    raise RuntimeError(
        "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in -- "
        "the server refuses to start without it rather than failing confusingly later."
    )
if not os.environ.get("BTS_SOCRATA_APP_TOKEN"):
    raise RuntimeError(
        "BTS_SOCRATA_APP_TOKEN is not set -- required for live traffic data (free, "
        "instant signup at https://data.bts.gov/signup, then Profile -> Developer "
        "Settings -> Create New App Token). Without it, every traffic/scoring "
        "question would fail at query time instead of here at startup."
    )

client = anthropic.Anthropic()
app = FastAPI()

WEB_DIR = Path(__file__).resolve().parent / "web"
VOICE_LLM_SHARED_SECRET = os.environ.get("VOICE_LLM_SHARED_SECRET")


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class ChatResponse(BaseModel):
    reply: str
    session_id: str
    used_web_search: bool = False


class VoiceLLMRequest(BaseModel):
    """Minimal slice of the OpenAI chat-completions request shape -- we only
    need `messages`; every other field (model, temperature, tool defs for
    ElevenLabs' own call-control tools, etc.) is accepted and ignored."""
    messages: list[dict]


@app.get("/")
def index() -> FileResponse:
    """Serve the single-page chat UI."""
    return FileResponse(WEB_DIR / "index.html")


@app.post("/chat", response_model=None)
def chat(req: ChatRequest) -> ChatResponse | JSONResponse:
    """Validate the request, run one agent turn, and persist the updated session."""
    message = (req.message or "").strip()
    if not message:
        return JSONResponse(status_code=400, content={"error": "message must not be empty"})
    if len(message) > 2000:
        return JSONResponse(status_code=400, content={"error": "message is too long (2000 char max)"})

    if req.session_id is not None and not sessions.is_valid_session_id(req.session_id):
        return JSONResponse(status_code=400, content={"error": "invalid session_id"})

    session_id = req.session_id or str(uuid.uuid4())
    history = sessions.load_messages(session_id)

    reply_text, updated_history, used_web_search = agent.run_turn(client, history, message)
    sessions.save(session_id, updated_history)

    return ChatResponse(reply=reply_text, session_id=session_id, used_web_search=used_web_search)


@app.post("/v1/chat/completions", response_model=None)
def voice_chat_completions(req: VoiceLLMRequest, authorization: str | None = Header(default=None)):
    """OpenAI-compatible endpoint so ElevenLabs' Conversational AI platform can
    use this exact agent (Claude + tools.py + guardrails.py, unchanged) as its
    real-time voice call's "Custom LLM" brain -- ElevenLabs handles STT/TTS/
    turn-taking; we only ever return final text, streamed as SSE."""
    if not VOICE_LLM_SHARED_SECRET:
        return JSONResponse(status_code=503, content={"error": "voice call mode is not configured"})
    if authorization != f"Bearer {VOICE_LLM_SHARED_SECRET}":
        return JSONResponse(status_code=401, content={"error": "invalid or missing credentials"})

    try:
        history, user_message = voice_llm.openai_messages_to_history(req.messages)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})

    return StreamingResponse(voice_llm.stream_reply(client, history, user_message), media_type="text/event-stream")


@app.get("/voice-call/signed-url", response_model=None)
def voice_call_signed_url():
    """Short-lived signed URL the browser uses to start a real-time voice call
    with our ElevenLabs Agent -- keeps ELEVENLABS_API_KEY server-side; the
    browser only ever sees a URL that expires in 15 minutes."""
    try:
        return JSONResponse(content={"signed_url": voice_llm.get_signed_url()})
    except RuntimeError as exc:
        message = str(exc)
        if "must both be set" in message:
            return JSONResponse(status_code=503, content={"error": "voice call mode is not configured"})
        return JSONResponse(status_code=502, content={"error": f"failed to get signed url: {message}"})


@app.get("/sessions", response_model=None)
def list_sessions() -> JSONResponse:
    """List saved conversations, newest first, for the History panel."""
    return JSONResponse(content={"sessions": sessions.list_sessions()})


@app.get("/sessions/{session_id}", response_model=None)
def get_session(session_id: str) -> JSONResponse:
    """Return one saved conversation's display messages for resuming."""
    if not sessions.is_valid_session_id(session_id):
        return JSONResponse(status_code=400, content={"error": "invalid session_id"})
    messages = sessions.get_display_messages(session_id)
    if messages is None:
        return JSONResponse(status_code=404, content={"error": "session not found"})
    return JSONResponse(content={"session_id": session_id, "messages": messages})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
