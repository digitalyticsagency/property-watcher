"""Merge all sources per profile, geocode, and apply the drive-time filter.

Merging happens here (not in the fetchers) so each listing keeps its `source`
tag through the merge — the dashboard needs to show "via email alert" vs
"via web search" (Step 3 of the brief).

Distance uses two free services with a graceful ladder:
  1. Nominatim (OpenStreetMap) to geocode the address — cached on disk.
  2. OSRM public routing for real driving time.
  3. Haversine x road-factor / average speed, if OSRM is unavailable.

A listing we cannot place at all is KEPT, not dropped, and flagged — silently
discarding a good property because geocoding hiccuped would be worse than
showing it with an unknown distance.
"""

from __future__ import annotations

import math
import sys
import time
from typing import Any

import requests

from common import (
    CACHE_DIR,
    Listing,
    PipelineError,
    dedupe,
    load_config,
    log,
    profile_config,
    read_json,
    read_stage,
    write_json,
    write_stage,
)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSRM_URL = "https://router.project-osrm.org/route/v1/driving"
GEOCODE_CACHE = CACHE_DIR / "geocode.json"
ROUTE_CACHE = CACHE_DIR / "routes.json"

SOURCE_STAGES = ("email_alerts", "search_api", "domain_api")


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def geocode(query: str, cfg: dict[str, Any], cache: dict[str, Any]) -> tuple[float, float] | None:
    key = query.strip().lower()
    if not key:
        return None
    if key in cache:
        hit = cache[key]
        return (hit[0], hit[1]) if hit else None

    geo_cfg = cfg.get("geocoding") or {}
    try:
        resp = requests.get(
            NOMINATIM_URL,
            params={"q": f"{query}, NSW, Australia", "format": "json", "limit": 1,
                    "countrycodes": "au"},
            headers={"User-Agent": geo_cfg.get("nominatim_user_agent", "nsw-property-watcher/1.0")},
            timeout=20,
        )
        resp.raise_for_status()
        results = resp.json()
    except (requests.RequestException, ValueError) as exc:
        log.info("geocode failed for %r: %s", query, exc)
        return None
    finally:
        # Nominatim's usage policy is a hard 1 request/second.
        time.sleep(1.1)

    if not results:
        cache[key] = None
        return None
    coords = (float(results[0]["lat"]), float(results[0]["lon"]))
    cache[key] = [coords[0], coords[1]]
    return coords


def drive_hours(
    origin: tuple[float, float],
    dest: tuple[float, float],
    cfg: dict[str, Any],
    cache: dict[str, Any],
) -> tuple[float, str]:
    key = f"{origin[0]:.4f},{origin[1]:.4f}->{dest[0]:.4f},{dest[1]:.4f}"
    if key in cache:
        entry = cache[key]
        return entry["hours"], entry["source"]

    geo_cfg = cfg.get("geocoding") or {}
    url = f"{OSRM_URL}/{origin[1]},{origin[0]};{dest[1]},{dest[0]}"
    try:
        resp = requests.get(url, params={"overview": "false"}, timeout=25)
        resp.raise_for_status()
        routes = resp.json().get("routes") or []
        if routes:
            hours = float(routes[0]["duration"]) / 3600.0
            cache[key] = {"hours": hours, "source": "osrm"}
            return hours, "osrm"
    except (requests.RequestException, ValueError, KeyError) as exc:
        log.info("OSRM routing unavailable (%s) — falling back to estimate", exc)

    km = haversine_km(origin, dest) * float(geo_cfg.get("fallback_road_factor", 1.35))
    hours = km / float(geo_cfg.get("fallback_avg_speed_kmh", 70))
    cache[key] = {"hours": hours, "source": "estimated"}
    return hours, "estimated"


def location_query(listing: Listing) -> str:
    """Best available place string: full address, else suburb, else title."""
    for candidate in (listing.address, listing.suburb, listing.title):
        if candidate and len(candidate.strip()) > 4:
            return candidate.strip()
    return ""


def merge_sources(profile: str) -> list[Listing]:
    merged: list[Listing] = []
    for stage in SOURCE_STAGES:
        try:
            items = read_stage(profile, stage)
        except PipelineError:
            log.info("[%s] stage %r absent — skipping", profile, stage)
            continue
        log.info("[%s] %d listings from %s", profile, len(items), stage)
        merged.extend(items)
    return dedupe(merged)


def process_profile(cfg: dict[str, Any], profile: str) -> list[Listing]:
    p = profile_config(cfg, profile)
    geo_cfg = cfg.get("geocoding") or {}
    origin = tuple(geo_cfg.get("sydney_cbd", [-33.8688, 151.2093]))
    limit = float(p["max_drive_hours_from_sydney_cbd"])

    geocode_cache = read_json(GEOCODE_CACHE, {})
    route_cache = read_json(ROUTE_CACHE, {})

    listings = merge_sources(profile)
    kept: list[Listing] = []
    dropped = 0

    for listing in listings:
        if listing.lat is None or listing.lon is None:
            coords = geocode(location_query(listing), cfg, geocode_cache)
            if coords:
                listing.lat, listing.lon = coords

        if listing.lat is None or listing.lon is None:
            listing.distance_source = "unknown"
            listing.tags = sorted({*listing.tags, "location-unconfirmed"})
            kept.append(listing)
            continue

        hours, source = drive_hours(origin, (listing.lat, listing.lon), cfg, route_cache)
        listing.drive_hours = round(hours, 2)
        listing.distance_source = source

        # Estimates are coarse, so give them a 15% grace band before rejecting —
        # a real listing shouldn't be lost to a straight-line approximation.
        threshold = limit * (1.15 if source == "estimated" else 1.0)
        if hours > threshold:
            dropped += 1
            continue
        if hours > limit:
            listing.tags = sorted({*listing.tags, "borderline-distance"})
        kept.append(listing)

    write_json(GEOCODE_CACHE, geocode_cache)
    write_json(ROUTE_CACHE, route_cache)
    log.info(
        "[%s] distance filter (<= %.2f h): kept %d, dropped %d",
        profile, limit, len(kept), dropped,
    )
    return kept


def main() -> int:
    cfg = load_config()
    for profile in cfg["profiles"]:
        write_stage(profile, "geocoded", process_profile(cfg, profile))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as exc:
        log.error("%s", exc)
        sys.exit(1)
