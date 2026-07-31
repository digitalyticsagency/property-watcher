"""Site checks against live NSW government data — no API key required.

Ported from the approach used in Parcel Ledger, against the same free endpoints:

  * Zoning + LGA      NSW EPI Primary Planning Layers, MapServer/2
  * Minimum lot size  NSW EPI Primary Planning Layers, MapServer/4
  * Bush fire prone   NSW RFS BFPL register, MapServer/0
  * Flood planning    NSW Planning Hazard layer, MapServer/1
  * Amenities         OpenStreetMap Overpass API

Why this matters more than it looks: the zone code here is *authoritative*.
Everything the granny-flat assessment did before was regex over agent copy, which
is marketing text. A real zone from the state register turns "unclear" into a
defensible answer, and catches lots the listing never mentions are industrial.

Every check degrades independently — a service being down annotates that one
field and never fails the run.
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
    load_config,
    log,
    read_json,
    read_stage,
    write_json,
    write_stage,
)

SVC = "https://mapprod3.environment.nsw.gov.au/arcgis/rest/services/"
ZONE_URL = SVC + "Planning/EPI_Primary_Planning_Layers/MapServer/2/query"
LOT_URL = SVC + "Planning/EPI_Primary_Planning_Layers/MapServer/4/query"
BFPL_URL = SVC + "Fire/BFPL/MapServer/0/query"
HAZARD_URL = SVC + "Planning/Hazard/MapServer/1/query"
OVERPASS_MIRRORS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)

SITE_CACHE = CACHE_DIR / "site_checks.json"

# Once Overpass proves unreachable, stop retrying it for every remaining
# listing. Three mirrors x a 20s timeout x 50 listings would otherwise burn
# nearly an hour of the job budget producing nothing.
_overpass_down = False

# Only these councils publish a flood planning layer to the state dataset, so
# "no flood mapping" elsewhere means unknown, NOT safe.
FLOOD_COUNCILS = {
    "BATHURST REGIONAL", "CLARENCE VALLEY", "FORBES", "HORNSBY",
    "MID-WESTERN REGIONAL", "TAMWORTH REGIONAL", "WENTWORTH",
    "WINGECARRIBEE", "WOLLONGONG", "YASS VALLEY",
}

AMENITY_LABELS = {
    "school": "Schools", "hospital": "Hospitals", "pharmacy": "Pharmacies",
    "doctors": "GPs", "childcare": "Childcare", "fuel": "Fuel",
    "supermarket": "Supermarkets", "station": "Train stations",
    "post_office": "Post offices", "bank": "Banks",
}


def point_query(lat: float, lon: float, fields: str) -> dict[str, str]:
    return {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": fields,
        "returnGeometry": "false",
        "f": "json",
    }


def envelope_query(lat: float, lon: float, d: float, fields: str) -> dict[str, str]:
    return {
        "geometry": f"{lon - d},{lat - d},{lon + d},{lat + d}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": fields,
        "returnGeometry": "false",
        "f": "json",
    }


def get_json(url: str, params: dict[str, str], timeout: int = 25) -> dict[str, Any] | None:
    """Never raises — a dead service annotates one field, it doesn't kill the run."""
    try:
        resp = requests.get(
            url, params=params, timeout=timeout, headers={"Accept": "application/json"}
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("error"):
            log.info("service error from %s: %s", url, data["error"])
            return None
        return data
    except (requests.RequestException, ValueError) as exc:
        log.info("site-check request failed (%s): %s", url.rsplit("/", 3)[-3], exc)
        return None


def first_attrs(data: dict[str, Any] | None) -> dict[str, Any] | None:
    feats = (data or {}).get("features") or []
    return feats[0].get("attributes") if feats else None


# --------------------------------------------------------------------------- checks


def check_zoning(lat: float, lon: float) -> dict[str, Any]:
    attrs = first_attrs(
        get_json(ZONE_URL, point_query(lat, lon, "SYM_CODE,LAY_CLASS,EPI_NAME,LGA_NAME"))
    )
    if not attrs:
        return {"status": "unknown", "code": "", "label": "", "lga": "",
                "detail": "No zone polygon returned for this point."}
    return {
        "status": "ok",
        "code": (attrs.get("SYM_CODE") or "").strip(),
        "label": (attrs.get("LAY_CLASS") or "").strip(),
        "epi": (attrs.get("EPI_NAME") or "").strip(),
        "lga": (attrs.get("LGA_NAME") or "").strip(),
    }


def check_min_lot_size(lat: float, lon: float) -> dict[str, Any]:
    attrs = first_attrs(get_json(LOT_URL, point_query(lat, lon, "LAY_CLASS,SYM_CODE")))
    if not attrs:
        return {"status": "unknown", "text": ""}
    return {
        "status": "ok",
        "text": (attrs.get("LAY_CLASS") or attrs.get("SYM_CODE") or "").strip(),
    }


def check_bushfire(lat: float, lon: float) -> dict[str, Any]:
    attrs = first_attrs(get_json(BFPL_URL, point_query(lat, lon, "Category,d_Category")))
    if attrs:
        cat = attrs.get("Category")
        return {
            "level": "on",
            "text": f"Category {cat} bush fire prone land",
            "detail": (attrs.get("d_Category") or ""),
        }

    near = get_json(BFPL_URL, envelope_query(lat, lon, 0.0025, "Category"))
    feats = (near or {}).get("features") or []
    if feats:
        cats = sorted({str(f["attributes"].get("Category")) for f in feats})
        return {
            "level": "near",
            "text": f"Category {'/'.join(cats)} bush fire prone land within ~250 m",
            "detail": "Proximity still triggers a bush fire assessment in most "
                      "councils. Confirm on the section 10.7 certificate.",
        }
    if near is None:
        return {"level": "unknown", "text": "Bush fire register did not respond",
                "detail": "Re-run later; absence here is not a result."}
    return {
        "level": "clear",
        "text": "Not on mapped bush fire prone land",
        "detail": "Nothing within ~250 m of this point in the RFS register.",
    }


def check_flood(lat: float, lon: float, lga: str) -> dict[str, Any]:
    attrs = first_attrs(
        get_json(HAZARD_URL, point_query(lat, lon, "LAY_CLASS,EPI_NAME,LGA_NAME"))
    )
    if attrs:
        return {
            "level": "on",
            "text": f"Flood planning area — {attrs.get('LAY_CLASS') or 'mapped'}",
            "detail": attrs.get("EPI_NAME") or "",
        }
    if lga and lga.upper() in FLOOD_COUNCILS:
        return {
            "level": "clear",
            "text": "Outside the mapped flood planning area",
            "detail": "This council publishes flood mapping to the state dataset, "
                      "so this is a real result.",
        }
    return {
        "level": "unknown",
        "text": "No state flood mapping for this council",
        "detail": "Only 10 NSW councils publish a flood planning layer. Absence "
                  "here is NOT evidence of safety — the section 10.7 certificate "
                  "is the authoritative answer.",
    }


def check_amenities(lat: float, lon: float, km: float = 10) -> dict[str, Any]:
    radius = int(max(1, min(30, km)) * 1000)
    station_radius = max(radius, 15000)
    query = (
        "[out:json][timeout:30];("
        f'nwr["amenity"~"^(school|hospital|pharmacy|doctors|childcare|fuel|post_office|bank)$"](around:{radius},{lat},{lon});'
        f'nwr["shop"="supermarket"](around:{radius},{lat},{lon});'
        f'nwr["railway"="station"](around:{station_radius},{lat},{lon});'
        ");out center tags 200;"
    )
    # Overpass instances rate-limit and go down independently, and some reject
    # requests without a real User-Agent with a bare 406. Try the mirrors in
    # turn; amenities are the least critical check, so give up quietly.
    global _overpass_down
    if _overpass_down:
        return {}

    elements = None
    for mirror in OVERPASS_MIRRORS:
        try:
            resp = requests.post(
                mirror,
                data={"data": query},
                timeout=20,
                headers={
                    "User-Agent": (
                        "nsw-property-watcher/1.0 "
                        "(+https://github.com/digitalyticsagency/property-watcher)"
                    ),
                    "Accept": "application/json",
                },
            )
            resp.raise_for_status()
            elements = resp.json().get("elements") or []
            break
        except (requests.RequestException, ValueError) as exc:
            log.info("Overpass mirror %s failed: %s", mirror.split("/")[2], exc)

    if elements is None:
        _overpass_down = True
        log.warning(
            "all Overpass mirrors unreachable — amenities will be omitted for the "
            "rest of this run (every other site check is unaffected)"
        )
        return {}

    groups: dict[str, list[dict[str, Any]]] = {}
    for el in elements:
        tags = el.get("tags") or {}
        kind = tags.get("amenity") or tags.get("shop") or tags.get("railway")
        if kind not in AMENITY_LABELS:
            continue
        el_lat = el.get("lat") or (el.get("center") or {}).get("lat")
        el_lon = el.get("lon") or (el.get("center") or {}).get("lon")
        if el_lat is None or el_lon is None:
            continue
        dx = (el_lon - lon) * 111.32 * math.cos(math.radians(lat))
        dy = (el_lat - lat) * 110.574
        groups.setdefault(kind, []).append(
            {"name": tags.get("name") or "(unnamed)", "km": round(math.hypot(dx, dy), 1)}
        )

    return {
        kind: sorted(items, key=lambda x: x["km"])[:5]
        for kind, items in sorted(groups.items())
    }


# --------------------------------------------------------------------------- driver


def run_checks(listing: Listing, cfg: dict[str, Any], cache: dict[str, Any]) -> dict[str, Any]:
    if listing.lat is None or listing.lon is None:
        return {"status": "skipped", "reason": "no coordinates for this listing"}

    key = f"{listing.lat:.5f},{listing.lon:.5f}"
    if key in cache:
        return cache[key]

    log.info("site checks for %s (%s)", listing.title[:60], key)
    zoning = check_zoning(listing.lat, listing.lon)
    result = {
        "status": "ok",
        "zoning": zoning,
        "min_lot_size": check_min_lot_size(listing.lat, listing.lon),
        "bushfire": check_bushfire(listing.lat, listing.lon),
        "flood": check_flood(listing.lat, listing.lon, zoning.get("lga", "")),
        "amenities": check_amenities(
            listing.lat, listing.lon,
            (cfg.get("site_checks") or {}).get("amenity_radius_km", 10),
        ),
    }
    cache[key] = result
    time.sleep(float((cfg.get("site_checks") or {}).get("delay_seconds", 1.0)))
    return result


def process_profile(cfg: dict[str, Any], profile: str, cache: dict[str, Any]) -> list[Listing]:
    listings = read_stage(profile, "geocoded")
    for listing in listings:
        site = run_checks(listing, cfg, cache)
        listing.site = site
        if site.get("status") != "ok":
            continue

        # The state register beats agent copy. Overwrite rather than merge.
        code = (site.get("zoning") or {}).get("code")
        if code:
            listing.zoning_raw = code

        bushfire = (site.get("bushfire") or {}).get("level")
        if bushfire == "on":
            listing.tags = sorted({*listing.tags, "bushfire-prone"})
        elif bushfire == "near":
            listing.tags = sorted({*listing.tags, "bushfire-nearby"})
        if (site.get("flood") or {}).get("level") == "on":
            listing.tags = sorted({*listing.tags, "flood-planning-area"})
    return listings


def main() -> int:
    cfg = load_config()
    if not (cfg.get("site_checks") or {}).get("enabled", True):
        log.info("site_checks disabled in config — passing listings through")
        for profile in cfg["profiles"]:
            write_stage(profile, "checked", read_stage(profile, "geocoded"))
        return 0

    cache = read_json(SITE_CACHE, {})
    for profile in cfg["profiles"]:
        write_stage(profile, "checked", process_profile(cfg, profile, cache))
    write_json(SITE_CACHE, cache)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as exc:
        log.error("%s", exc)
        sys.exit(1)
