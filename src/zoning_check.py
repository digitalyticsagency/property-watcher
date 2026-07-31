"""Granny-flat feasibility for the house_with_granny_flat profile only.

Constraint #8: "potential for a granny flat" must be actual zoning reasoning
against NSW secondary dwelling rules, and the output is always one of
"confirmed" / "likely" / "unclear — check with council". Never a guarantee.

Rules encoded (as at the 2026 SEPP consolidation — verify against the current
instrument before relying on it):

  * SEPP (Housing) 2021, Ch.3 Pt.5 — a secondary dwelling is permitted WITH
    CONSENT in zones where a dwelling house is permitted: R1-R5, RU1-RU6, C4.
  * Floor area cap: the greater of 60 m2, or 5% of the lot area.
  * The complying-development pathway (Codes SEPP 2008) additionally wants a
    minimum lot size around 450 m2 and frontage/setback compliance.

Why this stays conservative: zone codes alone don't settle it. The applicable
LEP for the specific lot can carry a minimum-lot-size clause, and overlays
(heritage, flood, bushfire, sewer) can defeat an otherwise-permissible proposal.
So the ceiling on any inference from a listing is "likely", never "confirmed" —
"confirmed" is reserved for listings that state an existing secondary dwelling.
"""

from __future__ import annotations

import re
import sys
from typing import Any

from common import (
    Listing,
    PipelineError,
    load_config,
    log,
    read_stage,
    write_stage,
)

# Zones where a dwelling house — and therefore a secondary dwelling — is
# permitted with consent.
SECONDARY_DWELLING_ZONES = {
    "R1", "R2", "R3", "R4", "R5",
    "RU1", "RU2", "RU3", "RU4", "RU5", "RU6",
    "C4",  # Environmental Living
}

# NSW Employment Zones Reform renumbered the E-codes. The OLD E3/E4
# (Environmental Management / Environmental Living) became C3/C4, and E1-E5 were
# reissued as commercial and industrial zones. Treating a modern E4 as
# residential would call a General Industrial lot granny-flat-ready, so the new
# codes are named explicitly and always rejected.
REASSIGNED_E_ZONES = {
    "E1": "Local Centre", "E2": "Commercial Centre", "E3": "Productivity Support",
    "E4": "General Industrial", "E5": "Heavy Industrial",
}

MIN_COMPLYING_LOT_M2 = 450
MIN_SECONDARY_DWELLING_M2 = 60

# Phrases that assert a secondary dwelling already exists on title.
EXISTING_PATTERNS = [
    r"\bgranny flat\b",
    r"\bsecondary dwelling\b",
    r"\bself[- ]contained (?:studio|flat|unit|cottage)\b",
    r"\bdual (?:occupancy|living|key)\b",
    r"\bteenage retreat with (?:kitchen|kitchenette)\b",
    r"\bin[- ]law (?:suite|accommodation)\b",
    r"\bstudio (?:apartment|flat) at (?:the )?rear\b",
]

# Phrases that only claim *potential* — not evidence of an existing dwelling.
POTENTIAL_PATTERNS = [
    r"\bpotential (?:for|to build)(?:[^.]{0,30})granny flat\b",
    r"\bgranny flat potential\b",
    r"\broom for a granny flat\b",
    r"\bscope for (?:a )?secondary dwelling\b",
    r"\bstca\b",  # "subject to council approval"
    r"\bsubject to council approval\b",
]

_ZONE_RE = re.compile(r"\b(R[1-5]|RU[1-6]|C[1-4]|E[1-4]|B[1-8]|IN[1-3]|SP[1-3])\b")


def _matches(patterns: list[str], text: str) -> str | None:
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return m.group(0)
    return None


def detect_zone(listing: Listing) -> tuple[str, bool]:
    """Return (zone_code, is_authoritative).

    Authoritative means it came from the NSW planning register via site_checks,
    not from a regex over agent marketing copy.
    """
    official = ((listing.site or {}).get("zoning") or {}).get("code", "")
    if official:
        return official.upper(), True
    if listing.zoning_raw:
        m = _ZONE_RE.search(listing.zoning_raw.upper())
        if m:
            return m.group(1), False
    return "", False


