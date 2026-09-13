# Airport Investment Intelligence Agent

A chat agent that helps investment analysts screen US commercial airports for
terminal/runway expansion potential, grounded in public aviation data and backed by a
deterministic scoring layer. Built following the `secure-org-agent-builder` standard
(see `SKILL.md` at the repo root one level up, and `CLAUDE.md`).

For scoring methodology, tradeoffs, and where AI is used, see **[DESIGN.md](DESIGN.md)**.
This file covers the engineering/security side: architecture, data flow, tool
permissions, security boundaries, injection defenses, grounding, limitations, and how
to run the tests.

## What it does

- Answers questions about US commercial airports' traffic, capacity pressure,
  utilization, long-haul route share, and expansion candidacy (see sample questions in
  the assignment brief — all four are exercised in `tests/test_live_eval.py`).
- Backs every ranking/comparison with a deterministic score (`src/scoring.py`), never
  an LLM-invented number.
- Explains its reasoning, citing the data source and year behind every claim, and
  states assumptions when it resolves ambiguity (e.g., "LA" → LAX) or when a metric is
  a proxy rather than an official figure.
- Supports multi-turn follow-ups within one chat session.

## What it deliberately does not do

- No voice interface (an explicit bonus in the brief; out of scope for one day).
- No live flight tracking — tested OpenSky Network, which refuses anonymous access to
  historical flights-by-airport (the endpoint that would have been useful here).
- No growth/trend forecasting — the anonymous BTS data available is a current-year
  snapshot, not a multi-year time series (see DESIGN.md tradeoffs).
- No multi-user accounts, login, or persistent history across restarts — this is a
  single-analyst local tool; session history lives in memory only.
- No write/delete/messaging actions of any kind — every tool is read-only.
- No non-US airport scoring (US firm investing in US airports; international
  *destinations* are still used for long-haul distance calculations).

## Architecture

```
Browser (src/web/index.html)
   |  POST /chat {message, session_id}
   |  GET /sessions, GET /sessions/{id}  (New / History buttons)
   |  POST /tts {text} -> audio/mpeg     (Voice button's spoken output)
   v
FastAPI server (src/server.py) -- localhost only
   |
   +-- sessions.py -- one JSON file per conversation under .sessions/
   +-- tts.py -- ElevenLabs speech synthesis, optional (falls back client-side)
   |
   v
Agent loop (src/agent.py) -- Claude Messages API, tool use, max 6 tool rounds
   |
   +-- tools.py -- 5 read-only functions, each validates identifiers before touching data
   |      |
   |      +-- retrieval.py --> OurAirports snapshot (data/airports_us.csv, runways_us.csv)
   |      +-- retrieval.py --> OpenFlights snapshot (data/routes.csv) + world coords
   |      +-- retrieval.py --> BTS T-100 live query (NTAD ArcGIS, cached to .cache/)
   |      |
   |      v
   |   scoring.py -- pure deterministic functions (no LLM, no network)
   |
   +-- web_search (native, server-side, Anthropic-hosted) -- supplementary evidence
          only, never the source of a scored number; see "Web search" below
```

One agent, one process. Every multi-airport question (comparisons, regional rankings)
is just several tool calls composed by the same agent loop — there's no concrete
requirement that justifies a second agent or any orchestration framework.

**No database.** Session history is a per-session message list saved as one JSON file
per conversation (`sessions.py`, under `.sessions/`) — this *does* need to survive a
restart (see "Conversation history" below), but "survive a restart" only needs a flat
file per session, not a query engine, so a database still isn't justified. **No vector
database.** Every retrieval is a structured lookup by airport code, region name, or
coordinate — never semantic search over free text — so embeddings would add complexity
with no benefit. **No queue or
background workers** — tool calls are a handful of cached HTTP round-trips per turn,
fast enough synchronously for one local user.

## Data flow and knowledge sources

Three sources, described in full in DESIGN.md (per-file provenance and exact filters
applied: [`data/README.md`](data/README.md)):

1. **OurAirports** (bundled, static) — airport metadata, coordinates, runway counts.
   Filtered at prep time (see `data/` below) to US airports with scheduled commercial
   service — 646 airports, not the full 16k+ US entries (private strips, closed
   fields, etc. are irrelevant to this use case and would just dilute the whitelist).
