from __future__ import annotations

import re

import retrieval
import scoring

_AIRPORTS = retrieval.load_airports_us()
_WORLD_COORDS = retrieval.load_airports_world_coords()
_RUNWAYS = retrieval.load_runway_counts()
_ROUTES = retrieval.load_routes_by_source()

_TYPE_RANK = {"large_airport": 0, "medium_airport": 1, "small_airport": 2}

# Common metro-area shorthand; anything else falls through to whole-word matching.
_METRO_ALIASES = {
    "la": "Los Angeles",
    "nyc": "New York",
    "sf": "San Francisco",
    "dc": "Washington",
    "philly": "Philadelphia",
    "vegas": "Las Vegas",
}

REGIONS: dict[str, list[str]] = {
    "new england": ["CT", "ME", "MA", "NH", "RI", "VT"],
    "mid-atlantic": ["NY", "NJ", "PA", "DE", "MD", "DC", "VA", "WV"],
    "southeast": ["NC", "SC", "GA", "FL", "AL", "MS", "TN", "KY", "AR", "LA"],
    "midwest": ["OH", "IN", "IL", "MI", "WI", "MN", "IA", "MO", "ND", "SD", "NE", "KS"],
    "southwest": ["TX", "OK", "NM", "AZ"],
    "mountain west": ["CO", "UT", "NV", "ID", "MT", "WY"],
    "pacific northwest": ["WA", "OR"],
    "pacific": ["CA", "OR", "WA", "AK", "HI"],
}


def _state_of(airport: retrieval.Airport) -> str:
    """State abbreviation from an airport's iso_region (e.g. "US-CA" -> "CA")."""
    return airport.iso_region.split("-")[-1]


def lookup_airport(query: str) -> dict:
    """Resolve free text (a code, city, or airport name) to a whitelisted airport,
    flagging `ambiguous: true` rather than silently picking one."""
    raw = (query or "").strip()
    if not raw:
        return {"status": "not_found", "query": query}

    code = raw.upper()
    if code in _AIRPORTS:
        a = _AIRPORTS[code]
        return {"status": "ok", "ambiguous": False, "best_match": _airport_summary(a), "candidates": [_airport_summary(a)]}

    search_term = _METRO_ALIASES.get(raw.lower(), raw).lower()
    candidates = []
    seen_idents = set()
    for airport in _AIRPORTS.values():
        if airport.ident in seen_idents:
            continue
        municipality_words = re.findall(r"[a-z]+", airport.municipality.lower())
        name_lower = airport.name.lower()
        is_match = (
            airport.municipality.lower() == search_term
            or search_term in municipality_words
            or search_term in name_lower
        )
        if is_match:
            seen_idents.add(airport.ident)
            candidates.append(airport)

    if not candidates:
        return {"status": "not_found", "query": query}

    candidates.sort(key=lambda a: (_TYPE_RANK.get(a.type, 9), a.name))
    return {
        "status": "ok",
        "ambiguous": len(candidates) > 1,
        "best_match": _airport_summary(candidates[0]),
        "candidates": [_airport_summary(a) for a in candidates[:5]],
    }


def _airport_summary(a: retrieval.Airport) -> dict:
    """Compact, model-facing view of an Airport record."""
    return {
        "iata_code": a.iata_code,
        "icao_code": a.icao_code,
        "name": a.name,
        "municipality": a.municipality,
        "state": _state_of(a),
        "type": a.type,
    }


def _resolve_code(code: str) -> retrieval.Airport | None:
    """Validate a code against the whitelist; None (not an exception) if unknown,
    so callers return invalid_identifier instead of making an external request."""
    if not code or not isinstance(code, str):
        return None
    return _AIRPORTS.get(code.strip().upper())


def get_airport_profile(code: str) -> dict:
    """Metadata + runway summary for one whitelisted airport. No external call."""
    airport = _resolve_code(code)
    if airport is None:
        return {"status": "invalid_identifier", "code": code}

    runway_info = _RUNWAYS.get(airport.ident, {"runway_count": 0, "longest_runway_ft": None})
    return {
        "status": "ok",
        "source": "OurAirports (bundled public-domain snapshot)",
        **_airport_summary(airport),
        "latitude_deg": airport.latitude_deg,
        "longitude_deg": airport.longitude_deg,
        "runway_count": runway_info["runway_count"],
        "longest_runway_ft": runway_info["longest_runway_ft"],
    }


def list_airports_in_region(region: str) -> dict:
    """Whitelisted airports whose state falls in a known static region grouping."""
    key = (region or "").strip().lower()
    states = REGIONS.get(key)
    if states is None:
        return {"status": "unknown_region", "region": region, "known_regions": sorted(REGIONS)}

    matches = [a for a in _AIRPORTS.values() if _state_of(a) in states]
    # de-dupe (airports appear twice in _AIRPORTS, once per code)
    seen = {}
    for a in matches:
        seen[a.ident] = a
    airports = sorted(seen.values(), key=lambda a: (_TYPE_RANK.get(a.type, 9), a.name))
    return {
        "status": "ok",
        "region": region,
        "states": states,
        "airports": [_airport_summary(a) for a in airports],
    }


