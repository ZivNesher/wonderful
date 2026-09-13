"""Deterministic scoring/ranking logic. No LLM calls anywhere in this file.

The model (agent.py) only ever narrates the numbers these functions produce; it
never computes or overrides them. See DESIGN.md for the methodology writeup and
the rationale for each KPI and weight.

Every KPI here is a documented proxy for something not directly published by any
free public source (see plan §12 / README "known limitations") -- the functions
are named and shaped to make that explicit rather than implying an official
metric exists.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from retrieval import great_circle_distance_miles

LONG_HAUL_THRESHOLD_MILES = 2500  # chosen convention, documented in DESIGN.md

# Composite "expansion candidacy" weights -- must sum to 1.0. See DESIGN.md.
WEIGHT_TRAFFIC_INTENSITY = 0.40
WEIGHT_CAPACITY_PRESSURE = 0.35
WEIGHT_UTILIZATION = 0.25


@dataclass
class AirportKpis:
    code: str
    year: int
    passengers: int
    departures: int
    enplanements: int
    runway_count: int | None
    longest_runway_ft: float | None
    avg_passengers_per_departure: float | None
    capacity_pressure: float | None  # departures per runway
    long_haul: dict = field(default_factory=dict)  # share/count/total, or "insufficient_data"


def avg_passengers_per_departure(passengers: int, departures: int) -> float | None:
    """Utilization proxy -- NOT a true seat-based load factor (no seats field
    exists in the anonymous BTS extract this project uses; see README)."""
    if not departures:
        return None
    return passengers / departures


def capacity_pressure(departures: int, runway_count: int | None) -> float | None:
    """Departures per usable runway, current year -- infrastructure-strain proxy.

    Caveat (documented, not hidden): real airport throughput depends on airspace,
    gates, staffing, and weather, none of which are in these datasets. This is a
    directional signal, not an engineering capacity study.
    """
    if not runway_count:
        return None
    return departures / runway_count


def percentile_rank(value: float, population: list[float]) -> float:
    """0-100 percentile of `value` within `population` (inclusive, simple rank).

    Used to put traffic, capacity pressure, and utilization on a comparable
    0-100 scale before combining them -- avoids arbitrary unit mixing.
    """
    if not population:
        return 0.0
    at_or_below = sum(1 for v in population if v <= value)
    return 100.0 * at_or_below / len(population)


def long_haul_share(
    origin_lat: float,
    origin_lon: float,
    destinations: list[tuple[str, float, float]],
    threshold_miles: float = LONG_HAUL_THRESHOLD_MILES,
) -> dict:
    """Share of an airport's known nonstop ROUTES (OpenFlights snapshot) that are
    long-haul by great-circle distance. This is a route-network measure, not a
    flights/passengers-weighted measure -- see README for why.

    `destinations`: list of (code, lat, lon) for each nonstop route out of the
    origin. Routes whose destination coordinates are unknown are skipped, not
    silently counted as short-haul.
    """
    if not destinations:
        return {"status": "insufficient_data", "reason": "no known routes for this airport"}

    long_haul = []
    all_routes = []
    for dest_code, dest_lat, dest_lon in destinations:
        distance = great_circle_distance_miles(origin_lat, origin_lon, dest_lat, dest_lon)
        all_routes.append((dest_code, distance))
        if distance >= threshold_miles:
            long_haul.append((dest_code, distance))

    return {
        "status": "ok",
        "threshold_miles": threshold_miles,
        "total_routes": len(all_routes),
        "long_haul_routes": len(long_haul),
        "share": len(long_haul) / len(all_routes),
        "long_haul_destinations": sorted(
            [code for code, _ in long_haul]
        ),
    }


def composite_expansion_score(
    traffic_percentile: float,
    capacity_pressure_percentile: float,
    utilization_percentile: float,
) -> float:
    """Weighted 0-100 expansion-candidacy score from three peer-percentile inputs.

    Weights (see DESIGN.md for rationale):
      40% traffic intensity  -- raw demand signal
      35% capacity pressure  -- infrastructure strain signal
      25% utilization        -- how full current operations already run
    """
    return (
        WEIGHT_TRAFFIC_INTENSITY * traffic_percentile
        + WEIGHT_CAPACITY_PRESSURE * capacity_pressure_percentile
        + WEIGHT_UTILIZATION * utilization_percentile
    )
