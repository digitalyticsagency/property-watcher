"""Hard filters, then per-profile AI scoring and commentary.

Order matters: deterministic hard filters run FIRST (budget, land size), so the
model only ever spends tokens on listings that already clear the numeric bar.

The Anthropic API is used strictly for judgement and prose — scoring against the
profile's must_haves/nice_to_haves, and writing the weekly summary and tab
intros (constraint #4). It is never asked to supply facts about a listing that
we did not actually retrieve, and unverified (snippet-only) listings are handed
to it clearly labelled so it can mark them lower-confidence.

The two profiles are scored in completely separate calls with separate prompts
and never share a score scale (constraint #7).
"""

from __future__ import annotations

import json
import sys
from typing import Any

import anthropic

from common import (
    WORK_DIR,
    Listing,
    PipelineError,
    archive_path,
    load_config,
    log,
    now_iso,
    profile_config,
    read_json,
    read_stage,
    require_env,
    seen_path,
    write_json,
    write_stage,
)

SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "The listing id given in the input"},
                    "score": {
                        "type": "integer",
                        "description": "Fit against this profile's criteria, 1 (poor) to 10 (excellent)",
                    },
                    "verdict": {
                        "type": "string",
                        "description": (
                            "ONE short plain-English sentence a home buyer reads first. "
                            "Say what this place is and whether it is worth their time. "
                            "No jargon, no zone codes, no hedging."
                        ),
                    },
                    "good_points": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "1-3 short plain-English phrases naming what genuinely suits "
                            "this buyer. Each cites a real detail. Empty if nothing does."
                        ),
                    },
                    "watch_outs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "1-3 short plain-English phrases naming what could cost money "
                            "or disappoint. Include unknowns. Empty only if genuinely none."
                        ),
                    },
                    "next_action": {
                        "type": "string",
                        "description": (
                            "The single most useful thing to do next, as an instruction "
                            "starting with a verb. Concrete: who to ask, what to check. "
                            "If it is not worth pursuing, say so plainly."
                        ),
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Short lowercase-hyphenated tags, e.g. flat-land, needs-work",
                    },
                    "confidence": {
                        "type": "string",
                        "enum": ["high", "low"],
                        "description": "low whenever the listing is marked UNVERIFIED",
                    },
                },
                "required": ["id", "score", "verdict", "good_points", "watch_outs",
                             "next_action", "tags", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["scores"],
    "additionalProperties": False,
}

COMMENTARY_SCHEMA = {
    "type": "object",
    "properties": {
        "weekly_summary": {
            "type": "string",
            "description": "2-4 sentences on what changed this week for this profile",
        },
        "top_pick_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Up to 3 listing ids worth looking at first",
        },
        "tab_intros": {
            "type": "object",
            "properties": {
                "overview": {"type": "string"},
                "all_matches": {"type": "string"},
                "by_score": {"type": "string"},
                "tags": {"type": "string"},
                "archive": {"type": "string"},
            },
            "required": ["overview", "all_matches", "by_score", "tags", "archive"],
            "additionalProperties": False,
        },
    },
    "required": ["weekly_summary", "top_pick_ids", "tab_intros"],
    "additionalProperties": False,
}


REPORT_PATH = WORK_DIR / "commentary.json"


def client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=require_env("ANTHROPIC_API_KEY"))


# --------------------------------------------------------------------------- hard filters


def hard_filter(listings: list[Listing], cfg: dict[str, Any], profile: str) -> list[Listing]:
    """Reject on the numbers we actually know. Unknowns are kept, not rejected.

    A missing price is common ("contact agent", auction) and is not evidence the
    property is out of budget — dropping those would hide good listings.
    """
    p = profile_config(cfg, profile)
    budget_min = p.get("budget_min_aud") or 0
    budget_max = p["budget_max_aud"]
    min_land = p["min_land_size_m2"]

    kept: list[Listing] = []
    rejected = 0
    for listing in listings:
        if listing.price_aud is not None:
            if listing.price_aud > budget_max * 1.05:  # 5% grace: agents list "offers over"
                rejected += 1
                continue
            if budget_min and listing.price_aud < budget_min * 0.95:
                rejected += 1
                continue
        else:
            listing.tags = sorted({*listing.tags, "price-unknown"})

        if listing.land_size_m2 is not None:
            if listing.land_size_m2 < min_land * 0.9:
                rejected += 1
                continue
        else:
            listing.tags = sorted({*listing.tags, "land-size-unknown"})

        kept.append(listing)

    log.info("[%s] hard filter: kept %d, rejected %d", profile, len(kept), rejected)
    return kept


# --------------------------------------------------------------------------- prompts