def get_traffic_stats(code: str) -> dict:
    """BTS T-100 traffic totals for one whitelisted airport -- trailing-12-month
    totals queried live from BTS's own monthly-updated Socrata feed
    (data.bts.gov), not a fixed calendar year."""
    airport = _resolve_code(code)
    if airport is None:
        return {"status": "invalid_identifier", "code": code}

    all_traffic = retrieval.fetch_bts_traffic_all()
    row = all_traffic.get(airport.iata_code.upper()) if airport.iata_code else None
    if row is None:
        return {
            "status": "insufficient_data",
            "code": code,
            "reason": "no BTS T-100 record for this airport in the trailing 12-month window",
        }

    return {
        "status": "ok",
        "source": "BTS T-100 Segment Summary By Origin Airport (data.bts.gov), trailing 12 months",
        "period": f"{row['period_start']} to {row['period_end']}",
        "iata_code": airport.iata_code,
        "passenger_boardings": row["passengers"],
        "departures": row["departures"],
        "seats": row["seats"],
        "load_factor_pct": row["load_factor"],
        "caveats": [
            "'passenger_boardings' counts passengers boarding AT this airport (domestic + "
            "international combined), summed over the trailing 12 months -- not a two-way "
            "total (no deplanements figure exists in this dataset). Never call it 'total "
            "passenger traffic' or 'passengers handled'; say 'passenger boardings' and name "
            "the period.",
            "'departures' is a flight-operation count (all classes), not a passenger count. "
            "This dataset has no separate arrivals figure, so state 'departures FROM this "
            "airport', never a 'total operations' figure that would need arrivals too.",
            "'load_factor_pct' is BTS's own reported seats-filled percentage for this period "
            "-- a real measured load factor, not the avg_passengers_per_departure proxy "
            "score_airport uses (kept for methodology consistency with existing percentiles).",
            "'period' is a trailing 12-month window (it moves forward as new BTS data arrives "
            "each month), NOT a calendar year -- never call it a 'complete calendar year' or "
            "'latest complete calendar year'. This data source doesn't separately expose a "
            "calendar-year figure; if asked for one, say plainly that only the trailing-12-month "
            "period is available, rather than reinterpreting it as a calendar year.",
        ],
    }


def _normalize_weights(weights) -> tuple[float, float, float] | None:
    """Turn a model-supplied {traffic, capacity_pressure, utilization} dict into
    a (traffic, pressure, utilization) tuple for a sensitivity scenario, defaulting
    any KPI not mentioned to 1 (equal weighting) -- None if nothing usable was given,
    so the caller falls back to the canonical score untouched."""
    if not weights or not isinstance(weights, dict):
        return None
    try:
        w = (
            float(weights.get("traffic", 1)),
            float(weights.get("capacity_pressure", 1)),
            float(weights.get("utilization", 1)),
        )
    except (TypeError, ValueError):
        return None
    if any(x < 0 for x in w) or sum(w) == 0:
        return None
    return w


