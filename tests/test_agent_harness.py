"""Tests the tool-calling loop mechanics in agent.py against a scripted fake
Anthropic client -- no network, no API key, fully deterministic. This proves
the harness itself (tool dispatch, loop termination, unknown-tool handling)
behaves correctly; it does NOT prove the model chooses to refuse jailbreaks or
phrases answers well -- that requires a live model and is covered separately by
tests/test_live_eval.py (skipped unless ANTHROPIC_API_KEY is set). See README
"How to run the evaluation tests" for why the split exists.
"""
import agent


class FakeTextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class FakeToolUseBlock:
    type = "tool_use"

    def __init__(self, name, input, id):  # noqa: A002 -- matches SDK field name
        self.name = name
        self.input = input
        self.id = id


class FakeServerToolUseBlock:
    """Stands in for the block type the real API emits when web_search (or any
    server-side tool) runs -- no client dispatch, just a marker in the content
    stream that agent.py's used_web_search detection looks for."""

    type = "server_tool_use"

    def __init__(self, name="web_search"):
        self.name = name


class FakeMessage:
    def __init__(self, content, stop_reason):
        self.content = content
        self.stop_reason = stop_reason


class FakeMessagesAPI:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


class FakeClient:
    def __init__(self, responses):
        self.messages = FakeMessagesAPI(responses)


def test_direct_answer_no_tool_use():
    client = FakeClient([FakeMessage([FakeTextBlock("Hello there.")], "end_turn")])
    reply, history, used_web_search = agent.run_turn(client, [], "hi")
    assert reply == "Hello there."
    assert history[0] == {"role": "user", "content": "hi"}
    assert used_web_search is False


def test_single_tool_round_trip_calls_real_tool_and_returns_final_text():
    responses = [
        FakeMessage([FakeToolUseBlock("get_airport_profile", {"code": "SFO"}, "call_1")], "tool_use"),
        FakeMessage([FakeTextBlock("SFO is a large hub.")], "end_turn"),
    ]
    client = FakeClient(responses)
    reply, history, used_web_search = agent.run_turn(client, [], "tell me about SFO")
    assert reply == "SFO is a large hub."
    assert used_web_search is False

    tool_result_messages = [m for m in history if m["role"] == "user" and isinstance(m["content"], list)]
    assert len(tool_result_messages) == 1
    result_block = tool_result_messages[0]["content"][0]
    assert result_block["tool_use_id"] == "call_1"
    assert '"status": "ok"' in result_block["content"]  # real tools.py ran, SFO is whitelisted


def test_unknown_tool_name_fails_closed_without_crashing():
    responses = [
        FakeMessage([FakeToolUseBlock("delete_everything", {}, "call_1")], "tool_use"),
        FakeMessage([FakeTextBlock("ok")], "end_turn"),
    ]
    client = FakeClient(responses)
    reply, history, _ = agent.run_turn(client, [], "do something else")
    assert reply == "ok"
    tool_result_messages = [m for m in history if m["role"] == "user" and isinstance(m["content"], list)]
    assert "unknown tool" in tool_result_messages[0]["content"][0]["content"]


def test_max_tool_rounds_cutoff_fails_closed():
    responses = [
        FakeMessage([FakeToolUseBlock("lookup_airport", {"query": "SFO"}, f"call_{i}")], "tool_use")
        for i in range(agent.MAX_TOOL_ROUNDS)
    ]
    client = FakeClient(responses)
    reply, _, _ = agent.run_turn(client, [], "keep looping")
    assert "tool-call budget" in reply
    assert len(client.messages.calls) == agent.MAX_TOOL_ROUNDS


def test_web_search_use_is_flagged_and_not_dispatched_as_a_client_tool():
    # server_tool_use blocks must NOT hit _run_tool (there's no "web_search"
    # entry in _DISPATCH) -- only real tool_use blocks are dispatched.
    responses = [
        FakeMessage([FakeServerToolUseBlock(), FakeTextBlock("Per a recent FAA notice, ...")], "end_turn"),
    ]
    client = FakeClient(responses)
    reply, _, used_web_search = agent.run_turn(client, [], "why is SFO congested?")
    assert reply == "Per a recent FAA notice, ..."
    assert used_web_search is True


def test_pause_turn_resumes_by_resending_unchanged_history():
    responses = [
        FakeMessage([FakeServerToolUseBlock()], "pause_turn"),
        FakeMessage([FakeServerToolUseBlock(), FakeTextBlock("Found it.")], "end_turn"),
    ]
    client = FakeClient(responses)
    reply, history, used_web_search = agent.run_turn(client, [], "deep dive on SFO")
    assert reply == "Found it."
    assert used_web_search is True
    assert len(client.messages.calls) == 2
    # Resume call must NOT add a new user/tool_result message -- same history,
    # just the paused assistant turn appended (per the real API's resume contract).
    resumed_messages = client.messages.calls[1]["messages"]
    assert resumed_messages[-1]["role"] == "assistant"


def test_pause_turn_exhausting_max_resumes_fails_closed():
    responses = [FakeMessage([FakeServerToolUseBlock()], "pause_turn") for _ in range(agent.MAX_PAUSE_RESUMES + 1)]
    client = FakeClient(responses)
    reply, _, used_web_search = agent.run_turn(client, [], "keep searching forever")
    assert "iteration limit" in reply
    assert used_web_search is True
    assert len(client.messages.calls) == agent.MAX_PAUSE_RESUMES + 1
