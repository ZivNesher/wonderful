from __future__ import annotations

import json
import os

import anthropic

import tools
from guardrails import SYSTEM_PROMPT

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")
MAX_TOKENS = 2048
MAX_TOOL_ROUNDS = 6
MAX_PAUSE_RESUMES = 3  # web_search's server-side loop caps at 10 iterations before pausing

_KNOWN_REGIONS = ", ".join(f"'{r.title()}'" for r in tools.REGIONS)

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
            f"List whitelisted airports in a known US region grouping. The only valid regions "
            f"are: {_KNOWN_REGIONS} -- these are state groupings, not individual states (e.g. "
            f"Texas is part of 'Southwest'). Never suggest a region name outside this exact list. "
            "Returns 'unknown_region' with the known list if the region isn't recognized -- ask "
            "the user to clarify rather than guessing states."
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
            "numbers yourself if you do need them. "
            "Optional `weights` arg runs a custom what-if sensitivity scenario ALONGSIDE the "
            "default score (never replacing it): pass any of traffic/capacity_pressure/"
            "utilization as numbers, e.g. {'capacity_pressure': 2} to double the weight of "
            "congestion. Any KPI you don't mention defaults to 1 (equal weighting), so 'double "
            "congestion' means {'capacity_pressure': 2} with traffic and utilization implicitly "
            "1 each. Call this once per airport you're re-ranking, with the same weights each "
            "time. To return to the normal ranking on a later question, just call it again with "
            "no `weights` arg -- there is no separate reset step."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "weights": {
                    "type": "object",
                    "description": (
                        "Optional custom KPI weights for a sensitivity scenario, e.g. "
                        "{'capacity_pressure': 2}. Omit entirely for the default score."
                    ),
                    "properties": {
                        "traffic": {"type": "number"},
                        "capacity_pressure": {"type": "number"},
                        "utilization": {"type": "number"},
                    },
                },
            },
            "required": ["code"],
        },
    },
    {
        "name": "rank_airports",
        "description": (
            "Deterministically rank 2+ whitelisted airports by expansion_candidacy_score, with "
            "exact rank numbers and ties computed in code (standard competition ranking: ties "
            "share a rank, the next distinct rank skips accordingly, e.g. 5, 5, 7). Call this "
            "ALONGSIDE score_airport (which you'd still call once per airport for the detailed "
            "figures/caveats) whenever presenting a ranking or comparing airport order -- and "
            "especially for a 'recalculate the ranking'/'how does the order change' follow-up "
            "after adjusting weights, pass the same `weights` here. Always state position/tie "
            "changes using exactly the rank numbers this tool returns; never infer or recall "
            "who moved past whom from the individual score_airport results."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "codes": {"type": "array", "items": {"type": "string"}, "description": "2+ airport codes to rank together"},
                "weights": {
                    "type": "object",
                    "description": "Optional, same shape as score_airport's -- adds a custom_ranking alongside the default one.",
                    "properties": {
                        "traffic": {"type": "number"},
                        "capacity_pressure": {"type": "number"},
                        "utilization": {"type": "number"},
                    },
                },
            },
            "required": ["codes"],
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
    "score_airport": lambda i: tools.score_airport(i.get("code", ""), weights=i.get("weights")),
    "rank_airports": lambda i: tools.rank_airports(i.get("codes", []), weights=i.get("weights")),
}


def _run_tool(name: str, tool_input: dict) -> dict:
    """Dispatch a client-side tool_use block to its handler, failing closed on an unknown name."""
    handler = _DISPATCH.get(name)
    if handler is None:
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

        # pause_turn: web_search hit its internal iteration cap. Resume by
        # resending the same history unchanged (no new message).
        resumes = 0
        while response.stop_reason == "pause_turn" and resumes < MAX_PAUSE_RESUMES:
            messages.append({"role": "assistant", "content": response.content})
            response = _create(client, messages)
            used_web_search = used_web_search or _used_web_search(response.content)
            resumes += 1

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "pause_turn":
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

    return (
        "I wasn't able to finish gathering data for that within my tool-call budget. "
        "Could you narrow the question (e.g. one airport or region at a time)?",
        messages,
        used_web_search,
    )
