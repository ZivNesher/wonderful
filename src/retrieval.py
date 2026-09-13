"""Loads the bundled public-data snapshots and queries the one live public API.

Three sources, all read-only, all covered in DESIGN.md:
  - OurAirports snapshot (data/airports_us.csv, data/runways_us.csv, data/airports_world.csv)
  - OpenFlights route snapshot (data/routes.csv)
  - BTS T-100 (NTAD ArcGIS FeatureServer) -- the one live call, cached to disk.

Nothing here talks to the model. This module only returns plain data structures;
`tools.py` is what the agent is allowed to call, and it validates every identifier
against the dictionaries loaded here before any external request is made.
"""
from __future__ import annotations

import csv
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path

import requests

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache"
CACHE_TTL_SECONDS = 24 * 60 * 60

BTS_FEATURE_SERVER = (
    "https://services.arcgis.com/xOi1kZaI0eWDREZv/ArcGIS/rest/services/"
    "T100_Domestic_Market_and_Segment_Data/FeatureServer/1/query"
)


@dataclass(frozen=True)
class Airport:
    ident: str
    type: str
    name: str
    latitude_deg: float
    longitude_deg: float
    iso_region: str
    municipality: str
    icao_code: str
    iata_code: str


def _read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_airports_us() -> dict[str, Airport]:
    """US airports with scheduled commercial service -- the scoring whitelist.

    Keyed by both IATA and ICAO code (uppercase) so callers can look up either.
    """
    by_code: dict[str, Airport] = {}
    for row in _read_csv(DATA_DIR / "airports_us.csv"):
        try:
            airport = Airport(
                ident=row["ident"],
                type=row["type"],
                name=row["name"],
                latitude_deg=float(row["latitude_deg"]),
                longitude_deg=float(row["longitude_deg"]),
                iso_region=row["iso_region"],
                municipality=row["municipality"],
                icao_code=row["icao_code"],
                iata_code=row["iata_code"],
            )
        except (ValueError, KeyError):
            continue
        if airport.iata_code:
            by_code[airport.iata_code.upper()] = airport
        if airport.icao_code:
            by_code[airport.icao_code.upper()] = airport
    return by_code


def load_airports_world_coords() -> dict[str, tuple[float, float]]:
    """Coordinate lookup for ANY airport (incl. international) by IATA/ICAO code.

    Needed because long-haul routes out of a US airport are frequently
    international, and the US-only whitelist above must not be used to resolve
    destination coordinates.
    """
    coords: dict[str, tuple[float, float]] = {}
    for row in _read_csv(DATA_DIR / "airports_world.csv"):
        try:
            lat, lon = float(row["latitude_deg"]), float(row["longitude_deg"])
        except ValueError:
            continue
        if row["iata_code"]:
            coords[row["iata_code"].upper()] = (lat, lon)
        if row["icao_code"]:
            coords[row["icao_code"].upper()] = (lat, lon)
    return coords


def load_runway_counts() -> dict[str, dict]:
    """Per-airport runway summary keyed by `ident` (OurAirports identifier).

    Returns {ident: {"runway_count": int, "longest_runway_ft": float | None}}.
    Closed runways are excluded -- they don't contribute usable capacity.
    """
    summary: dict[str, dict] = {}
    for row in _read_csv(DATA_DIR / "runways_us.csv"):
        if row.get("closed") == "1":
            continue
        ident = row["airport_ident"]
        entry = summary.setdefault(ident, {"runway_count": 0, "longest_runway_ft": None})
        entry["runway_count"] += 1
        try:
            length = float(row["length_ft"])
        except (ValueError, TypeError):
            length = None
        if length is not None:
            if entry["longest_runway_ft"] is None or length > entry["longest_runway_ft"]:
                entry["longest_runway_ft"] = length
    return summary


def load_routes_by_source() -> dict[str, list[str]]:
    """Nonstop route destinations keyed by source code (IATA/ICAO as OpenFlights used it)."""
    routes: dict[str, list[str]] = {}
    for row in _read_csv(DATA_DIR / "routes.csv"):
        routes.setdefault(row["source"].upper(), []).append(row["dest"].upper())
    return routes


def great_circle_distance_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine great-circle distance in statute miles."""
    radius_miles = 3958.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius_miles * math.asin(math.sqrt(a))


def _fetch_bts_page(offset: int, page_size: int = 1000) -> list[dict]:
    params = {
        "where": "1=1",
        "outFields": "year,origin,enplanements,passengers,departures,arrivals,freight,mail",
        "resultOffset": offset,
        "resultRecordCount": page_size,
        "f": "json",
    }
    response = requests.get(BTS_FEATURE_SERVER, params=params, timeout=20)
    response.raise_for_status()
    payload = response.json()
    return [f["attributes"] for f in payload.get("features", [])]


def fetch_bts_traffic_all() -> dict[str, dict]:
    """Bulk-fetch the entire BTS T-100 (NTAD ArcGIS) origin-airport table once,
    keyed by IATA code. One paginated fetch (~2 requests, the service caps pages
    at 1000 rows) instead of one HTTP call per airport -- this is both faster for
    a chat session that touches several airports and is what makes peer-percentile
    scoring possible without hundreds of live round-trips.

    Cached whole to disk with a 24h TTL.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / "bts_all.json"
    if cache_path.exists() and (time.time() - cache_path.stat().st_mtime) < CACHE_TTL_SECONDS:
        return json.loads(cache_path.read_text())["by_code"]

    by_code: dict[str, dict] = {}
    offset = 0
    while True:
        page = _fetch_bts_page(offset)
        if not page:
            break
        for row in page:
            origin = row.get("origin")
            if origin:
                by_code[origin.strip().upper()] = row
        if len(page) < 1000:
            break
        offset += 1000

    cache_path.write_text(json.dumps({"fetched_at": time.time(), "by_code": by_code}))
    return by_code
