from __future__ import annotations

import json
import os

import anthropic

import tools
from guardrails import SYSTEM_PROMPT

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")
MAX_TOKENS = 2048
MAX_TOOL_ROUNDS = 6  # hard cap so a confused loop can't run away
MAX_PAUSE_RESUMES = 3  # server-side web-search loop hits pause_turn at 10 internal
# iterations; this bounds how many times we resend to let it continue before
# failing closed, mirroring MAX_TOOL_ROUNDS' purpose for client-side tools.

TOOLS = [
    {
        "name": "lookup_airport",
        "description": (
            "Resolve free text (an airport code, city, or airport name) to a "
            "whitelisted US commercial airport. Use this whenever the user names "
            "a place rather than a 3-letter code, or to disambiguate."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "City, airport name, or code, e.g. 'Anchorage' or 'SFO'"}},
            "required": ["query"],
        },
    },
    {
        "name": "get_airport_profile",
        "description": "Get metadata and runway summary for one whitelisted airport by IATA or ICAO code.",
        "input_schema": {
            "type": "object",
            "properties": {"code": {"type": "string", "description": "3-letter IATA or 4-letter ICAO code, e.g. 'SFO' or 'KSFO'"}},
            "required": ["code"],
        },
    },
    {
        "name": "list_airports_in_region",
        "description": (
            "List whitelisted airports in a known US region grouping (e.g. 'New England', "
            "'Southeast', 'Pacific Northwest'). Returns 'unknown_region' with the known list "
            "if the region isn't recognized -- ask the user to clarify rather than guessing states."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"region": {"type": "string"}},
            "required": ["region"],
        },
    },
    {
        "name": "get_traffic_stats",
        "description": (
            "Get current-year BTS T-100 traffic totals (passengers, departures, arrivals, "
            "enplanements) for one whitelisted airport. This is the default tool for a plain "
            "factual question about one airport's traffic/congestion/demand -- prefer it over "
            "score_airport whenever the user isn't asking for a ranking or recommendation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
        },
    },
    {
        "name": "score_airport",
        "description": (
            "Deterministic expansion-candidacy scoring (traffic/capacity-pressure/utilization "
            "percentiles, composite score, long-haul route share) for one whitelisted airport. "
            "Call this ONLY when the analyst explicitly asks for a ranking, a comparison between "
            "multiple airports, or an investment/expansion recommendation (e.g. 'candidate for "
            "expansion', 'should we invest', 'rank/compare these'). Do not call it for a plain "
            "factual question about one airport's traffic, congestion, or demand -- use "
            "get_traffic_stats/get_airport_profile for that instead, and never estimate these "
            "numbers yourself if you do need them."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
        },
    },
    # Native, Anthropic-hosted server-side tool
    {"type": "web_search_20260209", "name": "web_search", "max_uses": 3},
]

_DISPATCH = {
    "lookup_airport": lambda i: tools.lookup_airport(i.get("query", "")),
    "get_airport_profile": lambda i: tools.get_airport_profile(i.get("code", "")),
    "list_airports_in_region": lambda i: tools.list_airports_in_region(i.get("region", "")),
    "get_traffic_stats": lambda i: tools.get_traffic_stats(i.get("code", "")),
    "score_airport": lambda i: tools.score_airport(i.get("code", "")),
}


def _run_tool(name: str, tool_input: dict) -> dict:
    """Dispatch a client-side tool_use block to its handler, failing closed on an unknown name."""
    handler = _DISPATCH.get(name)
    if handler is None:
        # The model can only see the 5 declared tools, so this means the SDK/API
        # sent something unexpected -- fail closed rather than guess.
        return {"status": "error", "reason": f"unknown tool: {name}"}
    try:
        return handler(tool_input)
    except Exception as exc:  # noqa: BLE001 -- surfaced to the model as tool data, not raised
        return {"status": "error", "reason": str(exc)}


def _create(client: anthropic.Anthropic, messages: list[dict]):
    """Send one Messages API request with the fixed model, tools, and system prompt."""
    return client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        tools=TOOLS,
        messages=messages,
    )


def _used_web_search(content) -> bool:
    """True if this response content contains a server-side web_search block."""
    return any(getattr(block, "type", None) in ("server_tool_use", "web_search_tool_result") for block in content)


def run_turn(client: anthropic.Anthropic, history: list[dict], user_message: str) -> tuple[str, list[dict], bool]:
    """Run one user turn to completion, including any tool round-trips.

    Returns (reply_text, updated_history, used_web_search).
    """
    messages = history + [{"role": "user", "content": user_message}]
    used_web_search = False

    for _ in range(MAX_TOOL_ROUNDS):
        response = _create(client, messages)
        used_web_search = used_web_search or _used_web_search(response.content)

        # web_search runs its own server-side loop (search, read, maybe search
        # again) and only hands control back to us at end_turn, a client
        # tool_use request, or pause_turn if it hit the server's internal
        # iteration cap. pause_turn resumes by resending the same history
        # unchanged -- no new user message, no tool_result (see SKILL.md-
        # aligned docs: "the API detects the trailing server_tool_use block").
        resumes = 0
        while response.stop_reason == "pause_turn" and resumes < MAX_PAUSE_RESUMES:
            messages.append({"role": "assistant", "content": response.content})
            response = _create(client, messages)
            used_web_search = used_web_search or _used_web_search(response.content)
            resumes += 1

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "pause_turn":
            # Still paused after MAX_PAUSE_RESUMES -- fail closed rather than
            # return a silently truncated answer.
            return (
                "I hit my web-search iteration limit before finishing that lookup. "
                "Could you narrow the question?",
                messages,
                used_web_search,
            )

        if response.stop_reason != "tool_use":
            reply_text = "".join(block.text for block in response.content if block.type == "text")
            return reply_text, messages, used_web_search

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            result = _run_tool(block.name, block.input)
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                }
            )
        messages.append({"role": "user", "content": tool_results})

    # Hit MAX_TOOL_ROUNDS without a final answer -- fail closed with a plain
    # message rather than silently returning nothing.
    return (
        "I wasn't able to finish gathering data for that within my tool-call budget. "
        "Could you narrow the question (e.g. one airport or region at a time)?",
        messages,
        used_web_search,
    )
