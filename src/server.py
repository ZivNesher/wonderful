from __future__ import annotations
import os
from dotenv import load_dotenv
load_dotenv()
import uuid
from pathlib import Path
import anthropic
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel
import agent
import sessions
import tts

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


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class ChatResponse(BaseModel):
    reply: str
    session_id: str
    used_web_search: bool = False


class TTSRequest(BaseModel):
    text: str


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


@app.post("/tts", response_model=None)
def text_to_speech(req: TTSRequest):
    """Synthesize speech for voice mode's spoken replies; 503 if unconfigured
    so the frontend can fall back to the browser's own speechSynthesis."""
    text = (req.text or "").strip()
    if not text:
        return JSONResponse(status_code=400, content={"error": "text must not be empty"})
    if len(text) > 2000:
        return JSONResponse(status_code=400, content={"error": "text is too long (2000 char max)"})
    if not tts.is_configured():
        return JSONResponse(status_code=503, content={"error": "voice output is not configured"})
    try:
        audio = tts.synthesize_speech(text)
    except Exception as exc:  # noqa: BLE001 -- turned into a clean error, never a raw 500
        return JSONResponse(status_code=502, content={"error": f"speech synthesis failed: {exc}"})
    return Response(content=audio, media_type="audio/mpeg")


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
