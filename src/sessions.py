from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

SESSIONS_DIR = Path(__file__).resolve().parent.parent / ".sessions"
TITLE_MAX_CHARS = 60


def is_valid_session_id(session_id: str) -> bool:
    """True iff session_id parses as a real uuid4 -- the whitelist check every
    function below relies on before touching the filesystem."""
    try:
        uuid.UUID(session_id)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _path(session_id: str) -> Path | None:
    """The on-disk path for a session, or None if the ID is invalid."""
    if not is_valid_session_id(session_id):
        return None
    return SESSIONS_DIR / f"{session_id}.json"


def _serialize_block(block):
    """Turn one content block (SDK object or plain dict) into a JSON-safe dict."""
    if isinstance(block, dict):
        return block
    if hasattr(block, "model_dump"):
        return block.model_dump(mode="json")
    return block


def _serialize_content(content):
    """Turn one message's content (a string or list of blocks) into JSON-safe form."""
    if isinstance(content, str):
        return content
    return [_serialize_block(b) for b in content]


def _auto_title(messages: list[dict]) -> str:
    """Derive a short conversation title from the first user message."""
    for m in messages:
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            text = m["content"].strip()
            if len(text) > TITLE_MAX_CHARS:
                return text[:TITLE_MAX_CHARS].rstrip() + "..."
            return text or "New conversation"
    return "New conversation"


def _read_raw(session_id: str) -> dict | None:
    """Load one session's full JSON record from disk, or None if missing/invalid."""
    path = _path(session_id)
    if path is None or not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def save(session_id: str, messages: list[dict]) -> None:
    """Persist the full raw message list (as agent.run_turn returns it) so a
    later load_messages() can feed it straight back into the API unchanged."""
    path = _path(session_id)
    if path is None:
        raise ValueError(f"refusing to save an invalid session_id: {session_id!r}")

    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    existing = _read_raw(session_id)
    created_at = existing["created_at"] if existing else time.time()
    serialized = [{"role": m["role"], "content": _serialize_content(m["content"])} for m in messages]
    path.write_text(
        json.dumps(
            {
                "session_id": session_id,
                "title": _auto_title(messages),
                "created_at": created_at,
                "updated_at": time.time(),
                "messages": serialized,
            }
        )
    )


def load_messages(session_id: str) -> list[dict]:
    """Raw message list, ready to pass straight into agent.run_turn as history."""
    raw = _read_raw(session_id)
    return raw["messages"] if raw else []


def list_sessions() -> list[dict]:
    """{session_id, title, updated_at} for every saved session, newest first."""
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    sessions = []
    for path in SESSIONS_DIR.glob("*.json"):
        try:
            raw = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        sessions.append(
            {"session_id": raw["session_id"], "title": raw["title"], "updated_at": raw["updated_at"]}
        )
    sessions.sort(key=lambda s: s["updated_at"], reverse=True)
    return sessions


def get_display_messages(session_id: str) -> list[dict] | None:
    """Reconstruct {role, text, used_web_search} per turn for the History panel;
    None if the session doesn't exist."""
    raw = _read_raw(session_id)
    if raw is None:
        return None

    raw_messages = raw["messages"]
    display: list[dict] = []
    i, n = 0, len(raw_messages)

    def is_user_text(msg) -> bool:
        return msg.get("role") == "user" and isinstance(msg.get("content"), str)

    while i < n:
        msg = raw_messages[i]
        if not is_user_text(msg):
            i += 1  # stray message not starting a turn -- shouldn't normally happen
            continue

        display.append({"role": "user", "text": msg["content"]})
        i += 1

        assistant_texts: list[str] = []
        used_web_search = False
        while i < n and not is_user_text(raw_messages[i]):
            turn_msg = raw_messages[i]
            if turn_msg.get("role") == "assistant":
                blocks = turn_msg.get("content") or []
                if any(isinstance(b, dict) and b.get("type") in ("server_tool_use", "web_search_tool_result") for b in blocks):
                    used_web_search = True
                text_blocks = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
                if text_blocks:
                    assistant_texts = text_blocks  # last assistant message's text wins, matching run_turn
            i += 1

        display.append(
            {"role": "assistant", "text": "".join(assistant_texts), "used_web_search": used_web_search}
        )

    return display
