"""Shared plumbing: config loading/validation, the Listing record, and JSON I/O.

Every pipeline stage imports from here so that a malformed config or an unwritable
data directory fails once, loudly, at the top of the run (constraint #10).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.yaml"
DATA_DIR = REPO_ROOT / "data"
WORK_DIR = DATA_DIR / "_work"
CACHE_DIR = DATA_DIR / "cache"
DOCS_DIR = REPO_ROOT / "docs"

PROFILES = ("lifestyle_acreage", "house_with_granny_flat")


class PipelineError(RuntimeError):
    """Raised for any condition that must abort the run with a non-zero exit."""


def setup_logging() -> logging.Logger:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        stream=sys.stderr,
    )
    return logging.getLogger("property-watcher")


log = setup_logging()


# --------------------------------------------------------------------------- config


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    if not path.exists():
        raise PipelineError(f"config.yaml not found at {path}")
    with path.open(encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    if not isinstance(cfg, dict):
        raise PipelineError("config.yaml did not parse into a mapping")
    validate_config(cfg)
    return cfg


def validate_config(cfg: dict[str, Any]) -> None:
    """Reject the shipped placeholders so a half-configured run can never publish.

    The brief says not to guess the regions/budget/land size, so the repo ships
    with sentinels and this check turns "forgot to fill it in" into a hard failure.
    """
    problems: list[str] = []

    profiles = cfg.get("profiles") or {}
    for name in PROFILES:
        p = profiles.get(name)
        if not p:
            problems.append(f"profiles.{name} is missing")
            continue

        regions = [r for r in (p.get("target_regions") or []) if str(r).strip()]
        if not regions:
            problems.append(f"profiles.{name}.target_regions is empty")
        if any("FILL_ME" in str(r) for r in regions):
            problems.append(
                f"profiles.{name}.target_regions still contains the FILL_ME placeholder"
            )

        budget_max = p.get("budget_max_aud") or 0
        if budget_max <= 0:
            problems.append(f"profiles.{name}.budget_max_aud must be greater than 0")
        budget_min = p.get("budget_min_aud") or 0
        if budget_min and budget_min > budget_max:
            problems.append(f"profiles.{name}.budget_min_aud exceeds budget_max_aud")

        if (p.get("min_land_size_m2") or 0) <= 0:
            problems.append(f"profiles.{name}.min_land_size_m2 must be greater than 0")
        if (p.get("max_drive_hours_from_sydney_cbd") or 0) <= 0:
            problems.append(
                f"profiles.{name}.max_drive_hours_from_sydney_cbd must be greater than 0"
            )

    ua = (cfg.get("geocoding") or {}).get("nominatim_user_agent", "")
    if "FILL_ME" in ua:
        problems.append(
            "geocoding.nominatim_user_agent still contains FILL_ME — "
            "Nominatim requires a real contact address in the User-Agent"
        )

    sources = cfg.get("data_sources") or []
    if not sources:
        problems.append("data_sources is empty — nothing would be fetched")

    if problems:
        raise PipelineError(
            "config.yaml is not ready:\n  - " + "\n  - ".join(problems)
        )


def profile_config(cfg: dict[str, Any], profile: str) -> dict[str, Any]:
    try:
        return cfg["profiles"][profile]
    except KeyError as exc:
        raise PipelineError(f"unknown profile {profile!r}") from exc


def enabled_sources(cfg: dict[str, Any]) -> set[str]:
    return set(cfg.get("data_sources") or [])


# --------------------------------------------------------------------------- listing


@dataclass
class Listing:
    """One candidate property, carried through the whole pipeline.

    `verified` is the load-bearing flag from constraint #3: False means we only
    ever saw a search-result title/snippet, never the listing page itself, so the
    dashboard must badge it as unconfirmed.
    """

    url: str
    profile: str
    source: str  # search_api | domain_api
    title: str = ""
    snippet: str = ""
    address: str = ""
    suburb: str = ""
    price_text: str = ""
    price_aud: int | None = None
    land_size_m2: int | None = None
    bedrooms: int | None = None
    bathrooms: int | None = None
    zoning_raw: str = ""
    verified: bool = False
    unverified_reason: str = ""
    first_seen: str = ""
    last_seen: str = ""
    # Filled by later stages.
    lat: float | None = None
    lon: float | None = None
    drive_hours: float | None = None
    distance_source: str = ""
    granny_flat_status: str = ""
    granny_flat_reasoning: str = ""
    granny_flat_next_step: str = ""
    score: float | None = None
    score_reason: str = ""
    # Plain-English analysis written for a home buyer, not a planner.
    verdict: str = ""
    good_points: list[str] = field(default_factory=list)
    watch_outs: list[str] = field(default_factory=list)
    next_action: str = ""
    tags: list[str] = field(default_factory=list)
    rejected_reason: str = ""
    # Live NSW government site checks (zoning, bushfire, flood, amenities).
    site: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return url_key(self.url)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["id"] = self.id
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Listing":
        known = {f for f in cls.__dataclass_fields__}  # noqa: SLF001
        return cls(**{k: v for k, v in d.items() if k in known})


def url_key(url: str) -> str:
    """Canonical dedupe key: scheme/host/path only, query and fragment dropped.

    Portals append tracking params to the same listing across emails and search
    results, so comparing raw URLs would let duplicates through (constraint #9).
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    host = (parts.netloc or "").lower().removeprefix("www.")
    path = (parts.path or "/").rstrip("/") or "/"
    canonical = urlunsplit(("https", host, path, "", ""))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- parsing helpers

_PRICE_RE = re.compile(
    r"\$\s*([\d,]+(?:\.\d+)?)\s*(k|m|million)?", re.IGNORECASE
)
_LAND_RE = re.compile(
    r"([\d,]+(?:\.\d+)?)\s*(m2|m²|sqm|sq\.?\s?m|ha|hectare|acre)s?", re.IGNORECASE
)
_BED_RE = re.compile(r"(\d+)\s*(?:bed|bedroom|br)\b", re.IGNORECASE)
_BATH_RE = re.compile(r"(\d+)\s*(?:bath|bathroom)\b", re.IGNORECASE)


def parse_price_aud(text: str) -> int | None:
    """Best-effort price extraction. Returns None for 'contact agent' / auction."""
    if not text:
        return None
    best: int | None = None
    for raw, unit in _PRICE_RE.findall(text):
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            continue
        unit = (unit or "").lower()
        if unit == "k":
            value *= 1_000
        elif unit in ("m", "million"):
            value *= 1_000_000
        if value < 10_000:  # "$500 per week" and similar noise
            continue
        # Ranges like "$800,000 - $850,000": take the low end as the asking price.
        if best is None or value < best:
            best = int(value)
    return best


def parse_land_size_m2(text: str) -> int | None:
    if not text:
        return None
    best: int | None = None
    for raw, unit in _LAND_RE.findall(text):
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            continue
        unit = unit.lower().replace(".", "").replace(" ", "")
        if unit in ("ha", "hectare"):
            value *= 10_000
        elif unit == "acre":
            value *= 4046.86
        if value <= 0:
            continue
        best = max(best or 0, int(value))
    return best


def parse_int(pattern: re.Pattern[str], text: str) -> int | None:
    if not text:
        return None
    m = pattern.search(text)
    return int(m.group(1)) if m else None


def parse_bedrooms(text: str) -> int | None:
    return parse_int(_BED_RE, text)


def parse_bathrooms(text: str) -> int | None:
    return parse_int(_BATH_RE, text)


def enrich_from_text(listing: Listing, text: str) -> None:
    """Fill any still-empty numeric fields from free text (title + snippet + body)."""
    if listing.price_aud is None:
        listing.price_aud = parse_price_aud(text)
    if listing.land_size_m2 is None:
        listing.land_size_m2 = parse_land_size_m2(text)
    if listing.bedrooms is None:
        listing.bedrooms = parse_bedrooms(text)
    if listing.bathrooms is None:
        listing.bathrooms = parse_bathrooms(text)


# --------------------------------------------------------------------------- io


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        raise PipelineError(f"{path} is not valid JSON: {exc}") from exc


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False, sort_keys=True)
        fh.write("\n")
    tmp.replace(path)