def assess(listing: Listing) -> tuple[str, str]:
    """Return (status, reasoning). Status is confirmed | likely | unclear."""
    text = " ".join(
        filter(None, [listing.title, listing.snippet, listing.address, listing.zoning_raw])
    )
    zone, authoritative = detect_zone(listing)
    land = listing.land_size_m2
    notes: list[str] = []

    existing = _matches(EXISTING_PATTERNS, text)
    potential = _matches(POTENTIAL_PATTERNS, text)

    # A "potential" phrase containing the words "granny flat" also trips the
    # existing-patterns regex, so potential wins when both match.
    if existing and not potential:
        if not listing.verified:
            return (
                "unclear — check with council",
                f"A search snippet mentions {existing!r}, but the listing page was "
                "never successfully fetched, so this is unconfirmed text. Open the "
                "listing to check whether the dwelling exists and is approved.",
            )
        notes.append(f"listing states {existing!r}")
        notes.append(
            "confirm it is an APPROVED secondary dwelling on title — an unapproved "
            "conversion is a liability, not an asset"
        )
        return "confirmed", "; ".join(notes)

    if potential:
        notes.append(f"listing claims potential ({potential!r}) — agent copy, not a determination")

    if zone and zone in REASSIGNED_E_ZONES:
        notes.append(
            f"zone {zone} is {REASSIGNED_E_ZONES[zone]} under the NSW Employment "
            "Zones Reform — not a residential zone, and a secondary dwelling is "
            "not permitted"
        )
        return "unclear — check with council", "; ".join(notes)

    if zone:
        source = (
            "NSW planning register" if authoritative else "listing text (unverified)"
        )
        notes.append(f"zone {zone} from {source}")
        if zone in SECONDARY_DWELLING_ZONES:
            notes.append(
                f"zone {zone} permits a secondary dwelling with consent under "
                "SEPP (Housing) 2021 Ch.3 Pt.5"
            )
            if land:
                if land >= MIN_COMPLYING_LOT_M2:
                    cap = max(MIN_SECONDARY_DWELLING_M2, int(land * 0.05))
                    notes.append(
                        f"{land} m2 lot clears the ~{MIN_COMPLYING_LOT_M2} m2 "
                        f"complying-development threshold; floor area cap ~{cap} m2"
                    )
                    notes.append(
                        "still subject to the LEP minimum lot size for THIS lot, plus "
                        "setbacks, sewer, flood and bushfire overlays"
                    )
                    return "likely", "; ".join(notes)
                notes.append(
                    f"{land} m2 lot is below the ~{MIN_COMPLYING_LOT_M2} m2 "
                    "complying-development threshold — a DA may still succeed"
                )
                return "unclear — check with council", "; ".join(notes)
            notes.append("land size unknown, so the lot-size test cannot be applied")
            return "unclear — check with council", "; ".join(notes)

        notes.append(
            f"zone {zone} is not one where a secondary dwelling is permitted under "
            "SEPP (Housing) 2021"
        )
        return "unclear — check with council", "; ".join(notes)

    notes.append("no zoning information found in the listing")
    if land and land >= MIN_COMPLYING_LOT_M2:
        notes.append(
            f"{land} m2 lot would be large enough IF the zone permits it — "
            "look up the lot on the NSW Planning Portal"
        )
    return "unclear — check with council", "; ".join(notes)


def process_profile(profile: str) -> list[Listing]:
    listings = read_stage(profile, "checked")

    if profile != "house_with_granny_flat":
        # The acreage profile treats a granny flat as a nice-to-have, so it is
        # scored by the ranker rather than gated here.
        return listings

    counts: dict[str, int] = {}
    for listing in listings:
        status, reasoning = assess(listing)
        listing.granny_flat_status = status
        listing.granny_flat_reasoning = reasoning
        listing.tags = sorted({*listing.tags, f"granny-flat:{status.split(' ')[0]}"})
        counts[status] = counts.get(status, 0) + 1

    log.info("[%s] granny-flat assessment: %s", profile, counts)
    return listings


def main() -> int:
    cfg = load_config()
    for profile in cfg["profiles"]:
        write_stage(profile, "zoned", process_profile(profile))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as exc:
        log.error("%s", exc)
        sys.exit(1)
