"""Path B (optional) — Domain.com.au Developer API.

Disabled by default: enable by adding `path_b_domain_api` to data_sources in
config.yaml and setting the DOMAIN_API_KEY secret. This is Domain's own
sanctioned API, so unlike scraping it is a legitimate direct-portal route.

Listings from here are verified=True — the data comes from the portal's own
structured response.
"""

from __future__ import annotations

import sys
from typing import Any

import requests

from common import (
    Listing,
    PipelineError,
    dedupe,
    enabled_sources,
    load_config,
    log,
    now_iso,
    profile_config,
    require_env,
    write_stage,
)

SEARCH_URL = "https://api.domain.com.au/v1/listings/residential/_search"


def build_payload(cfg: dict[str, Any], profile: str) -> dict[str, Any]:
    p = profile_config(cfg, profile)
    locations = [
        {"state": "NSW", "region": "", "area": "", "suburb": region,
         "includeSurroundingSuburbs": True}
        for region in p.get("target_regions", [])
        if str(region).strip()
    ]
    payload: dict[str, Any] = {
        "listingType": "Sale",
        "propertyTypes": (
            ["House", "AcreageSemiRural", "VacantLand", "Rural"]
            if profile == "lifestyle_acreage"
            else ["House", "Duplex", "Townhouse"]
        ),
        "minPrice": p.get("budget_min_aud") or 0,
        "maxPrice": p.get("budget_max_aud"),
        "minLandArea": p.get("min_land_size_m2"),
        "locations": locations,
        "pageSize": 100,
    }
    return payload


def fetch_profile(cfg: dict[str, Any], profile: str) -> list[Listing]:
    api_key = require_env("DOMAIN_API_KEY")
    resp = requests.post(
        SEARCH_URL,
        json=build_payload(cfg, profile),
        headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
        timeout=45,
    )
    if resp.status_code >= 400:
        raise PipelineError(
            f"Domain API search failed ({resp.status_code}): {resp.text[:300]}"
        )

    listings: list[Listing] = []
    for entry in resp.json():
        if entry.get("type") != "PropertyListing":
            continue
        core = entry.get("listing") or {}
        prop = core.get("propertyDetails") or {}
        price = core.get("priceDetails") or {}
        url = core.get("listingSlug", "")
        if url and not url.startswith("http"):
            url = f"https://www.domain.com.au/{url.lstrip('/')}"
        if not url:
            continue

        listing = Listing(
            url=url,
            profile=profile,
            source="domain_api",
            title=prop.get("displayableAddress", "") or url,
            address=prop.get("displayableAddress", ""),
            suburb=prop.get("suburb", ""),
            price_text=price.get("displayPrice", ""),
            price_aud=price.get("price") or None,
            land_size_m2=int(prop.get("landArea") or 0) or None,
            bedrooms=prop.get("bedrooms"),
            bathrooms=prop.get("bathrooms"),
            lat=prop.get("latitude"),
            lon=prop.get("longitude"),
            verified=True,
            first_seen=now_iso(),
            last_seen=now_iso(),
            raw={"domain_listing_id": core.get("id")},
        )
        listings.append(listing)

    listings = dedupe(listings)
    log.info("[%s] %d listings from Domain API", profile, len(listings))
    return listings


def main() -> int:
    cfg = load_config()
    if "path_b_domain_api" not in enabled_sources(cfg):
        log.info("path_b_domain_api not enabled — writing empty stages")
        for profile in cfg["profiles"]:
            write_stage(profile, "domain_api", [])
        return 0

    for profile in cfg["profiles"]:
        write_stage(profile, "domain_api", fetch_profile(cfg, profile))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as exc:
        log.error("%s", exc)
        sys.exit(1)