def render_listing(listing: Listing) -> dict[str, Any]:
    return {
        "id": listing.id,
        "verified": listing.verified,
        "source": listing.source,
        "title": listing.title[:200],
        "address": listing.address,
        "price_text": listing.price_text,
        "price_aud": listing.price_aud,
        "land_size_m2": listing.land_size_m2,
        "bedrooms": listing.bedrooms,
        "bathrooms": listing.bathrooms,
        "zoning": listing.zoning_raw,
        "drive_hours_from_sydney": listing.drive_hours,
        "distance_confidence": listing.distance_source,
        "granny_flat_status": listing.granny_flat_status,
        "granny_flat_reasoning": listing.granny_flat_reasoning,
        # Registry facts. These are authoritative — do not contradict them.
        "site_checks": {
            "zone": (listing.site.get("zoning") or {}).get("code"),
            "zone_meaning": (listing.site.get("zoning") or {}).get("label"),
            "council": (listing.site.get("zoning") or {}).get("lga"),
            "bushfire": (listing.site.get("bushfire") or {}).get("text"),
            "flood": (listing.site.get("flood") or {}).get("text"),
            "min_lot_size": (listing.site.get("min_lot_size") or {}).get("text"),
        } if listing.site.get("status") == "ok" else None,
        "text": (listing.snippet or "")[:1200],
        "note": (
            "UNVERIFIED — only a search-result snippet was retrieved, the listing "
            "page itself could not be read. Score on the snippet alone and set "
            "confidence to low."
            if not listing.verified
            else ""
        ),
    }


def system_prompt(cfg: dict[str, Any], profile: str) -> str:
    p = profile_config(cfg, profile)
    return f"""You are helping evaluate NSW property listings for ONE specific buyer profile.

PROFILE: {p.get('label', profile)}
Target regions: {', '.join(p.get('target_regions', []))}
Max drive time from Sydney CBD: {p['max_drive_hours_from_sydney_cbd']} hours
Minimum land size: {p['min_land_size_m2']} m2
Budget: ${p.get('budget_min_aud') or 0:,} - ${p['budget_max_aud']:,} AUD

MUST HAVES:
{chr(10).join('  - ' + m for m in p.get('must_haves', []))}

NICE TO HAVES:
{chr(10).join('  - ' + m for m in p.get('nice_to_haves', []))}

Scoring rules:
  - Score 1-10 for fit against THIS profile only. Do not compare against any other
    buyer profile or any other search.
  - Weight must-haves far more heavily than nice-to-haves.
  - Judge ONLY on the data provided. Never assert a feature that is not in the
    listing text. If something important is unknown, say it is unknown in your
    reason and let that limit the score.
  - Any listing marked UNVERIFIED must get confidence "low", and its score should
    reflect that you are reading a search snippet rather than the real listing.
  - For the granny-flat profile, treat granny_flat_status as authoritative — it
    comes from a zoning check. Do not upgrade "unclear" to a promise.
  - Tags are short, lowercase, hyphenated, and factual (flat-land, steep-block,
    bushfire-risk, needs-renovation, town-water, tenanted, subdividable).

HOW TO WRITE (this matters as much as the scoring):
  - Write for someone buying a home, not someone who works in property. Assume
    no knowledge of planning law, zone codes, or industry shorthand.
  - Short sentences. Everyday words. "The block is big enough" beats "the parcel
    satisfies the minimum lot size requirement".
  - Never write a zone code on its own. If a zone matters, say what it means.
  - Say the useful thing, not the safe thing. "Probably too small for what you
    want" is more useful than "may present some constraints".
  - Do not repeat the same fact in the verdict, the good points and the watch
    outs. Say each thing once, in the place it belongs.
  - next_action is an instruction, starting with a verb, that the buyer could do
    tomorrow: ring someone, check something, book something, or skip it.
  - Where the site checks say something is unknown, treat it as unknown. Never
    fill a gap with a guess."""


def score_batch(
    api: anthropic.Anthropic, cfg: dict[str, Any], profile: str, batch: list[Listing]
) -> dict[str, dict[str, Any]]:
    anth = cfg.get("anthropic") or {}
    payload = json.dumps([render_listing(l) for l in batch], indent=2, ensure_ascii=False)

    response = api.messages.create(
        model=anth.get("model", "claude-opus-5"),
        max_tokens=int(anth.get("max_tokens", 16000)),
        thinking={"type": "adaptive"},
        output_config={
            "effort": anth.get("effort", "medium"),
            "format": {"type": "json_schema", "schema": SCORE_SCHEMA},
        },
        system=system_prompt(cfg, profile),
        messages=[
            {
                "role": "user",
                "content": (
                    f"Score each of these {len(batch)} listings. Return one entry per "
                    f"listing, using the exact id given.\n\n{payload}"
                ),
            }
        ],
    )

    if response.stop_reason == "refusal":
        raise PipelineError(
            f"[{profile}] scoring request was refused by safety classifiers "
            f"({getattr(response.stop_details, 'category', None)})"
        )

    text = next((b.text for b in response.content if b.type == "text"), "")
    if not text:
        raise PipelineError(f"[{profile}] scoring returned no text content")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PipelineError(f"[{profile}] scoring returned non-JSON output: {exc}") from exc

    return {entry["id"]: entry for entry in parsed.get("scores", [])}


