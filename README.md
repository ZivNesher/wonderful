# Airport Investment Intelligence Agent

A chat agent for screening US commercial airports for terminal/runway expansion
potential — grounded in public aviation data, backed by a deterministic scoring layer.
See [DESIGN.md](DESIGN.md) for scoring methodology and tradeoffs; this file covers
architecture, security, and how to run it.

## What it does

- Answers questions about airport traffic, congestion, long-haul mix, and expansion
  candidacy, citing its data source for every number.
- Ranks/compares airports using a deterministic score (`scoring.py`), never an
  LLM-invented number.
- Supports follow-up questions, saved across sessions (New/History in the UI).
- Voice mode: push-to-talk mic input, spoken replies.

## What it doesn't do

- No real-time/interruptible voice conversation (see DESIGN.md).
- No growth forecasting — scoring is current-state, not predictive.
- No multi-user accounts — single local analyst tool.
- No write/delete/external-messaging actions — every tool is read-only.

## Architecture

```
Browser (web/index.html)
   |  POST /chat, /tts   GET /sessions, /sessions/{id}
   v
FastAPI server (server.py) -- localhost only
   |
   +-- sessions.py  -- one JSON file per conversation (.sessions/)
   +-- tts.py       -- ElevenLabs speech synthesis (optional)
   |
   v
Agent loop (agent.py) -- Claude, tool use
   |
   +-- tools.py -- 5 read-only functions, validate identifiers before touching data
   |      +-- retrieval.py --> OurAirports / OpenFlights (bundled) + BTS T-100 (live)
   |      v
   |   scoring.py -- deterministic KPIs, no LLM
   |
   +-- web_search (native Anthropic tool) -- supplementary, never a scored number
```

One agent, one process. No database (sessions are one JSON file each — nothing here
needs a query engine), no vector database (every lookup is by airport code/region, not
semantic search), no queue (a few cached HTTP calls per turn is fast enough
synchronously for one user).

## Tool permissions

| Tool | Touches |
|---|---|
| `lookup_airport`, `get_airport_profile`, `list_airports_in_region` | The 646-airport whitelist |
| `get_traffic_stats`, `score_airport` | Whitelist + live BTS query (cached 24h) |
| `web_search` (native) | The open web — capped at 3 searches/turn, supplementary only |

All read-only. No medium/high-risk tools exist, so there's no confirmation-gate to
build. Every identifier is checked against the whitelist **in code** before any
external call — an unknown code never reaches the BTS API.

## Security boundaries

- **Secrets stay server-side.** `ANTHROPIC_API_KEY` and `ELEVENLABS_API_KEY` are never
  sent to the browser or placed in a prompt.
- **Fixed tool schema.** No arbitrary HTTP/shell/file/code-execution tool is ever
  exposed to the model — a jailbreak has nothing to escalate to.
- **Tool output is data, not instructions** (including web search results) — stated
  explicitly in the system prompt, since open web text is the least vetted input here.
- **Path-safe session IDs.** `GET /sessions/{id}` and `/chat`'s `session_id` are
  validated as real UUIDs in code before touching the filesystem.
- **Localhost only.** `server.py` binds to `127.0.0.1`; no login system, since this is
  a single-user local tool (not a multi-tenant service).
- **Grounding.** Every airport-specific number must come from a tool call in that
  conversation; missing data is reported as missing, never estimated.

## Running it

```bash
cd airport-investment-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in ANTHROPIC_API_KEY (ELEVENLABS_API_KEY optional)
python3 src/server.py  # binds to 127.0.0.1:8000
```

## Tests

```bash
pytest tests/                                              # offline, no API key needed
ANTHROPIC_API_KEY=sk-... pytest tests/test_live_eval.py     # live behavioral eval
```

Offline tests (`test_scoring.py`, `test_tools.py`, `test_agent_harness.py`,
`test_server.py`, `test_sessions.py`, `test_tts.py`) prove the code — scoring
formulas, identifier validation, the tool-calling loop, HTTP layer, persistence — with
a scripted fake Anthropic client, so none of it depends on a real model call.
`test_live_eval.py` runs the skill's behavioral checklist (scope refusal, jailbreak
resistance, prompt/credential extraction, indirect injection, grounding) against the
real Claude API — the only way to actually test model behavior, not just the harness
around it. Skipped automatically without `ANTHROPIC_API_KEY`.