2. **OpenFlights `routes.dat`** (bundled, static) — nonstop route pairs, used with
   OurAirports coordinates to compute long-haul share.
3. **BTS T-100** (live, NTAD ArcGIS FeatureServer, cached 24h) — current-year
   passengers/departures/arrivals/enplanements per origin airport.

The model has **no filesystem, shell, or general/agentic web access** — no code
interpreter, no arbitrary HTTP client, nothing that lets it fetch or execute anything
of its choosing. The five functions in `tools.py` are the only *client-dispatched*
capability. It also has exactly one narrower thing beyond that: a native,
Anthropic-hosted `web_search` tool (see "Web search" below) — categorically different
from a generic browsing tool: no client-side execution, no new credential, bounded
per-turn by `max_uses`, and never the source of a number the scoring system relies on.
Its own pretrained knowledge is separately barred (in the system prompt) from supplying
airport-specific numbers regardless of source; it may only narrate what a tool
returned.

`data/` also includes `airports_world.csv` — a broader (non-US-filtered) coordinate
lookup, needed because long-haul routes out of a US airport are frequently
international, and destination coordinates must be resolvable even when the
destination itself isn't a scoring target.

## Tool permissions (least privilege)

| Tool | Risk | What it can touch |
|---|---|---|
| `lookup_airport` | Low (read) | The 646-airport whitelist only |
| `get_airport_profile` | Low (read) | Whitelist + runway data |
| `list_airports_in_region` | Low (read) | Whitelist, filtered by a static state-group constant |
| `get_traffic_stats` | Low (read) | Whitelist + one live BTS query (cached) |
| `score_airport` | Low (read) | All of the above, composed |
| `web_search` (native/server-side) | Low (read) | The open web, via Anthropic's infrastructure — capped at 5 searches/turn (`max_uses`), supplementary evidence only (see below) |

All read-only. There are no medium- or high-risk tools in this system (no writes, no
deletes, no external messages, no irreversible actions) — so there is no
human-confirmation-gate mechanism to build here; that's a deliberate simplification,
not a gap. Every identifier passed to one of the five data tools is checked against
the whitelist loaded from `data/airports_us.csv` **in `tools.py`, before any external
call** — the model cannot get an arbitrary string sent to the BTS API or used to
fabricate a result; identifiers outside the whitelist come back as `invalid_identifier`
immediately.

## Web search

Added as a stretch capability (see conversation record / commit history for the
motivating case: "unmet demand" style questions have no metric in any of the three
grounded data sources). It's the Messages API's native `web_search_20260209` server
tool — declared in `agent.py::TOOLS` like any other tool, but it runs entirely on
Anthropic's infrastructure: no HTTP client of our own, no new dependency, no new
credential (see "Why each dependency exists" below).

**Deliberately scoped as supplementary, not a sixth grounded source** — the system
prompt (`guardrails.py` "WEB SEARCH") is explicit that the five data tools are what
"grounded" means here, and web search is reached for only when a data tool returns
`not_found`/`insufficient_data`, or the question needs qualitative context no
structured dataset provides (news, reported plans, the "why" behind a pattern). Every
web-sourced answer must be visibly separated from data-tool figures and cited.

**Why the model judges source credibility itself, rather than a maintained domain
allowlist:** the API supports `allowed_domains`/`blocked_domains` as a hard, code-level
filter, and that was the first design considered — but per direction to keep this
simple and let the agent reason about source quality itself (the way an analyst would),
it's left to the model's judgment per query instead of a maintained list. The tradeoff:
credibility judgment is prompt-enforced, not code-enforced, for this one tool — a
deliberate exception to this project's general "deterministic code for deterministic
rules" stance, justified by `max_uses` still bounding cost/scope in code, and by every
other capability in the system (which identifiers are valid, what a score means)
remaining fully code-enforced.

**`pause_turn` handling:** web search runs its own server-side loop (search, read,
maybe search again) up to an internal 10-iteration cap; hitting it returns
`stop_reason: "pause_turn"`, which `agent.py::run_turn` resumes automatically (resending
the same history, no new message) up to `MAX_PAUSE_RESUMES` (3) before failing closed
with a plain "hit my iteration limit" message — same fail-closed shape as
`MAX_TOOL_ROUNDS` for the data tools.

