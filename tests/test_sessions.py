"""Tests for local, file-based conversation persistence (sessions.py). Fully
offline/deterministic -- uses a temp directory in place of the real .sessions/
so these never touch or depend on real saved conversations.
"""
import uuid

import pytest

import sessions


@pytest.fixture(autouse=True)
def _isolated_sessions_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "SESSIONS_DIR", tmp_path)


def _uid() -> str:
    return str(uuid.uuid4())


def test_is_valid_session_id():
    assert sessions.is_valid_session_id(str(uuid.uuid4())) is True
    assert sessions.is_valid_session_id("../../etc/passwd") is False
    assert sessions.is_valid_session_id("not-a-uuid") is False
    assert sessions.is_valid_session_id("") is False
    assert sessions.is_valid_session_id(None) is False


def test_save_and_load_round_trip_preserves_dict_and_object_like_content():
    sid = _uid()
    messages = [
        {"role": "user", "content": "how many passengers does SFO handle?"},
        {"role": "assistant", "content": [{"type": "text", "text": "About 17.6 million."}]},
    ]
    sessions.save(sid, messages)
    reloaded = sessions.load_messages(sid)
    assert reloaded == messages


def test_save_rejects_invalid_session_id():
    with pytest.raises(ValueError):
        sessions.save("../../etc/passwd", [{"role": "user", "content": "hi"}])


def test_load_messages_unknown_session_returns_empty_list():
    assert sessions.load_messages(_uid()) == []


def test_load_messages_invalid_session_id_returns_empty_list_without_touching_disk():
    assert sessions.load_messages("../not-a-real-id") == []


def test_auto_title_uses_first_user_message_truncated():
    sid = _uid()
    long_question = "x" * 100
    sessions.save(sid, [{"role": "user", "content": long_question}])
    listed = sessions.list_sessions()
    assert len(listed) == 1
    assert listed[0]["title"].endswith("...")
    assert len(listed[0]["title"]) == sessions.TITLE_MAX_CHARS + 3


def test_list_sessions_sorted_newest_first(monkeypatch):
    sid_old, sid_new = _uid(), _uid()
    times = iter([100.0, 100.0, 200.0, 200.0])  # created_at, updated_at per save() call
    monkeypatch.setattr(sessions.time, "time", lambda: next(times))
    sessions.save(sid_old, [{"role": "user", "content": "first conversation"}])
    sessions.save(sid_new, [{"role": "user", "content": "second conversation"}])

    listed = sessions.list_sessions()
    assert [s["session_id"] for s in listed] == [sid_new, sid_old]


def test_get_display_messages_unknown_session_returns_none():
    assert sessions.get_display_messages(_uid()) is None


def test_get_display_messages_reconstructs_simple_turn():
    sid = _uid()
    sessions.save(
        sid,
        [
            {"role": "user", "content": "how many passengers does SFO handle?"},
            {"role": "assistant", "content": [{"type": "text", "text": "About 17.6 million."}]},
        ],
    )
    display = sessions.get_display_messages(sid)
    assert display == [
        {"role": "user", "text": "how many passengers does SFO handle?"},
        {"role": "assistant", "text": "About 17.6 million.", "used_web_search": False},
    ]


def test_get_display_messages_skips_tool_round_trip_and_uses_final_text():
    sid = _uid()
    sessions.save(
        sid,
        [
            {"role": "user", "content": "SFO traffic?"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "get_traffic_stats", "input": {"code": "SFO"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "{...}"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "SFO handled 17.6M passengers."}]},
        ],
    )
    display = sessions.get_display_messages(sid)
    assert display == [
        {"role": "user", "text": "SFO traffic?"},
        {"role": "assistant", "text": "SFO handled 17.6M passengers.", "used_web_search": False},
    ]


def test_get_display_messages_flags_web_search_and_multi_turn():
    sid = _uid()
    sessions.save(
        sid,
        [
            {"role": "user", "content": "why is SFO congested?"},
            {"role": "assistant", "content": [{"type": "server_tool_use", "name": "web_search"}, {"type": "text", "text": "Runway geometry is the constraint."}]},
            {"role": "user", "content": "and ANC?"},
            {"role": "assistant", "content": [{"type": "text", "text": "ANC is less constrained."}]},
        ],
    )
    display = sessions.get_display_messages(sid)
    assert display == [
        {"role": "user", "text": "why is SFO congested?"},
        {"role": "assistant", "text": "Runway geometry is the constraint.", "used_web_search": True},
        {"role": "user", "text": "and ANC?"},
        {"role": "assistant", "text": "ANC is less constrained.", "used_web_search": False},
    ]
