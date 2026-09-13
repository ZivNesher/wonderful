from __future__ import annotations

import csv
import json
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path

import requests

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache"
CACHE_TTL_SECONDS = 24 * 60 * 60

# BTS T-100 Segment Summary By Origin Airport, hosted live on BTS's own Socrata
# portal -- updated monthly (unlike the old ArcGIS snapshot, capped at 2024).
# Single-record lookups work unauthenticated, but any $where/aggregate query
# (needed for a trailing-12-month sum across airports) requires a free Socrata
# app token -- see BTS_SOCRATA_APP_TOKEN in .env.example.
BTS_SOCRATA_BASE = "https://data.bts.gov/resource/r495-tyji.json"
BTS_TRAILING_MONTHS = 12


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
    """Read a CSV file into a list of dict rows."""
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_airports_us() -> dict[str, Airport]:
    """US airports with scheduled commercial service (the scoring whitelist),
    keyed by both IATA and ICAO code (uppercase)."""
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
    """Coordinate lookup for ANY airport worldwide by IATA/ICAO code -- needed
    for international long-haul destinations, unlike the US-only whitelist."""
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
    """Per-airport runway count/longest-length, keyed by `ident`. Closed
    runways are excluded -- they don't contribute usable capacity."""
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


def _socrata_headers() -> dict:
    token = os.environ.get("BTS_SOCRATA_APP_TOKEN")
    return {"X-App-Token": token} if token else {}


def _months_before(date_str: str, months_back: int) -> str:
    """`date_str` (YYYY-MM-...) minus `months_back` months, as YYYY-MM-01."""
    year, month = int(date_str[:4]), int(date_str[5:7])
    total = year * 12 + (month - 1) - months_back
    year, month = divmod(total, 12)
    return f"{year:04d}-{month + 1:02d}-01"


def _latest_reporting_month() -> str:
    """YYYY-MM-DD of the most recent month present in the BTS T-100 dataset --
    queried live so the trailing window always anchors to real data, not an
    assumed publication lag."""
    response = requests.get(
        BTS_SOCRATA_BASE,
        params={"$select": "max(reporting_month) as latest"},
        headers=_socrata_headers(),
        timeout=20,
    )
    response.raise_for_status()
    return response.json()[0]["latest"][:10]


def fetch_bts_traffic_all() -> dict[str, dict]:
    """Bulk-fetch trailing-12-month BTS T-100 origin-airport totals (one grouped
    SoQL query against the live, monthly-updated data.bts.gov feed), keyed by
    IATA code, and cache it to disk for 24h."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / "bts_all.json"
    if cache_path.exists() and (time.time() - cache_path.stat().st_mtime) < CACHE_TTL_SECONDS:
        return json.loads(cache_path.read_text())["by_code"]

    period_end = _latest_reporting_month()
    period_start = _months_before(period_end, BTS_TRAILING_MONTHS - 1)

    response = requests.get(
        BTS_SOCRATA_BASE,
        params={
            "$select": (
                "origin_airport_code, sum(total_departures) as departures, "
                "sum(total_passengers) as passengers, sum(total_seats) as seats"
            ),
            "$where": f"reporting_month >= '{period_start}' AND reporting_month <= '{period_end}'",
            "$group": "origin_airport_code",
            "$limit": 5000,
        },
        headers=_socrata_headers(),
        timeout=30,
    )
    response.raise_for_status()

    by_code: dict[str, dict] = {}
    for row in response.json():
        code = (row.get("origin_airport_code") or "").strip().upper()
        if not code:
            continue
        departures = int(row.get("departures") or 0)
        passengers = int(row.get("passengers") or 0)
        seats = int(row.get("seats") or 0)
        by_code[code] = {
            "departures": departures,
            "passengers": passengers,
            "seats": seats,
            "load_factor": round(100 * passengers / seats, 1) if seats else None,
            "period_start": period_start[:7],
            "period_end": period_end[:7],
        }

    cache_path.write_text(json.dumps({"fetched_at": time.time(), "by_code": by_code}))
    return by_code
