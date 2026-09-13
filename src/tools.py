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
    """Current-year BTS traffic totals for one whitelisted airport (live, cached)."""
    airport = _resolve_code(code)
    if airport is None:
        return {"status": "invalid_identifier", "code": code}

    all_traffic = retrieval.fetch_bts_traffic_all()
    row = all_traffic.get(airport.iata_code.upper()) if airport.iata_code else None
    if row is None:
        return {
            "status": "insufficient_data",
            "code": code,
            "reason": "no current-year BTS T-100 record for this airport",
        }

    return {
        "status": "ok",
        "source": "BTS T-100 Domestic Market and Segment Data (NTAD), current-year totals",
        "year": row["year"],
        "iata_code": airport.iata_code,
        "passengers": row["passengers"],
        "departures": row["departures"],
        "arrivals": row["arrivals"],
        "enplanements": row["enplanements"],
    }


def score_airport(code: str) -> dict:
    """Deterministic expansion-candidacy scoring for one whitelisted airport,
    combining BTS traffic, OurAirports runways, and OpenFlights routes."""
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
    utilization = scoring.avg_passengers_per_departure(traffic["passengers"], traffic["departures"])

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

    traffic_percentile = scoring.percentile_rank(traffic["passengers"], passengers_population)
    pressure_percentile = scoring.percentile_rank(pressure, pressure_population) if pressure is not None else None
    utilization_percentile = (
        scoring.percentile_rank(utilization, utilization_population) if utilization is not None else None
    )

    composite = None
    if pressure_percentile is not None and utilization_percentile is not None:
        composite = scoring.composite_expansion_score(traffic_percentile, pressure_percentile, utilization_percentile)

    destinations = []
    for dest_code in _ROUTES.get(airport.iata_code.upper(), []) + _ROUTES.get(airport.icao_code.upper(), []):
        coords = _WORLD_COORDS.get(dest_code.upper())
        if coords:
            destinations.append((dest_code, coords[0], coords[1]))
    long_haul = scoring.long_haul_share(airport.latitude_deg, airport.longitude_deg, destinations)

    return {
        "status": "ok",
        "code": airport.iata_code or airport.icao_code,
        "name": airport.name,
        "year": traffic["year"],
        "sources": [traffic["source"], "OurAirports (bundled)", "OpenFlights routes.dat (bundled)"],
        "inputs": {
            "passengers": traffic["passengers"],
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
            "avg_passengers_per_departure is a utilization proxy, not a true seat-based load factor (no public seat-capacity field was available anonymously).",
            "capacity_pressure is a directional proxy (departures per runway), not an official airfield-capacity figure.",
            "Percentiles are cross-sectional (current year only); no multi-year growth trend is available from the anonymous BTS source used here.",
            "long_haul share reflects OpenFlights' route-network snapshot, not live schedules or passenger volume.",
        ],
    }
