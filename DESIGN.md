# Design Document — Airport Investment Intelligence Agent

## What this is

A chat agent for investment analysts screening US airports for terminal/runway
modernization potential. It answers questions like "which New England airports are
strong expansion candidates?" or "what's the long-haul share out of Anchorage?" by
combining three public data sources with a deterministic scoring layer, and using
Claude only to interpret the question, call the right tools, and narrate the result.

See `README.md` for the full architecture/security/testing writeup. This document
covers what the assignment specifically asked for: scoring methodology, key
tradeoffs, and where/how AI is used.

## Data sources (and why these three)

| Source | What it gives us | Access |
|---|---|---|
| [OurAirports](https://ourairports.com/data/) | Airport metadata, coordinates, US state, runway count/length | Static snapshot, bundled, public domain |
| [OpenFlights `routes.dat`](https://openflights.org/data.php) | Real nonstop origin→destination route pairs | Static snapshot, bundled, free |
| [BTS T-100](https://geodata.bts.gov/datasets/usdot::t-100-domestic-market-and-segment-data/about) (NTAD ArcGIS extract) | Current-year passengers/departures/arrivals/enplanements per origin airport | Live query, free, no key |

**Why three sources instead of one:** the obvious single source, BTS T-100, is the
official public dataset for airline traffic — but the only version of it reachable
without a login is a simplified extract with no destination, no distance, and no
seat-capacity fields, and it's a current-year snapshot rather than a time series (see
"What we verified vs. assumed" below). OpenFlights fills the destination/distance gap
(needed for long-haul share); OurAirports fills the runway/capacity and
region/geography gap. All three are free and require no signup.

## Scoring methodology

Everything below is plain, deterministic Python (`src/scoring.py`) — the model never
computes or overrides these numbers; it only receives and narrates them.

**Component metrics** (per airport, current year):

1. **Avg. passengers per departure** = passengers ÷ departures. A utilization proxy.
   *Explicitly not* a true seat-based load factor — no public, anonymous source used
   here publishes seat capacity, so this measures average realized load per flight,
   not load as a fraction of available seats.
2. **Capacity pressure** = departures ÷ usable runway count. An infrastructure-strain
   proxy — more departures squeezed through fewer runways suggests more physical
   pressure on the airfield. Not an FAA-declared capacity figure; real throughput also
   depends on airspace, gates, and staffing, none of which are in these datasets.
3. **Traffic intensity percentile** = this airport's passenger count ranked against
   every other tracked US airport, current year (0–100). Used in place of a
   growth-trend metric (see tradeoffs below).
4. **Long-haul route share** = of this airport's known nonstop routes (OpenFlights),
   the fraction whose great-circle distance (via OurAirports coordinates) is ≥ 2,500
   statute miles. This is a **route-network** share, not a flights- or
   passengers-weighted share — OpenFlights records route existence, not frequency.

**Composite expansion-candidacy score** (0–100):

```
score = 0.40 × traffic_intensity_percentile
      + 0.35 × capacity_pressure_percentile
      + 0.25 × utilization_percentile
```

All three inputs are first converted to a 0–100 percentile rank against the same peer
set (all US airports with current-year BTS data), so they're on a comparable scale
before combining — this avoids arbitrarily mixing raw units (passengers vs.
departures-per-runway vs. a ratio). Weights are a judgment call: traffic intensity
(raw demand) is weighted highest because unmet demand only matters if there's demand;
capacity pressure next, because that's the direct expansion signal; utilization last,
as a supporting/confirming signal. These weights are constants in `scoring.py` and are
the first thing to revisit if an analyst disagrees with a ranking.

The agent always shows the component numbers alongside the composite score — never a
bare number — so an analyst can see *why* an airport ranked where it did. **But only
when scoring is actually what was asked for.** Early testing showed the agent reaching
for `score_airport` and its percentile/composite-score vocabulary even for plain
descriptive questions ("what's SFO's traffic like," "why is it congested") — a real
usability problem, since most of that framework is irrelevant noise for a question that
isn't about ranking or recommending anything. Fixed by tightening `score_airport`'s
tool description to fire only on an explicit ranking/comparison/recommendation ask
(`agent.py`), with a matching system-prompt rule ("TALK LIKE AN ANALYST, NOT A
PRINTOUT," `guardrails.py`) that a plain question gets a plain conversational answer
from `get_traffic_stats`/`get_airport_profile` instead. This is a code-level (tool
description) fix backed by a prompt-level one, not a prompt fix alone — see
`tests/test_live_eval.py::test_plain_factual_question_does_not_pull_in_scoring_jargon`
and `::test_ranking_question_does_use_scoring` for the behavior this locks in.

**"Unmet demand" and "congestion"** have no single official public metric, so the
agent doesn't invent one. For an explicit ranking/recommendation question, it answers
by presenting the component KPIs above together (e.g., high traffic percentile + high
capacity pressure + high utilization = a demand-pressure story) and says explicitly
that these are proxies. For a plain "what/why" question, it instead gives the raw
traffic baseline plus qualitative web-search context (see "Web search
as a stretch capability" below) — e.g. reported physical/regulatory constraints behind
the numbers — kept visibly separate from the grounded figures, never blended in as if it
carried the same certainty.

## Web search as a stretch capability

The scoring methodology above is retrospective and metric-based; it has no way to
answer *why* a pattern exists beyond what the KPIs themselves imply. Real example: asked
"what is the unmet flight demand at SFO and why," the agent could correctly compute
that SFO sits in the ~96th-99th percentile on traffic and capacity-pressure proxies,
but had nothing to say about the *why* beyond that — no data tool here publishes airport
infrastructure narratives (runway geometry, FAA rate restrictions, site constraints).

Added: Anthropic's native `web_search` server tool (no new dependency, no new
credential — see README "Web search"). Two decisions worth flagging for review:

- **Supplementary, not a sixth grounded source.** The composite score and every KPI
  remain 100% deterministic-code-derived; web search only ever adds qualitative
  context, always cited and visibly separated from the scored data in the answer.
- **Model judges source credibility, not a maintained allowlist.** The API supports a
  hard `allowed_domains` filter, which was the natural first design given this
  project's "deterministic code over prompt trust" stance elsewhere. Per explicit
  direction to keep this simple and let the agent reason about data quality itself,
  that was dropped in favor of prompt guidance telling the model to weigh sources the
  way an analyst would (an FAA/airport-authority page over an unsourced blog) and say
  what kind of source it's relying on. This is a real, deliberate exception to the
  "enforce deterministic rules in code" principle applied everywhere else in this
  system — justified because judging source credibility is itself a natural-language
  reasoning task (squarely "use the model for interpreting/reasoning" per SKILL.md
  §14), not a deterministic rule like identifier validation, and because `max_uses`
  still bounds cost/scope in code regardless of which sources get used.

## Key tradeoffs

**What we verified vs. what the original plan assumed.** The initial plan assumed BTS
T-100 would anonymously provide destination, distance, seats, and multiple years of
history. Live testing during implementation showed the only anonymous BTS endpoint
(NTAD/ArcGIS-hosted) is a thinner, current-year, origin-only extract; the richer
Socrata-hosted version requires login. Rather than silently dropping the assignment's
named "long-haul %" example, OpenFlights was added specifically to cover it. Two
concrete consequences: **no growth-trend KPI** (replaced with cross-sectional
peer-percentile scoring), and **no true load factor** (renamed to "avg. passengers per
departure" and caveated). This is disclosed in every scored answer via a `caveats`
list the tool returns, and enforced in the system prompt as a citation requirement —
not just noted here and forgotten.

**Route existence vs. route frequency.** OpenFlights tells us an airport *has* a route
to Tokyo; it doesn't tell us how often that route flies or how full it is. A single
weekly long-haul flight and a daily one both count as "one long-haul route." This is
the right tradeoff for a one-day build (no other free anonymous source gives
frequency), but it means long-haul *share* should be read as route-network breadth, not
passenger-weighted traffic mix — the agent states this every time it reports the
metric.

**No forecasting.** The score is retrospective/current-state, not a predictive model.
Given the one-day scope and the "prefer the smallest reasonable implementation"
instruction, a trained growth-prediction model was explicitly out of scope — an
analyst using this tool is meant to combine the current-state signal with their own
judgment about future demand, not treat the score as a forecast.

**Single-user, no auth.** This is a local analyst tool, not a multi-tenant service —
no login system, no per-user data isolation, because there's no multi-tenant data to
isolate. Documented in README as a scoping assumption, not an oversight.

## Where and how AI is used

Claude (Anthropic Messages API, tool use) is used for four things:

1. **Understanding the question** — mapping "compare LA and Santa Ana's congestion"
   into two `score_airport` tool calls, or "New England candidates" into a
   `list_airports_in_region` call followed by several `score_airport` calls.
2. **Choosing and sequencing tool calls** — deciding which of the five data tools apply
   and in what order, including asking for clarification when `lookup_airport` reports
   an ambiguous match, and recognizing when a question needs `web_search` instead of
   (or alongside) the data tools.
3. **Composing the final explanation** — turning the structured tool output (numbers,
   sources, caveats) into readable prose that cites its sources and states
   assumptions/uncertainty, per the assignment's explicit requirement.
4. **Judging web-search source credibility** — the one place in this system where a
   quality judgment is left entirely to the model rather than enforced in code (see
   "Web search as a stretch capability" above for why that specific exception was
   made).

Claude is **never** used to: compute a score, decide whether an identifier is valid,
decide what data the agent is allowed to touch, or determine authorization/scope
enforcement at the capability level — all of that is plain code (`scoring.py`,
`tools.py`'s whitelist checks, the fixed tool schema in `agent.py`). See README
"Security boundaries" for the full reasoning on why each of those is code-enforced
rather than prompt-enforced.
