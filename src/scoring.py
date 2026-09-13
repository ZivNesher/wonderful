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
    """Departures per usable runway, current year -- an infrastructure-strain
    proxy, not an engineering capacity study (real throughput depends on more)."""
    if not runway_count:
        return None
    return departures / runway_count


def percentile_rank(value: float, population: list[float]) -> float:
    """0-100 percentile of `value` within `population` -- puts differently-scaled
    KPIs on a comparable footing before combining them."""
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
    """Share of an airport's known nonstop routes that are long-haul by
    great-circle distance -- a route-network measure, not passenger-weighted."""
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


def rank_by_score(scored_items: list[tuple[str, float]]) -> list[dict]:
    """Standard competition ranking (ties share a rank; the next distinct rank
    skips accordingly, e.g. 1, 2, 2, 4) over (code, score) pairs, highest score
    first. Exists so position/tie comparisons are computed once, in code, rather
    than inferred by the model from a list of individual scores."""
    ranked = sorted(scored_items, key=lambda item: item[1], reverse=True)
    result = []
    for i, (code, score) in enumerate(ranked):
        rank = i + 1 if i == 0 or score != ranked[i - 1][1] else result[-1]["rank"]
        result.append({"code": code, "score": score, "rank": rank})
    return result


def composite_expansion_score(
    traffic_percentile: float,
    capacity_pressure_percentile: float,
    utilization_percentile: float,
    weights: tuple[float, float, float] | None = None,
) -> float:
    """Weighted 0-100 expansion-candidacy score from three peer-percentile
    inputs. Defaults to the canonical 40/35/25 traffic/pressure/utilization
    weighting (see DESIGN.md) when `weights` is omitted. A custom (traffic,
    pressure, utilization) tuple can be passed for a what-if sensitivity
    scenario -- weights are normalized by their own sum, so e.g. (1, 2, 1)
    (double weight on capacity pressure) works the same as the default triple
    summing to 1.0."""
    w_traffic, w_pressure, w_utilization = weights or (
        WEIGHT_TRAFFIC_INTENSITY, WEIGHT_CAPACITY_PRESSURE, WEIGHT_UTILIZATION
    )
    total_weight = w_traffic + w_pressure + w_utilization
    return (
        w_traffic * traffic_percentile
        + w_pressure * capacity_pressure_percentile
        + w_utilization * utilization_percentile
    ) / total_weight