**UI indicator:** the chat page shows a "🔍 Included a web search" badge on replies
where it happened (`used_web_search` in the `/chat` response, set by
`agent.py::_used_web_search` scanning the response for `server_tool_use`/
`web_search_tool_result` blocks). This is **post-hoc, not live** — the search happens
server-side inside one non-streamed API call, so there's no mid-request signal to show
while the analyst is waiting; a live "now searching" state would require switching
`/chat` to streaming (SSE), which was left out as more infrastructure than this
capability warrants.

## Conversation history

The chat page has **New** and **History** buttons. History lists saved conversations
(title auto-generated from the first message, most-recent-first) and lets the analyst
resume one — clicking an entry reloads its full message history and continues the same
conversation, including tool-call context, not just replayed text.

**Why this needed a real design change, not just a UI addition:** the original design
explicitly said session history didn't need to survive a restart (an in-memory dict was
enough for "support follow-up questions within a session"). "Go back into a
conversation later" is a different, genuine requirement — it needs the *full* message
history the model relies on (including `tool_use`/`tool_result` blocks, not just the
text shown in the UI) to still exist after the server process has restarted. That
ruled out anything client-side-only (the browser doesn't have that internal state) and
meant the server needed real persistence.

**Still no database.** `sessions.py` writes one JSON file per conversation under
`.sessions/` (gitignored, like `.cache/`) — `save`/`load_messages`/`list_sessions`/
`get_display_messages` are the entire access pattern, all satisfied by flat-file
read/write. Verified end-to-end against the real API (not just structurally): a real
tool-using conversation was serialized to disk, reloaded from the JSON, and fed into a
fresh API call as history — the follow-up answer correctly referenced the earlier turn,
confirming the round-trip preserves full model context, not just display text.

**Two message representations, on purpose.** The server persists the *raw* API message
list (what `agent.run_turn` needs to continue the conversation) but the `/sessions/{id}`
endpoint returns a separate, *reconstructed display* list (`sessions.get_display_messages`)
— just `{role, text, used_web_search}` per turn, with intermediate tool-call scaffolding
collapsed out. The browser only ever needs the second shape (it's what `/chat` already
returned live); it never sees raw tool payloads for a resumed conversation any more than
it does for a live one.

**Security note specific to this feature:** `GET /sessions/{id}` and `POST /chat`'s
`session_id` both take a client-supplied ID and use it to build a filesystem path.
`sessions.is_valid_session_id` (a `uuid.UUID(...)` parse) rejects anything that isn't a
real server-issued uuid4 *before* it reaches a path — a deterministic, code-level check
(SKILL.md §14 "ID validation"), not a trust assumption, even though the only real client
here is our own single-user frontend. `tests/test_server.py::test_get_session_invalid_id_rejected`
and `::test_invalid_session_id_rejected_on_chat` cover this directly (a `../../etc/passwd`-
style ID).

## Voice mode

A 🎤 button next to the text input, explicit push-to-talk rather than automatic
silence-detection: click to start listening (icon becomes ⏹️, a pulsing red mic), speak,
click again when you're done to stop and send — the transcript is submitted through
`sendMessage()`, the same function the typed form uses, so voice and text conversations
share one code path and one saved session. This is a deliberately scoped-down "Tier 1"
implementation, not a real-time conversational agent; see below for why that's a
different, much larger project.

**"Is it hearing me?"** — the input field shows the live partial transcript as you
speak (`SpeechRecognition`'s `interimResults`), so there's direct, visible proof it's
listening rather than a bare "Listening…" label with no feedback. The field is
read-only while listening (so the live transcript and manual typing can't collide) and
clears back to editable once you stop. Clicking while a reply is being generated or
spoken cancels that turn and returns to idle rather than doing nothing.

**Input** is the browser's native `SpeechRecognition` (`webkitSpeechRecognition` on
Chrome/Edge/Safari; confirmed working in Safari too, not just Chromium browsers;
Firefox and Brave don't support it -- unsupported/blocked browsers get a clear message
instead of silently failing). No new dependency, no new API key, no backend change.
Note this API isn't fully on-device — Chrome streams the audio to Google's servers to
transcribe it, which is why it can fail with a `network` error if that path is blocked
(VPN/firewall/privacy extension) even though everything else in the app is local; a
flaky first attempt is also a documented quirk of the API, so a `network` error retries
automatically up to 3 times before surfacing a diagnostic message.

**Brave is a special case, detected explicitly.** Brave exposes the `SpeechRecognition`
JS API (so feature detection passes) but deliberately disables the Google backend it
depends on — every attempt fails with `network`, permanently, not intermittently, so
the 3-retry logic above would just waste 1.5s failing the same way every time. The page
checks `navigator.brave.isBrave()` (Brave's own official detection API) and, when true,
skips straight to a Brave-specific message pointing at Chrome/Edge instead of retrying
a call that can never succeed. Text chat and spoken *replies* (TTS) are unaffected in
Brave — this only applies to the mic input path.

**Output** is ElevenLabs TTS (`src/tts.py`, `POST /tts`) for a natural-sounding voice,
falling back to the browser's own `speechSynthesis` (with a one-time notice) if
`ELEVENLABS_API_KEY` isn't set or the call fails — voice mode works out of the box,
just with a more robotic fallback voice until a key is added. The reply text is
converted to plain speakable text first (`stripMarkdownForSpeech`) so it doesn't read
`**`, `#`, or table pipes aloud.

**If it's falling back to the robotic voice despite a key being set:** the fallback
also triggers on any non-200 from ElevenLabs, not just a missing key, and their API
returns a 401 with `"missing_permissions"` if the key exists but wasn't granted the
**Text to Speech** permission when it was created (ElevenLabs API keys are scoped) —
check that permission in your ElevenLabs dashboard, not just that the key is present.
Also remember `.env` is only read at server startup, so a key added while the server
was already running needs a restart to take effect.

**Why this is turn-based, not a real conversation.** A genuine real-time voice agent —
continuous listening with barge-in (the agent stops talking the instant you start),
low-latency streamed speech in both directions — needs a fundamentally different
architecture: a WebSocket server (this one is plain request/response), streaming
speech-to-text (browser STT isn't built for it), the agent loop switched to streamed
token generation, and voice-activity detection for turn-taking. That's a multi-day
build with its own new failure modes, not an extension of this project, and voice was
called a bonus in the brief — so this implementation deliberately stays turn-based:
each exchange completes (listen → think → speak) before the next one starts, and you
can't interrupt a reply mid-sentence, only stop voice mode entirely.

**Security/cost note:** `ELEVENLABS_API_KEY` follows the same pattern as
`ANTHROPIC_API_KEY` — server-side only (`tts.py`), never sent to the browser. `/tts`
caps input at 2,000 characters, mirroring `/chat`'s cap, so a single call can't run away
on cost. If the key is unset, `/tts` returns 503 rather than crashing the server or
breaking text chat and voice input, which are fully independent of it.

## Security boundaries

- **Model ↔ tools:** a fixed tool schema (`agent.py::TOOLS`) — five client-dispatched
  data functions plus one native server-side `web_search`. No arbitrary HTTP/shell/file
  tool, and no code-execution tool, is ever exposed to the model. Even if a jailbreak
  fully succeeded at making the model "want" to do something else, it has no tool that
  would let it — web_search included, since it's a narrow, read-only, Anthropic-hosted
  capability with no client-side execution surface for a jailbreak to abuse.
- **Tools ↔ external API:** identifiers are validated against the loaded whitelist
  before being interpolated into the BTS query string (`tools.py::_resolve_code`,
  `retrieval.py::fetch_bts_traffic_all`'s alpha/length check as a second layer).
- **Secrets:** `ANTHROPIC_API_KEY` lives only in a server-side `.env` (see
  `.env.example`), loaded by `server.py`. Never placed in the system prompt, never sent
  to the browser, never logged. There is no second credential — the BTS and
  OpenFlights/OurAirports sources need no key at all, which was a deliberate choice
  (see DESIGN.md tradeoffs on why the login-gated BTS/Socrata dataset was not used).
- **Frontend ↔ backend:** `server.py`'s `__main__` binds to `127.0.0.1` by default.
  One static page, one `/chat` endpoint, nothing else. No login system — documented
  single-user assumption, not an oversight; the boundary that actually matters
  (browser JS never sees the API key or tool internals) holds regardless.
- **Output rendering:** assistant replies are markdown-formatted (bold, lists, inline
  code), so the page needs to turn some of that into real HTML — but it never treats
  model output as HTML directly. `renderMarkdownSafe` (in `web/index.html`) first runs
  the *entire* reply through the browser's own text-escaping (`div.textContent = text`,
  then read back `div.innerHTML`), so any literal `<script>` or `<img onerror=...>` the
  model emitted is neutralized to inert, visible text before anything else happens.
  Only *after* that does it apply a small fixed set of regex substitutions for
  `**bold**`, `` `code` ``, and list syntax — each substitution only ever introduces a
  tag this code wrote (`<strong>`, `<code>`, `<ul><li>`, `<p>`), never a tag lifted from
  the model's text. User messages and system notices still use plain `textContent`,
  since they need no formatting.
- **No high-risk tools exist**, so there is no destructive-action-confirmation flow to
  build. Noted explicitly rather than silently skipped (see `tests/test_tools.py`'s
  final comment on this).

## Prompt-injection and data-leakage defenses

- **Direct injection/jailbreak** (chat messages: "ignore instructions," "reveal your
  system prompt," roleplay bypass, encoded/translated extraction) — the system prompt
  (`guardrails.py`) instructs refusal and states this holds "no matter how the request
  is phrased, translated, encoded, or role-played." There is nothing sensitive in the
  system prompt itself (no credentials, no other-user data), so even a successful
  extraction discloses non-sensitive policy text, not a security breach.
- **Indirect injection via tool output** — the system prompt explicitly states tool
  results are data, not instructions. `tests/test_tools.py::test_indirect_injection_in_tool_output_is_inert_data`
  and `tests/test_live_eval.py::test_indirect_injection_via_tool_output_is_not_obeyed`
  both exercise this with a mocked malicious BTS response. This risk is meaningfully
  higher for `web_search` than for the structured data tools — open web pages are
  exactly the free-text, uncurated content this principle exists for, unlike
  BTS/OurAirports/OpenFlights' narrow numeric/code fields — so the system prompt calls
  it out by name ("this applies doubly to web pages") rather than relying on the
  generic instruction alone. The existing canary-string test mocks a BTS response
  rather than a live search result (there's no way to control what a real search
  returns), but the same mechanism applies: nothing in `tools.py` or `agent.py`
  distinguishes web content from any other tool result when deciding what to obey.
- **Data leakage** — there is no per-customer/tenant data anywhere in this system (all
  source data is public aviation statistics), so there's no cross-user leakage surface
  to defend. The only real secret is the API key, covered above.
- **Fail closed** — unresolvable identifiers, missing data, and exhausted tool-call
  budgets (`agent.py::MAX_TOOL_ROUNDS`) all return an explicit "can't do this" message
  rather than guessing or hanging.

## Grounding strategy

Two tiers, and the system prompt requires keeping them visibly separate in every answer:

- **Primary (grounded):** every airport-specific *number* must come from one of the
  five data tool calls made in that conversation — never from the model's training
  data. The scoring functions are the sole source of composite scores; the model
  receives and narrates them, never computes or overrides them. Every data-tool result
  carries a `source` field (and often a `caveats` list); the system prompt requires
  citing it inline. Missing data (`not_found` / `invalid_identifier` /
  `insufficient_data`) is always surfaced as such, never silently filled in — enforced
  by instruction and demonstrated in `tests/test_tools.py`.
- **Supplementary (web search):** qualitative context only, never a number the scoring
  system relies on, always cited and visibly marked apart from grounded figures, with
  its own credibility judged per-query (see "Web search" above) rather than presented
  with the same certainty as a BTS number.

## Known limitations

See DESIGN.md's "Key tradeoffs" section for the full reasoning. In short: no seat-based
load factor, no multi-year growth trend, long-haul share measures route-network
breadth (not passenger-weighted traffic), and OpenFlights' route snapshot is a
community dataset last broadly refreshed years ago, not a live schedule feed. All of
these are surfaced to the analyst in-answer via each tool's `caveats`/`source` fields,
not hidden. Additionally: web search source-credibility judgment is prompt-enforced,
not code-enforced (see "Web search" above) — the one place in this system where that's
true; and the "searched the web" UI indicator is post-hoc, not a live progress signal
(no streaming).

## Why each dependency exists

| Dependency | Why |
|---|---|
| `anthropic` | The only LLM call in the system (tool use + composition) |
| `fastapi`, `uvicorn`, `pydantic` | The chosen chat interface (a minimal local web app) |
| `requests` | The one live HTTP call, to BTS's public ArcGIS endpoint (also used by `tts.py` for ElevenLabs -- one small REST call didn't justify their SDK) |
| `python-dotenv` | Loads `.env` locally so the API key isn't exported by hand every run |
| `pytest` | Runs the test suite |

Nothing else. No ORM, no vector store client, no task queue, no web framework beyond
FastAPI itself. **Web search added no new dependency** — it's a native Messages API
tool, called through the same `anthropic` client already in this table. **Voice mode
added no new Python dependency either** — speech input/output are browser APIs, and
ElevenLabs TTS is one REST call via the `requests` library already listed above.

## Running it

```bash
cd airport-investment-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in ANTHROPIC_API_KEY (ELEVENLABS_API_KEY is optional -- Voice mode)
python3 src/server.py  # binds to 127.0.0.1:8000
```

Open `http://127.0.0.1:8000`.

The bundled `data/*.csv` files are pre-filtered snapshots (see
"Data flow and knowledge sources" above); regenerating them requires re-downloading
`airports.csv`/`runways.csv` from OurAirports and `routes.dat` from OpenFlights and
re-running the same filters described there — not needed to just run the agent.

## How to run the evaluation tests

```bash
pytest tests/                      # offline suite: no network, no API key needed
ANTHROPIC_API_KEY=sk-... pytest tests/test_live_eval.py   # live behavioral eval
```

The suite is split in two, deliberately:

- **`test_scoring.py`, `test_tools.py`, `test_agent_harness.py`, `test_server.py`,
  `test_sessions.py`, `test_tts.py`** — fully deterministic, offline, no API key.
  These prove the code: scoring formulas, identifier validation (skill §17 "tool
  safety": invalid parameters, unauthorized identifiers), the tool-calling loop's
  mechanics (dispatch, unknown-tool handling, the `MAX_TOOL_ROUNDS` fail-closed
  cutoff, `pause_turn` resumption and its own `MAX_PAUSE_RESUMES` cutoff, the
  `used_web_search` detection), the HTTP layer (request validation, session
  continuity across a simulated restart, invalid-session-id rejection), conversation
  persistence (save/load round-trips, auto-titling, turn reconstruction for the
  History panel), and the ElevenLabs client (request shape, truncation, unconfigured/
  upstream-failure error paths) — using a scripted fake Anthropic client /
  monkeypatched `agent.run_turn`/`tts.synthesize_speech` and an isolated temp
  directory in place of `.sessions/`, so none of this depends on a real model call,
  a real ElevenLabs key, or touches real saved conversations. Voice mode's
  browser-side state machine (listen → send → speak → listen again, including
  mid-speech interruption not hanging the loop, and the markdown-to-speech text
  cleanup) was verified against the actual shipped script with a scripted
  `SpeechRecognition`/`Audio`/`fetch` DOM stub during development, the same
  technique used for the markdown renderer -- not checked into this repo (it
  needs a JS test runner this project doesn't otherwise depend on), so treat
  that piece as developer-verified rather than covered by `pytest tests/`.
- **`test_live_eval.py`** — the skill §13/§17 behavioral checklist (off-topic refusal,
  direct jailbreak, prompt/credential extraction, encoded extraction, indirect
  injection via a mocked tool response, ambiguous questions, regional/multi-airport
  questions, missing-data handling), plus tests specific to web search (the "unmet
  demand" question reasons from real data without fabricating a metric; a
  news/plans-style question actually triggers `used_web_search`) and to tool-selection
  gating (a plain factual question must NOT surface `score_airport`'s percentile/
  composite-score jargon; an actual ranking question must) — run against the **real**
  Claude API. This is the only way to actually test "does the model refuse a jailbreak"
  or "does it reach for the right tool" — a mocked client can prove the harness is
  correct but can't prove model behavior. Skipped automatically if `ANTHROPIC_API_KEY`
  isn't set.

This project's own test run: all 80 tests passing (63 offline + the 17-test live eval
suite run against the real Claude API), and the full pipeline was smoke-tested against
live BTS data for SFO, ANC, SNA, LAX, and BOS during development, plus a manual
end-to-end check of a real "unmet demand" question showing web search correctly
supplementing (not replacing) the grounded proxy metrics. If you regenerate
`data/*.csv` or change the system prompt, re-run `pytest tests/` with a real
`ANTHROPIC_API_KEY` in `.env` to re-validate model behavior, not just the code.