def score_profile(
    api: anthropic.Anthropic, cfg: dict[str, Any], profile: str, listings: list[Listing]
) -> list[Listing]:
    if not listings:
        return []
    batch_size = int((cfg.get("anthropic") or {}).get("batch_size", 12))
    results: dict[str, dict[str, Any]] = {}
    for i in range(0, len(listings), batch_size):
        batch = listings[i : i + batch_size]
        log.info("[%s] scoring batch %d (%d listings)", profile, i // batch_size + 1, len(batch))
        results.update(score_batch(api, cfg, profile, batch))

    missing = 0
    for listing in listings:
        entry = results.get(listing.id)
        if not entry:
            missing += 1
            listing.score = None
            listing.verdict = "Not scored — the model did not return an entry for this listing."
            listing.score_reason = listing.verdict
            continue
        listing.score = float(entry["score"])
        listing.verdict = entry.get("verdict", "")
        listing.good_points = entry.get("good_points") or []
        listing.watch_outs = entry.get("watch_outs") or []
        listing.next_action = entry.get("next_action", "")
        # score_reason stays populated for the archive and any older consumer.
        listing.score_reason = entry.get("verdict", "")
        listing.tags = sorted({*listing.tags, *(entry.get("tags") or [])})
        if entry.get("confidence") == "low":
            listing.tags = sorted({*listing.tags, "low-confidence"})

    if missing:
        log.warning("[%s] %d listings came back unscored", profile, missing)
    return listings


def write_commentary(
    api: anthropic.Anthropic,
    cfg: dict[str, Any],
    profile: str,
    listings: list[Listing],
    new_ids: set[str],
) -> dict[str, Any]:
    anth = cfg.get("anthropic") or {}
    ranked = sorted(listings, key=lambda l: (l.score or 0), reverse=True)[:20]
    digest = [
        {
            "id": l.id,
            "title": l.title[:120],
            "score": l.score,
            "reason": l.score_reason,
            "is_new_this_week": l.id in new_ids,
            "verified": l.verified,
            "source": l.source,
        }
        for l in ranked
    ]

    response = api.messages.create(
        model=anth.get("model", "claude-opus-5"),
        max_tokens=int(anth.get("max_tokens", 16000)),
        thinking={"type": "adaptive"},
        output_config={
            "effort": anth.get("effort", "medium"),
            "format": {"type": "json_schema", "schema": COMMENTARY_SCHEMA},
        },
        system=system_prompt(cfg, profile),
        messages=[
            {
                "role": "user",
                "content": (
                    f"This week's run for this profile found {len(listings)} current "
                    f"matches, {len(new_ids)} of them new since the last run.\n\n"
                    f"Top listings by score:\n{json.dumps(digest, indent=2)}\n\n"
                    "Write: (1) a short 'what changed this week' summary, (2) up to 3 "
                    "top pick ids, (3) a one-or-two sentence intro for each dashboard "
                    "tab. Ground every claim in the data above; if the week was quiet, "
                    "say so plainly rather than inflating it."
                ),
            }
        ],
    )

    if response.stop_reason == "refusal":
        raise PipelineError(f"[{profile}] commentary request was refused")

    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise PipelineError(f"[{profile}] commentary returned non-JSON output: {exc}") from exc


# --------------------------------------------------------------------------- seen / archive


def update_seen_and_archive(profile: str, listings: list[Listing]) -> set[str]:
    """Dedupe against previous runs and keep an all-time archive (constraint #9)."""
    seen: dict[str, Any] = read_json(seen_path(profile), {})
    archive: dict[str, Any] = read_json(archive_path(profile), {})

    new_ids: set[str] = set()
    for listing in listings:
        key = listing.id
        if key in seen:
            listing.first_seen = seen[key].get("first_seen", listing.first_seen)
        else:
            new_ids.add(key)
        listing.last_seen = now_iso()
        seen[key] = {
            "url": listing.url,
            "source": listing.source,
            "first_seen": listing.first_seen,
            "last_seen": listing.last_seen,
        }
        archive[key] = listing.to_dict()

    write_json(seen_path(profile), seen)
    write_json(archive_path(profile), archive)
    log.info(
        "[%s] %d new since last run; archive now holds %d listings",
        profile, len(new_ids), len(archive),
    )
    return new_ids


def main() -> int:
    cfg = load_config()
    api = client()
    summaries: dict[str, Any] = {}

    for profile in cfg["profiles"]:
        listings = read_stage(profile, "zoned")
        listings = hard_filter(listings, cfg, profile)
        new_ids = update_seen_and_archive(profile, listings)
        listings = score_profile(api, cfg, profile, listings)
        listings.sort(key=lambda l: (l.score or 0), reverse=True)

        commentary = (
            write_commentary(api, cfg, profile, listings, new_ids)
            if listings
            else {
                "weekly_summary": "No listings matched this profile's filters in this run.",
                "top_pick_ids": [],
                "tab_intros": {
                    k: "Nothing to show yet for this profile."
                    for k in ("overview", "all_matches", "by_score", "tags", "archive")
                },
            }
        )
        commentary["new_ids"] = sorted(new_ids)
        summaries[profile] = commentary

        write_stage(profile, "ranked", listings)

    write_json(REPORT_PATH, {"generated_at": now_iso(), "profiles": summaries})
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as exc:
        log.error("%s", exc)
        sys.exit(1)
