# Airport Investment Intelligence Agent

A chat agent that helps analysts figure out which US airports are worth investing in
for terminal or runway expansion. It's grounded in public aviation data and backed by
a deterministic scoring system — no invented numbers. See [DESIGN.md](DESIGN.md) for
how the scoring actually works and what tradeoffs we made along the way. This file is
about the architecture, security, and how to actually run the thing.

## What it does

- Answers questions about airport traffic, congestion, long-haul routes, and whether
  an airport looks like a good expansion candidate — and always tells you where the
  number came from.
- Ranks or compares airports with a deterministic score, computed in plain Python
  (`scoring.py`), never something the AI just made up. You can even ask it to run a
  "what if" scenario, like dou bling the weight it gives to congestion.
- Remembers past conversations, so you can pick up where you left off (see New/History
  in the sidebar).
- Supports a real, live voice call with the agent — you talk, it talks back, using
  ElevenLabs' voice platform. No typing needed.

## What it doesn't do

- No growth forecasting — it tells you where things stand today, not where they're
  headed.
- Nothing that writes, deletes, or sends anything anywhere — every tool it has only
  reads data.

## Data sources

- **Public API — BTS T-100** (data.bts.gov, updated monthly). Gives us passenger
  boardings, departures, seats, and load factor for every US airport, always looking
  at the trailing 12 months. It's free, but needs a Socrata app token — see
  "Running it" below.
- **Bundled datasets** (static files, not APIs):
  - **OurAirports** — airport and runway details.
  - **OpenFlights** — which routes each airport actually flies, used to work out
    long-haul share.

