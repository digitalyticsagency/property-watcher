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


def _article(word: str) -> str:
    return "an" if word[:1].lower() in "aeiou" else "a"


def zone_phrase(listing: Listing, zone: str, authoritative: bool) -> str:
    """'zoned R3 (Medium Density Residential)' — with the human label when we have it."""
    label = ((listing.site or {}).get("zoning") or {}).get("label", "")
    named = f"{zone} ({label})" if label else zone
    where = (
        "The council planning register says this land is"
        if authoritative
        else "The listing text mentions the land is"
    )
    return f"{where} zoned {named}."


def assess(listing: Listing) -> tuple[str, str, str]:
    """Return (status, explanation, next_step) — all in plain English.

    Written for someone buying a house, not someone reading a planning
    instrument. Every sentence answers one of: what did we find, what does it
    mean for you, what should you do about it. Section and SEPP numbers are left
    out of the explanation; they belong in the code and its comments, not in the
    thing a buyer reads on a Saturday morning.
    """
    text = " ".join(
        filter(None, [listing.title, listing.snippet, listing.address, listing.zoning_raw])
    )
    zone, authoritative = detect_zone(listing)
    land = listing.land_size_m2
    said: list[str] = []

    existing = _matches(EXISTING_PATTERNS, text)
    potential = _matches(POTENTIAL_PATTERNS, text)

    # A "potential" phrase containing the words "granny flat" also trips the
    # existing-patterns regex, so potential wins when both match.
    if existing and not potential:
        if not listing.verified:
            return (
                "unclear — check with council",
                "The search result mentions a granny flat, but we could not open "
                "the actual listing page to confirm it. Treat this as a maybe "
                "until you have seen the listing yourself.",
                "Open the listing and check whether the granny flat is real.",
            )
        return (
            "confirmed",
            "The listing says there is already a granny flat here. That is the "
            "thing you are looking for — but a granny flat that was built "
            "without council approval is a cost, not a bonus, because you can be "
            "made to fix or remove it.",
            "Ask the agent for the granny flat's approval paperwork before you "
            "make an offer.",
        )

    if potential:
        said.append(
            "The agent says there is 'potential' for a granny flat. That is "
            "marketing, not a decision from council."
        )

    if zone and zone in REASSIGNED_E_ZONES:
        return (
            "unclear — check with council",
            " ".join(said + [
                zone_phrase(listing, zone, authoritative),
                f"That is {_article(REASSIGNED_E_ZONES[zone])} "
                f"{REASSIGNED_E_ZONES[zone].lower()} zone, not a residential one, "
                "so you cannot expect to put a granny flat on it.",
            ]),
            "Skip this one unless you have checked with council directly.",
        )

    if zone:
        said.append(zone_phrase(listing, zone, authoritative))

        if zone in SECONDARY_DWELLING_ZONES:
            said.append("Granny flats are allowed in that zone if council approves one.")

            if land and land >= MIN_COMPLYING_LOT_M2:
                cap = max(MIN_SECONDARY_DWELLING_M2, int(land * 0.05))
                said.append(
                    f"The block is {land:,} m², comfortably above the roughly "
                    f"{MIN_COMPLYING_LOT_M2} m² councils usually want, and you "
                    f"could build up to about {cap} m²."
                )
                said.append(
                    "Council still has the final say — this lot may have its own "
                    "minimum size, and flood or bushfire rules can change the answer."
                )
                return (
                    "likely",
                    " ".join(said),
                    "Worth a look. Ask council what this specific lot allows before "
                    "you commit.",
                )

            if land:
                said.append(
                    f"The block is {land:,} m², which is under the roughly "
                    f"{MIN_COMPLYING_LOT_M2} m² councils usually want. You may "
                    "still get approval, but it is not the easy path."
                )
                return (
                    "unclear — check with council",
                    " ".join(said),
                    "Ask council whether a granny flat is possible on a block this size.",
                )

            said.append("We could not find the block size, so we cannot judge whether it is big enough.")
            return (
                "unclear — check with council",
                " ".join(said),
                "Find the block size on the listing, then check it against council's minimum.",
            )

        said.append("Granny flats are not allowed in that zone.")
        return (
            "unclear — check with council",
            " ".join(said),
            "Skip this one unless council tells you otherwise.",
        )

    said.append("We could not find out how this land is zoned.")
    if land and land >= MIN_COMPLYING_LOT_M2:
        said.append(
            f"The block is {land:,} m², which would be big enough if the zoning allows it."
        )
    return (
        "unclear — check with council",
        " ".join(said),
        "Look the address up on the NSW Planning Portal to find its zone.",
    )


def process_profile(profile: str) -> list[Listing]:
    listings = read_stage(profile, "checked")

    if profile != "house_with_granny_flat":
        # The acreage profile treats a granny flat as a nice-to-have, so it is
        # scored by the ranker rather than gated here.
        return listings

    counts: dict[str, int] = {}
    for listing in listings:
        status, reasoning, next_step = assess(listing)
        listing.granny_flat_status = status
        listing.granny_flat_reasoning = reasoning
        listing.granny_flat_next_step = next_step
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
