# Design — Airport Investment Intelligence Agent

## What it does

A chat agent that helps analysts screen US airports for terminal or runway expansion
potential, using public aviation data and a scoring model that's plain deterministic
code — not something the AI is guessing at. Claude's job is to understand the
question, decide which data to pull, and explain the answer in normal language. It
never computes a score itself.

## Data sources

| Source | Gives us | Access |
|---|---|---|
| [OurAirports](https://ourairports.com/data/) | Airport details, coordinates, runways | Bundled snapshot |
| [OpenFlights](https://openflights.org/data.php) `routes.dat` | Which routes each airport flies | Bundled snapshot |
| [BTS T-100](https://data.bts.gov/d/r495-tyji) (data.bts.gov) | Passenger boardings, departures, seats, and load factor, always looking at the trailing 12 months (right now that's 2025-05 through 2026-04) | Live public API, updated monthly, needs a free Socrata app token |

No single free source gives us everything (destination, distance, seat counts), so we
combine three: BTS for traffic volume and load factor, OpenFlights plus OurAirports'
coordinates for long-haul distance, and OurAirports for runway capacity. Where each
file actually came from: [`data/README.md`](data/README.md).

## Scoring methodology

All deterministic Python (`scoring.py`) — Claude just narrates these numbers, it
never invents them.

- **Avg. passengers per departure** = passengers ÷ departures. A rough stand-in for
  "how full are the planes," and it's what actually feeds the composite score below.
  A real, BTS-reported load factor is also available now (`load_factor_pct`), but we
  kept it separate rather than swapping it into the formula, so the scoring
  methodology stays stable and easy to explain.
- **Capacity pressure** = departures ÷ runway count. A rough proxy for how strained an
  airport's runways are — not an official FAA number, and not the same thing as
  terminal or gate congestion (that would need data like security wait times, which
  we don't have).
- **Percentiles** — each of the above gets turned into a percentile against every
  other airport we track, so a small regional airport and a huge international hub
  can be compared fairly.
- **Long-haul route share** — of an airport's known nonstop routes, what fraction are
  2,500+ miles (measured as great-circle distance using OurAirports' coordinates).
  This tells you about the breadth of the route network, not how many people are
  actually flying those routes.
- **Composite expansion-candidacy score** = 40% traffic + 35% capacity pressure + 25%
  utilization, all as percentiles. That weighting is a judgment call, written as
  constants in `scoring.py` — it's the one official score.
- **Sensitivity scenarios** — you can ask the agent to reweight things, like "double
  the weight of congestion." It runs the same formula with different weights (anything
  you don't mention defaults to 1) and shows you the custom score and the official one
  side by side. This is always a temporary what-if, never a replacement for the real
  score.


## Key tradeoffs

- **No growth trend.** BTS gives us a rolling 12-month window, not a multi-year
  history, so all our scoring is a snapshot comparison against peers right now, not a
  trend over time. And that window genuinely isn't a calendar year — it just rolls
  forward each month — so if you ask for "the latest complete calendar year," the
  agent will tell you honestly that this data source doesn't have one, rather than
  pretending the rolling window is the same thing.
- **No arrivals number.** This BTS feed only reports departures per airport, so the
  agent can tell you how many flights leave an airport, but not the total number of
  flights in and out combined — that data just isn't published here.
- **Passenger counts are outbound only.** We only know who boarded a flight at each
  airport, not who got off one — there's no arrivals-side passenger count in this
  data. Since every airport is measured the same way, rankings are at least
  internally consistent, but an airport with unusually lopsided inbound/outbound
  traffic could still look different than it would under a true two-way count.
- **This isn't a financial model.** The score tells you about demand, traffic, and
  capacity strain — not profitability. A real investment case would also need
  construction costs, financing terms, actual airport/airline revenue, and a proper
  demand forecast, none of which we have.
- **Long-haul share counts routes, not passengers.** OpenFlights tells us which
  routes exist, not how full or how frequent they are.
- **Single local user.** No accounts, no multi-tenant separation — this is built to
  be one analyst's local tool, not something you'd deploy for a team.

## Where AI is used

Claude handles understanding the question, deciding which tools to call, judging how
trustworthy a web search result looks (the one place we genuinely leave a judgment
call to the model instead of code), and writing up the final answer with sources
cited.

Claude never computes a score, validates an airport code, or decides what data it's
allowed to touch — all of that is plain code (`scoring.py`, the whitelist in
`tools.py`, the fixed tool list in `agent.py`). That's true for the "what if"
sensitivity scenarios too: Claude only picks which weight numbers to try; the actual
math and ranking happen in `scoring.py`.

## Voice call

The agent can also run as a real, continuous phone-style call — you talk, it answers
back, and you can interrupt it like a normal conversation. We plug our existing agent 
into ElevenLabs' Conversational AI platform: they handle the
microphone, the transcription, the spoken replies, and the turn-taking, and our
server just answers the question "what should the agent say next," using the exact
same Claude, tools, and system prompt as typed chat.

The one real wrinkle: our agent sometimes needs several seconds to answer, especially
if it has to call a tool like a live traffic lookup. A real-time voice platform
expects an answer fast, or it assumes something's wrong and gives up on the call. So
our server immediately says something like "Let me check on that," then keeps sending
small signs of life every second while the real answer is still being worked out, and
finally sends the real answer once it's ready. All three pieces arrive as one smooth
reply, spoken as a single continuous answer with a natural pause in the middle — not
three separate things.
