# Design — Airport Investment Intelligence Agent

## What it does

A chat agent that helps analysts screen US airports for terminal/runway expansion
potential, using public aviation data and a deterministic scoring model. Claude
handles conversation and tool orchestration; it never computes a score itself.

## Data sources

| Source | Gives us | Access |
|---|---|---|
| [OurAirports](https://ourairports.com/data/) | Airport metadata, coordinates, runways | Static snapshot, bundled |
| [OpenFlights](https://openflights.org/data.php) `routes.dat` | Nonstop route pairs | Static snapshot, bundled |
| [BTS T-100](https://geodata.bts.gov/datasets/usdot::t-100-domestic-market-and-segment-data/about) (NTAD) | Current-year traffic (passengers/departures) | Live query, free, no key |

No single free source anonymously provides destination/distance/seats, so three
sources cover what one couldn't: BTS for traffic volume, OpenFlights + OurAirports
coordinates for long-haul distance, OurAirports for runway capacity. Per-file
provenance: [`data/README.md`](data/README.md).

## Scoring methodology

Deterministic Python (`scoring.py`) — Claude narrates these numbers, never invents them.

- **Avg. passengers per departure** = passengers ÷ departures. A utilization proxy,
  not true seat-based load factor (no public seat-capacity field available).
- **Capacity pressure** = departures ÷ runway count. An infrastructure-strain proxy,
  not an official FAA capacity figure.
- **Traffic/pressure/utilization percentiles** = each metric ranked against all other
  tracked airports (0–100), so differently-scaled numbers combine fairly.
- **Long-haul route share** = of an airport's known nonstop routes, the fraction
  ≥2,500 miles (great-circle, via OurAirports coordinates). Measures route-network
  breadth, not passenger-weighted traffic.
- **Composite expansion-candidacy score** = `0.40×traffic + 0.35×pressure + 0.25×utilization`
  (all percentiles). Weights are a judgment call, documented as constants in `scoring.py`.
- **Sensitivity scenarios.** The analyst can ask for custom weights (e.g. "double the
  weight of congestion"); `rank_airports` recomputes a labeled `custom_score` and its
  own rank/ties alongside the default score — same formula, different weight inputs,
  never replacing the canonical score.

"Unmet demand" and "congestion" have no official public metric, so the agent never
invents one — it either presents the proxies above (when asked to rank/recommend) or
pulls qualitative context via web search (when asked a plain "why" question), never both
blended together.

## Key tradeoffs

- **No growth trend.** The only anonymous BTS endpoint is a current-year snapshot, not
  a time series — scoring is cross-sectional (peer comparison), not longitudinal.
- **No true load factor.** No public source publishes seat capacity anonymously.
- **Long-haul share is route count, not passenger volume.** OpenFlights records which
  routes exist, not how often they fly.
- **Turn-based voice, not real-time.** A continuous, interruptible voice conversation
  needs a WebSocket server and streaming STT/TTS — a different, larger project than a
  chat agent with a spoken input/output mode. Voice was called a bonus in the brief.
- **Single local user.** No auth, no multi-tenant isolation — appropriate for a local
  analyst tool, not a deployed service.

## Where AI is used

Claude handles: understanding the question, choosing which tool(s) to call, judging
web-search source credibility (the one place a quality judgment is left to the model
rather than code), and composing the final explanation with citations.

Claude never: computes a score, validates an identifier, or decides what data/tools
are reachable — all of that is plain code (`scoring.py`, `tools.py`'s whitelist,
`agent.py`'s fixed tool schema). This holds for sensitivity scenarios too: Claude only
chooses which weight numbers to pass; `scoring.py` does the arithmetic and ranking.