def stage_path(profile: str, stage: str) -> Path:
    return WORK_DIR / profile / f"{stage}.json"


def write_stage(profile: str, stage: str, listings: list[Listing]) -> None:
    write_json(stage_path(profile, stage), [l.to_dict() for l in listings])
    log.info("[%s] wrote %d listings to stage %r", profile, len(listings), stage)


def read_stage(profile: str, stage: str) -> list[Listing]:
    raw = read_json(stage_path(profile, stage), None)
    if raw is None:
        raise PipelineError(
            f"stage {stage!r} for profile {profile!r} has not been produced yet "
            f"(expected {stage_path(profile, stage)})"
        )
    return [Listing.from_dict(d) for d in raw]


def seen_path(profile: str) -> Path:
    return DATA_DIR / profile / "seen_listings.json"


def archive_path(profile: str) -> Path:
    return DATA_DIR / profile / "archive" / "all_listings.json"


STATUS_PATH = WORK_DIR / "source_status.json"


def record_source(source: str, ok: bool, detail: str = "") -> None:
    """Note whether a data source succeeded this run, for the dashboard banner."""
    status = read_json(STATUS_PATH, {})
    status[source] = {"ok": ok, "detail": detail[:400], "at": now_iso()}
    write_json(STATUS_PATH, status)


def run_source(source: str, stage: str, profiles: list[str], fetch_fn) -> int:
    """Run one fetcher, surviving a source-level outage.

    Constraint #10 says never publish silently on a broken run — but that is
    about *silence*, not about coupling. One provider returning 403 shouldn't
    blockade a second, healthy source: the run continues, the failure is
    recorded, and build_dashboard shows it as a banner. What stays fatal is
    every source failing, which the workflow checks before publishing.
    """
    try:
        for profile in profiles:
            write_stage(profile, stage, fetch_fn(profile))
        record_source(source, True)
        return 0
    except PipelineError as exc:
        log.error("SOURCE FAILED (%s): %s", source, exc)
        log.error(
            "Continuing so other sources can still publish — this run's dashboard "
            "will show %s as unavailable.", source,
        )
        for profile in profiles:
            write_stage(profile, stage, [])
        record_source(source, False, str(exc))
        return 0


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise PipelineError(
            f"required secret {name} is not set — add it under "
            f"Settings → Secrets and variables → Actions"
        )
    return value


def dedupe(listings: list[Listing]) -> list[Listing]:
    """Collapse duplicates by canonical URL, preferring the verified copy.

    The same property routinely arrives from both a saved-search email and the
    search API; we keep whichever record carries more confirmed detail.
    """
    best: dict[str, Listing] = {}
    for item in listings:
        key = item.id
        current = best.get(key)
        if current is None:
            best[key] = item
            continue
        if item.verified and not current.verified:
            best[key] = item
        elif item.verified == current.verified and _detail_score(item) > _detail_score(current):
            best[key] = item
    return list(best.values())


def _detail_score(listing: Listing) -> int:
    fields = (
        listing.price_aud,
        listing.land_size_m2,
        listing.bedrooms,
        listing.address or None,
        listing.zoning_raw or None,
    )
    return sum(1 for f in fields if f)
