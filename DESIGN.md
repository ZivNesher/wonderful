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
| [BTS T-100](https://data.bts.gov/d/r495-tyji) (data.bts.gov) | Passenger boardings, departures, seats, and load factor, summed over a trailing 12-month window that moves with the live data (currently 2025-05 to 2026-04) | Public API, queried live at runtime, monthly-updated, free Socrata app token required |

No single free source anonymously provides destination/distance/seats, so three
sources cover what one couldn't: BTS for traffic volume and load factor, OpenFlights +
OurAirports coordinates for long-haul distance, OurAirports for runway capacity.
Per-file provenance: [`data/README.md`](data/README.md).

BTS switched mid-project from a static NTAD snapshot (2024 only, no key, but no
`arrivals`/`seats` either) to BTS's own monthly Socrata feed once we found it: this
data.bts.gov dataset is updated monthly, includes a real seats-based load factor (not
just a proxy), but requires a free app token for any multi-row query (single-airport
lookups are open). It also has no `arrivals` field, so "total flight operations" is no
longer something the agent can state — only departures.

## Scoring methodology

Deterministic Python (`scoring.py`) — Claude narrates these numbers, never invents them.

- **Avg. passengers per departure** = passengers ÷ departures. A utilization proxy used
  for this composite score's percentile methodology. A real seats-based load factor is
  now separately available (`get_traffic_stats`'s `load_factor_pct`, BTS-reported) but
  isn't fed into the composite, to keep the documented weighting/methodology stable.
- **Capacity pressure** = departures ÷ runway count. An infrastructure-strain proxy —
  not an official FAA capacity figure, and not a direct measure of terminal/passenger
  congestion (that would need terminal design capacity, peak-hour volume, or
  gate/security wait-time data, none of which is in this dataset).
- **Traffic/pressure/utilization percentiles** = each metric ranked against all other
  tracked airports (0–100), so differently-scaled numbers combine fairly.
- **Long-haul route share** = of an airport's known nonstop routes, the fraction
  ≥2,500 miles (great-circle, via OurAirports coordinates). Measures route-network
  breadth, not passenger-weighted traffic.
- **Composite expansion-candidacy score** = `0.40×traffic + 0.35×pressure + 0.25×utilization`
  (all percentiles). This 40/35/25 weighting is the canonical, production score — a
  judgment call documented as constants in `scoring.py`.
- **Sensitivity scenarios.** The analyst can ask for custom weights (e.g. "double the
  weight of congestion"); `rank_airports` recomputes a separately labeled `custom_score`
  and its own rank/ties alongside the default score, using the same formula with
  different weight inputs (any KPI not mentioned defaults to 1, i.e. equal weighting
  *for that scenario only*). This is always temporary and additive — it never overwrites
  or is described as the canonical 40/35/25 score.

"Unmet demand" and "congestion" have no official public metric, so the agent never
invents one — it either presents the proxies above (when asked to rank/recommend) or
pulls qualitative context via web search (when asked a plain "why" question), never both
blended together.

## Key tradeoffs

- **No growth trend.** BTS's feed gives a trailing 12-month window, not a multi-year
  time series — scoring is cross-sectional (peer comparison), not longitudinal. This
  window is *not* a calendar year (it moves forward monthly, e.g. 2025-05 to 2026-04)
  and there's no separate "latest complete calendar year" figure available — the agent
  says so plainly rather than reinterpreting the window as one.
- **No arrivals figure.** This BTS feed only reports departures per origin airport, so
  the agent can state "departures from X" but never a "total operations" (departures +
  arrivals) figure — that data simply isn't published here.
- **Outbound-only traffic, not guaranteed unbiased.** `passenger_boardings` counts
  boardings only (no deplanements figure exists). Applying that same definition to
  every airport makes rankings internally consistent, but airports with unusual
  inbound/outbound imbalances could still be over- or under-represented — this is not
  equivalent to ranking by total two-way passenger traffic.
- **Not a financial ROI model.** The composite score measures operational demand,
  traffic intensity, capacity pressure, and utilization proxies — it is not a
  profitability forecast. A real ROI/profitability model would need data this project
  doesn't have: construction cost, financing terms, airport/airline revenue, gate
  economics, operating cost, and project-specific demand forecasts.
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
