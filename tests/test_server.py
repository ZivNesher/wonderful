"""HTTP-layer tests for server.py -- request validation and session handling.
`agent.run_turn` is monkeypatched so this never calls the real API or needs a key.
Conversation persistence (sessions.py) is redirected to a temp directory so
these tests never touch or depend on real saved conversations.
"""
import os

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-dummy-for-import-only")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import agent  # noqa: E402
import server  # noqa: E402
import sessions  # noqa: E402
import tts  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_sessions_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "SESSIONS_DIR", tmp_path)


def _fake_run_turn(client, history, message):
    reply = f"echo: {message}"
    updated = history + [{"role": "user", "content": message}, {"role": "assistant", "content": [{"type": "text", "text": reply}]}]
    return reply, updated, False


def test_empty_message_rejected(monkeypatch):
    monkeypatch.setattr(agent, "run_turn", _fake_run_turn)
    with TestClient(server.app) as c:
        resp = c.post("/chat", json={"message": "   "})
    assert resp.status_code == 400


def test_too_long_message_rejected(monkeypatch):
    monkeypatch.setattr(agent, "run_turn", _fake_run_turn)
    with TestClient(server.app) as c:
        resp = c.post("/chat", json={"message": "x" * 2001})
    assert resp.status_code == 400


def test_valid_message_returns_reply_and_session_id(monkeypatch):
    monkeypatch.setattr(agent, "run_turn", _fake_run_turn)
    with TestClient(server.app) as c:
        resp = c.post("/chat", json={"message": "hello"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["reply"] == "echo: hello"
    assert body["session_id"]
    assert body["used_web_search"] is False


def test_used_web_search_flag_passed_through_to_response(monkeypatch):
    def _fake_run_turn_with_search(client, history, message):
        return "found via search", history + [{"role": "user", "content": message}], True

    monkeypatch.setattr(agent, "run_turn", _fake_run_turn_with_search)
    with TestClient(server.app) as c:
        resp = c.post("/chat", json={"message": "why is SFO congested?"})
    assert resp.json()["used_web_search"] is True


def test_session_history_persists_across_calls(monkeypatch):
    seen_histories = []

    def _recording_run_turn(client, history, message):
        seen_histories.append(list(history))
        return _fake_run_turn(client, history, message)

    monkeypatch.setattr(agent, "run_turn", _recording_run_turn)
    with TestClient(server.app) as c:
        first = c.post("/chat", json={"message": "first"}).json()
        c.post("/chat", json={"message": "second", "session_id": first["session_id"]})

    assert seen_histories[0] == []  # first call: no prior history
    assert len(seen_histories[1]) == 2  # second call: sees the first turn's history


def test_session_survives_server_restart(monkeypatch):
    """The whole point of moving off the old in-memory dict: history must be
    readable by a fresh server process, not just within one running instance."""
    monkeypatch.setattr(agent, "run_turn", _fake_run_turn)
    with TestClient(server.app) as c:
        session_id = c.post("/chat", json={"message": "first"}).json()["session_id"]

    seen_histories = []

    def _recording_run_turn(client, history, message):
        seen_histories.append(list(history))
        return _fake_run_turn(client, history, message)

    monkeypatch.setattr(agent, "run_turn", _recording_run_turn)
    with TestClient(server.app) as c:  # a new TestClient/app lifespan == "restart"
        c.post("/chat", json={"message": "second", "session_id": session_id})

    assert len(seen_histories[0]) == 2  # loaded from disk, not memory


def test_invalid_session_id_rejected_on_chat(monkeypatch):
    monkeypatch.setattr(agent, "run_turn", _fake_run_turn)
    with TestClient(server.app) as c:
        resp = c.post("/chat", json={"message": "hi", "session_id": "../../etc/passwd"})
    assert resp.status_code == 400


def test_list_sessions_empty_initially():
    with TestClient(server.app) as c:
        resp = c.get("/sessions")
    assert resp.status_code == 200
    assert resp.json() == {"sessions": []}


def test_list_sessions_after_a_conversation(monkeypatch):
    monkeypatch.setattr(agent, "run_turn", _fake_run_turn)
    with TestClient(server.app) as c:
        session_id = c.post("/chat", json={"message": "how many passengers does SFO handle?"}).json()["session_id"]
        resp = c.get("/sessions")
    listed = resp.json()["sessions"]
    assert len(listed) == 1
    assert listed[0]["session_id"] == session_id
    assert "SFO" in listed[0]["title"]


def test_get_session_returns_display_messages(monkeypatch):
    monkeypatch.setattr(agent, "run_turn", _fake_run_turn)
    with TestClient(server.app) as c:
        session_id = c.post("/chat", json={"message": "how many passengers does SFO handle?"}).json()["session_id"]
        resp = c.get(f"/sessions/{session_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == session_id
    assert body["messages"][0] == {"role": "user", "text": "how many passengers does SFO handle?"}
    assert body["messages"][1]["role"] == "assistant"


def test_get_session_not_found():
    import uuid

    with TestClient(server.app) as c:
        resp = c.get(f"/sessions/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_get_session_invalid_id_rejected():
    with TestClient(server.app) as c:
        resp = c.get("/sessions/../../etc/passwd")
    assert resp.status_code in (400, 404)  # FastAPI routing may itself normalize/404 a path-traversal id


def test_tts_empty_text_rejected():
    with TestClient(server.app) as c:
        resp = c.post("/tts", json={"text": "   "})
    assert resp.status_code == 400


def test_tts_too_long_text_rejected():
    with TestClient(server.app) as c:
        resp = c.post("/tts", json={"text": "x" * 2001})
    assert resp.status_code == 400


def test_tts_unconfigured_returns_503(monkeypatch):
    monkeypatch.setattr(tts, "API_KEY", None)
    with TestClient(server.app) as c:
        resp = c.post("/tts", json={"text": "hello"})
    assert resp.status_code == 503


def test_tts_success_returns_audio(monkeypatch):
    monkeypatch.setattr(tts, "API_KEY", "sk-fake-key")
    monkeypatch.setattr(tts, "synthesize_speech", lambda text: b"fake-mp3-bytes")
    with TestClient(server.app) as c:
        resp = c.post("/tts", json={"text": "hello"})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/mpeg"
    assert resp.content == b"fake-mp3-bytes"


def test_tts_upstream_failure_returns_502(monkeypatch):
    monkeypatch.setattr(tts, "API_KEY", "sk-fake-key")

    def _boom(text):
        raise RuntimeError("upstream exploded")

    monkeypatch.setattr(tts, "synthesize_speech", _boom)
    with TestClient(server.app) as c:
        resp = c.post("/tts", json={"text": "hello"})
    assert resp.status_code == 502


def test_dotenv_loaded_before_project_modules_import_in_source_order():
    """Regression test for a real bug: tts.py (and agent.py) read an env var
    as a module-level constant at import time, so server.py must call
    load_dotenv() textually before importing tts/agent/sessions -- otherwise
    a correctly-configured ELEVENLABS_API_KEY in .env is silently ignored
    (tts.is_configured() stays False, /tts always 503 even with a valid key
    file sitting right there).

    Checked as source order, not by spawning a process and letting
    load_dotenv() auto-discover a .env file: python-dotenv's default
    find_dotenv() (usecwd=False) locates the *real* server.py via frame
    introspection and walks up from there regardless of a test's cwd or
    sys.path tricks -- an earlier version of this test spawned a subprocess
    expecting isolation and got none, silently validating against this
    repo's real .env instead of a fake one, so it passed even with the bug
    reintroduced. Source-order is the actual invariant that matters here and
    has no such escape hatch.
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "src" / "server.py").read_text()
    load_dotenv_pos = source.index("load_dotenv()")
    for module_import in ("import tts", "import agent", "import sessions"):
        assert load_dotenv_pos < source.index(module_import), (
            f"{module_import!r} appears before load_dotenv() in server.py -- "
            "its module-level env var reads would silently miss .env"
        )
