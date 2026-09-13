"""HTTP-layer tests for server.py -- request validation and session handling.
`agent.run_turn` is monkeypatched so this never calls the real API or needs a key.
Conversation persistence (sessions.py) is redirected to a temp directory so
these tests never touch or depend on real saved conversations.
"""
import os

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-dummy-for-import-only")
os.environ.setdefault("BTS_SOCRATA_APP_TOKEN", "test-dummy-for-import-only")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import agent  # noqa: E402
import server  # noqa: E402
import sessions  # noqa: E402
import voice_llm  # noqa: E402


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


def test_voice_llm_unconfigured_returns_503(monkeypatch):
    monkeypatch.setattr(server, "VOICE_LLM_SHARED_SECRET", None)
    with TestClient(server.app) as c:
        resp = c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 503


def test_voice_llm_missing_auth_rejected(monkeypatch):
    monkeypatch.setattr(server, "VOICE_LLM_SHARED_SECRET", "test-secret")
    with TestClient(server.app) as c:
        resp = c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 401


def test_voice_llm_wrong_auth_rejected(monkeypatch):
    monkeypatch.setattr(server, "VOICE_LLM_SHARED_SECRET", "test-secret")
    with TestClient(server.app) as c:
        resp = c.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "Bearer wrong-secret"},
        )
    assert resp.status_code == 401


def test_voice_llm_valid_request_streams_openai_compatible_sse(monkeypatch):
    monkeypatch.setattr(server, "VOICE_LLM_SHARED_SECRET", "test-secret")
    monkeypatch.setattr(agent, "run_turn", _fake_run_turn)
    with TestClient(server.app) as c:
        resp = c.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "system", "content": "ignored"}, {"role": "user", "content": "hello"}]},
            headers={"Authorization": "Bearer test-secret"},
        )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert "echo: hello" in resp.text
    assert resp.text.rstrip().endswith("data: [DONE]")


def test_voice_llm_sends_filler_chunk_before_the_real_answer(monkeypatch):
    """The filler must arrive as its own early chunk, before the (potentially
    slow, tool-calling) real answer -- that's the whole latency fix: real
    bytes flow immediately so ElevenLabs' Custom LLM integration doesn't
    time out and retry/fail the call while agent.run_turn is still working."""
    monkeypatch.setattr(server, "VOICE_LLM_SHARED_SECRET", "test-secret")
    monkeypatch.setattr(agent, "run_turn", _fake_run_turn)
    with TestClient(server.app) as c:
        resp = c.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hello"}]},
            headers={"Authorization": "Bearer test-secret"},
        )
    filler_pos = resp.text.find("Let me check on that.")
    answer_pos = resp.text.find("echo: hello")
    assert filler_pos != -1 and answer_pos != -1
    assert filler_pos < answer_pos


def test_voice_llm_sends_keepalive_chunks_during_a_slow_turn(monkeypatch):
    """Regression test: a real call was observed dropping mid-wait even after
    the filler fixed the 'no response at all' case -- one long silent gap
    between chunks was *also* enough for ElevenLabs to hang up. The stream
    must keep producing bytes the whole time agent.run_turn is still running,
    not just once at the start."""
    import time

    monkeypatch.setattr(server, "VOICE_LLM_SHARED_SECRET", "test-secret")
    monkeypatch.setattr(voice_llm, "KEEPALIVE_INTERVAL_SECONDS", 0.05)

    def _slow_run_turn(client, history, message):
        time.sleep(0.2)
        return _fake_run_turn(client, history, message)

    monkeypatch.setattr(agent, "run_turn", _slow_run_turn)
    with TestClient(server.app) as c:
        resp = c.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hello"}]},
            headers={"Authorization": "Bearer test-secret"},
        )
    # keepalive chunks carry a single-space content delta (not an empty
    # delta -- see stream_reply's docstring for why) -- count them distinctly
    # from the filler/real-answer/stop chunks.
    keepalive_count = resp.text.count('"content": " "')
    assert keepalive_count >= 2
    assert "echo: hello" in resp.text


def test_voice_llm_uses_full_message_history_not_just_latest(monkeypatch):
    """ElevenLabs resends the whole conversation each call (it's stateless on our
    side) -- everything but the latest user message must become `history`."""
    monkeypatch.setattr(server, "VOICE_LLM_SHARED_SECRET", "test-secret")
    seen = {}

    def _recording_run_turn(client, history, message):
        seen["history"] = history
        seen["message"] = message
        return "ok", history, False

    monkeypatch.setattr(agent, "run_turn", _recording_run_turn)
    with TestClient(server.app) as c:
        c.post(
            "/v1/chat/completions",
            json={
                "messages": [
                    {"role": "user", "content": "first"},
                    {"role": "assistant", "content": "first reply"},
                    {"role": "user", "content": "second"},
                ]
            },
            headers={"Authorization": "Bearer test-secret"},
        )
    assert seen["message"] == "second"
    assert seen["history"] == [{"role": "user", "content": "first"}, {"role": "assistant", "content": "first reply"}]


def test_voice_llm_rejects_history_not_ending_in_user_message(monkeypatch):
    monkeypatch.setattr(server, "VOICE_LLM_SHARED_SECRET", "test-secret")
    with TestClient(server.app) as c:
        resp = c.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "assistant", "content": "nothing to reply to"}]},
            headers={"Authorization": "Bearer test-secret"},
        )
    assert resp.status_code == 400


def test_voice_call_signed_url_unconfigured_returns_503(monkeypatch):
    monkeypatch.setattr(voice_llm, "get_signed_url", lambda: (_ for _ in ()).throw(RuntimeError("must both be set")))
    with TestClient(server.app) as c:
        resp = c.get("/voice-call/signed-url")
    assert resp.status_code == 503


def test_voice_call_signed_url_success(monkeypatch):
    monkeypatch.setattr(voice_llm, "get_signed_url", lambda: "wss://fake-signed-url")
    with TestClient(server.app) as c:
        resp = c.get("/voice-call/signed-url")
    assert resp.status_code == 200
    assert resp.json() == {"signed_url": "wss://fake-signed-url"}


def test_voice_call_signed_url_upstream_failure_returns_502(monkeypatch):
    monkeypatch.setattr(voice_llm, "get_signed_url", lambda: (_ for _ in ()).throw(RuntimeError("ElevenLabs 404: agent not found")))
    with TestClient(server.app) as c:
        resp = c.get("/voice-call/signed-url")
    assert resp.status_code == 502


def test_dotenv_loaded_before_project_modules_import_in_source_order():
    """Regression test for a real bug: a module that reads an env var as a
    module-level constant at import time needs load_dotenv() to have already
    run, or a correctly-configured .env value is silently ignored (this bit
    us for real with tts.py before it was removed).

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
    for module_import in ("import agent", "import sessions"):
        assert load_dotenv_pos < source.index(module_import), (
            f"{module_import!r} appears before load_dotenv() in server.py -- "
            "its module-level env var reads would silently miss .env"
        )