Full methodology and where every file came from: [DESIGN.md](DESIGN.md#data-sources).

## Architecture

```
    +-----------------------------------+    +-------------------------------------------+
    | BROWSER  (web/index.html)         |    | ELEVENLABS  (cloud)                       |
    | typed chat + voice-call button    |    | runs the live call: mic, speech-to-text,  |
    |                                   |    | spoken replies, turn-taking               |
    +-----------------------------------+    +-------------------------------------------+
                      |                                            |
         POST /chat, GET /sessions,                    POST /v1/chat/completions
         GET /voice-call/signed-url                   ("what should I say next?")
                      v                                            v
 +--------------------------------------------------------------------------------------+
 | SERVER   (server.py)                                                                 |
 | needs a public URL while a voice call is running                                     |
 +--------------------------------------------------------------------------------------+
                      |                      |                     |
                      v                      v                     v
     +--------------------------------+      |    +--------------------------------+
     | sessions.py                    |      |    | voice_llm.py                   |
     | saves each text conversation   |      |    | signed-url + streams answers   |
     | to a JSON file                 |      |    | back during a call             |
     +--------------------------------+      |    +--------------------------------+
                                             |                     |
                                             v                     v
 +--------------------------------------------------------------------------------------+
 | AGENT LOOP   (agent.py)                                                              |
 | Claude + fixed tool list + guardrails.py system prompt                               |
 | loop:  ask -> (maybe) run a tool -> ask again -> final answer                        |
 +--------------------------------------------------------------------------------------+
                      |                                            |
              client-side tools                               native tool
                      v                                            v
       +----------------------------+               +----------------------------+
       | tools.py                   |               | web_search                 |
       | 6 read-only functions;     |               | (Anthropic-hosted)         |
       | checks airport code vs     |               | supplementary only --      |
       | whitelist FIRST            |               | never a scored number      |
       +----------------------------+               +----------------------------+
                      |
                      +--------------------------------------------+
                      v                                            v
     +--------------------------------+           +--------------------------------+
     | retrieval.py                   |           | scoring.py                     |
     | loads & fetches:               |           | deterministic KPIs.            |
     | - OurAirports (bundled)        |           | No LLM involved --             |
     | - OpenFlights (bundled)        |           | plain Python math,             |
     | - BTS T-100 (live, cached)     |           | same input = same output       |
     +--------------------------------+           +--------------------------------+
```

Under `SERVER`, three things branch off: `sessions.py` just saves and loads text-chat
history to disk, and never talks to the agent itself. The middle arrow is `server.py`
calling the agent directly — that's what happens for ordinary typed chat. The right
arrow goes to `voice_llm.py`, which is what calls the agent during a live voice call.
Either way, every real question ends up going through the exact same agent loop, the
exact same tools, and the exact same rules — voice and text never diverge once a
question actually needs answering.

Lower down, `tools.py` calls `retrieval.py` (to fetch raw data) and `scoring.py` (to
do the math) independently — they never call each other.

One thing this diagram leaves out on purpose: during a live call, the browser and
ElevenLabs also talk directly to each other for the actual call audio, over their own
connection. That's *why* our server needs a public URL at all — not so the browser can
reach it (it already can, it's just our own webpage), but so ElevenLabs' cloud can
call back into `/v1/chat/completions` to ask what the agent should say next.

One agent, one process. No database beyond a JSON file per conversation, no vector
database (everything is looked up by airport code or region, not semantic search), no
background queue — a handful of cached HTTP calls per question is fast enough to just
do synchronously.

## Tool permissions

| Tool | Touches |
|---|---|
| `lookup_airport`, `get_airport_profile`, `list_airports_in_region` | The 646-airport whitelist |
| `get_traffic_stats`, `score_airport`, `rank_airports` | Whitelist + a live BTS query (cached 24h) |
| `web_search` (native) | The open web — capped at 3 searches per turn, and only used to fill gaps, never to produce a score |

Everything here is read-only, so there's nothing that needs an "are you sure?"
confirmation step. Every airport code gets checked against the whitelist in code
before it's ever used in a real request — an unknown code never reaches the BTS API.

## Security boundaries

- **Secrets never leave the server.** `ANTHROPIC_API_KEY`, `BTS_SOCRATA_APP_TOKEN`, and
  `ELEVENLABS_API_KEY` are never sent to the browser or dropped into a prompt.
- **The model can't do anything we didn't build a tool for.** There's no generic
  "run this command" or "call this URL" tool, so even a successful jailbreak has
  nowhere to escalate to.
- **Tool results are treated as data, not instructions** — including anything that
  comes back from a web search. This is spelled out explicitly in the system prompt,
  since the open web is the least trustworthy input this system ever sees.
- **Session IDs are checked before touching disk.** Both `/chat` and `/sessions/{id}`
  validate that the ID is a real UUID before it's used to read or write a file.
- **Every number is grounded.** Any airport-specific figure the agent states has to
  come from an actual tool call made in that conversation. If the data isn't there,
  it says so — it never fills the gap with a guess.

## Voice call

Beyond typed chat, you can have an actual real-time conversation with the agent —
click the call button, talk normally, get spoken answers back. This works through
ElevenLabs' Conversational AI platform: ElevenLabs handles the microphone, the
speech-to-text, the text-to-speech, and the back-and-forth of a live call, while our
own server plugs in as the "brain" behind it, using the exact same Claude agent and
tools as the typed chat.

To turn this on, you'll need:
- An ElevenLabs account with Conversational AI (Agents) access, and an Agent created
  there with its "Custom LLM" pointed at your server's `/v1/chat/completions`.
- A public URL your server is reachable at (a tool like ngrok works fine for
  testing) — ElevenLabs needs to be able to reach your server from the internet.
- The env vars below, filled in.

## Running it

```bash
cd airport-investment-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in the required keys below
python3 src/server.py  # binds to 127.0.0.1:8000
```

Required:
- `ANTHROPIC_API_KEY` — powers the agent itself.
- `BTS_SOCRATA_APP_TOKEN` — free, instant, no approval wait. Sign up at
  [data.bts.gov/signup](https://data.bts.gov/signup), then Profile → Developer
  Settings → Create New App Token.

Only needed for the voice call feature:
- `ELEVENLABS_API_KEY` — needs the "ElevenAgents: Write" permission turned on.
- `ELEVENLABS_AGENT_ID` — the Agent you created in ElevenLabs' dashboard.
- `VOICE_LLM_SHARED_SECRET` — any random string you make up yourself. It's how the
  server checks that a request to `/v1/chat/completions` is really coming from your
  ElevenLabs Agent, not a stranger on the internet. Put the same value into the
  Agent's Custom LLM config as its Authorization header ("Bearer &lt;this value&gt;").