def score_airport(code: str, weights: dict | None = None) -> dict:
    """Deterministic expansion-candidacy scoring for one whitelisted airport,
    combining BTS traffic, OurAirports runways, and OpenFlights routes. An
    optional `weights` dict adds a custom sensitivity-scenario score alongside
    (never instead of) the canonical expansion_candidacy_score."""
    airport = _resolve_code(code)
    if airport is None:
        return {"status": "invalid_identifier", "code": code}

    traffic = get_traffic_stats(code)
    if traffic["status"] != "ok":
        return {"status": "insufficient_data", "code": code, "reason": traffic.get("reason", "no traffic data")}

    runway_info = _RUNWAYS.get(airport.ident, {"runway_count": 0, "longest_runway_ft": None})
    runway_count = runway_info["runway_count"] or None

    all_traffic = retrieval.fetch_bts_traffic_all()
    passengers_population = [r["passengers"] for r in all_traffic.values() if r.get("passengers")]
    departures_population = [r["departures"] for r in all_traffic.values() if r.get("departures")]

    pressure = scoring.capacity_pressure(traffic["departures"], runway_count)
    utilization = scoring.avg_passengers_per_departure(traffic["passenger_boardings"], traffic["departures"])

    pressure_population = []
    for iata, row in all_traffic.items():
        peer_airport = _AIRPORTS.get(iata)
        if not peer_airport:
            continue
        peer_runways = _RUNWAYS.get(peer_airport.ident, {}).get("runway_count")
        p = scoring.capacity_pressure(row.get("departures", 0), peer_runways)
        if p is not None:
            pressure_population.append(p)

    utilization_population = [
        u for u in (
            scoring.avg_passengers_per_departure(r.get("passengers", 0), r.get("departures", 0))
            for r in all_traffic.values()
        ) if u is not None
    ]

    traffic_percentile = scoring.percentile_rank(traffic["passenger_boardings"], passengers_population)
    pressure_percentile = scoring.percentile_rank(pressure, pressure_population) if pressure is not None else None
    utilization_percentile = (
        scoring.percentile_rank(utilization, utilization_population) if utilization is not None else None
    )

    composite = None
    if pressure_percentile is not None and utilization_percentile is not None:
        composite = scoring.composite_expansion_score(traffic_percentile, pressure_percentile, utilization_percentile)

    custom_scenario = None
    custom_weights = _normalize_weights(weights)
    if custom_weights is not None and composite is not None:
        custom_score = scoring.composite_expansion_score(
            traffic_percentile, pressure_percentile, utilization_percentile, weights=custom_weights
        )
        custom_scenario = {
            "label": "custom sensitivity scenario -- NOT the default methodology",
            "weights_used": {
                "traffic": custom_weights[0],
                "capacity_pressure": custom_weights[1],
                "utilization": custom_weights[2],
            },
            "custom_score": round(custom_score, 1),
        }

    destinations = []
    for dest_code in _ROUTES.get(airport.iata_code.upper(), []) + _ROUTES.get(airport.icao_code.upper(), []):
        coords = _WORLD_COORDS.get(dest_code.upper())
        if coords:
            destinations.append((dest_code, coords[0], coords[1]))
    long_haul = scoring.long_haul_share(airport.latitude_deg, airport.longitude_deg, destinations)

    result = {
        "status": "ok",
        "code": airport.iata_code or airport.icao_code,
        "name": airport.name,
        "period": traffic["period"],
        "sources": [traffic["source"], "OurAirports (bundled)", "OpenFlights routes.dat (bundled)"],
        "inputs": {
            "passenger_boardings": traffic["passenger_boardings"],
            "departures": traffic["departures"],
            "runway_count": runway_count,
        },
        "avg_passengers_per_departure": utilization,
        "capacity_pressure_departures_per_runway": pressure,
        "traffic_percentile_vs_peers": round(traffic_percentile, 1),
        "capacity_pressure_percentile_vs_peers": round(pressure_percentile, 1) if pressure_percentile is not None else None,
        "utilization_percentile_vs_peers": round(utilization_percentile, 1) if utilization_percentile is not None else None,
        "expansion_candidacy_score": round(composite, 1) if composite is not None else None,
        "long_haul": long_haul,
        "caveats": [
            "avg_passengers_per_departure is a utilization proxy used for this composite score's "
            "percentile methodology; a real seats-based load factor is separately available from "
            "get_traffic_stats (load_factor_pct), just not fed into this composite for consistency "
            "with the documented weighting.",
            "capacity_pressure is a directional proxy (departures per runway), not an official airfield-capacity figure.",
            "Percentiles are cross-sectional (this trailing-12-month period only, not a time series); "
            "no multi-year growth trend is available from this BTS feed.",
            "long_haul share reflects OpenFlights' route-network snapshot, not live schedules or passenger volume.",
            "These are throughput/capacity-pressure proxies, not a direct measure of terminal or "
            "passenger congestion -- that would need terminal design capacity, peak-hour passenger "
            "volume, gate utilization, or security wait-time data, none of which is in this dataset.",
            "traffic_percentile_vs_peers is based on outbound passenger_boardings only. Applying "
            "the same outbound-only definition to every airport makes the comparison internally "
            "consistent, but that does NOT guarantee the ranking is unbiased -- airports with "
            "unusual inbound/outbound imbalances could still be affected, so this is not "
            "equivalent to ranking by total two-way passenger traffic.",
        ],
    }
    if custom_scenario is not None:
        result["custom_scenario"] = custom_scenario
    return result


def rank_airports(codes: list, weights: dict | None = None) -> dict:
    """Deterministically rank 2+ whitelisted airports by expansion_candidacy_score
    (and, if `weights` is given, by the matching custom sensitivity score) -- exact
    rank numbers and ties are computed in code via scoring.rank_by_score, not
    inferred by the model. Reuses score_airport for every number; no new scoring
    logic lives here."""
    if not codes or not isinstance(codes, list):
        return {"status": "error", "reason": "codes must be a non-empty list of airport codes"}

    scored = {}
    for code in codes:
        result = score_airport(code, weights=weights)
        if result["status"] != "ok":
            return {"status": "insufficient_data", "code": code, "reason": result.get("reason", "scoring failed")}
        scored[result["code"]] = result

    response = {
        "status": "ok",
        "default_ranking": scoring.rank_by_score(
            [(code, r["expansion_candidacy_score"]) for code, r in scored.items()]
        ),
    }

    custom_items = [(code, r["custom_scenario"]["custom_score"]) for code, r in scored.items() if "custom_scenario" in r]
    if custom_items:
        response["custom_ranking"] = scoring.rank_by_score(custom_items)
        response["custom_scenario_label"] = next(iter(scored.values()))["custom_scenario"]["label"]
        response["weights_used"] = next(iter(scored.values()))["custom_scenario"]["weights_used"]

    return response
