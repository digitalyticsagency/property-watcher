"""Path C — discover listings across the wider NSW property web via a search API.

Two stages, and the split matters:

  1. DISCOVERY uses the *official* Google Custom Search JSON API (or Bing Web
     Search) — structured JSON, sanctioned, free tier. We never scrape a Google
     or Bing results page (constraint #1).

  2. ENRICHMENT optionally fetches each discovered listing URL for real detail.
     This is allowed (constraint #3) but must degrade gracefully: robots.txt is
     honoured, every fetch is wrapped, and on any failure we keep the search
     result's title/snippet/URL and mark the listing unverified rather than
     inventing details or crashing the run.
"""

from __future__ import annotations

import html
import re
import sys
import time
import urllib.robotparser
from typing import Any
from urllib.parse import urlsplit

import requests

from common import (
    Listing,
    PipelineError,
    dedupe,
    enabled_sources,
    enrich_from_text,
    load_config,
    log,
    now_iso,
    profile_config,
    require_env,
    write_stage,
)

GOOGLE_CSE_ENDPOINT = "https://www.googleapis.com/customsearch/v1"
BING_ENDPOINT = "https://api.bing.microsoft.com/v7.0/search"

_TAG_RE = re.compile(r"<[^>]+>")
_robots_cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}


# --------------------------------------------------------------------------- queries


def build_queries(cfg: dict[str, Any], profile: str) -> list[str]:
    """One query per (region x keyword-theme), capped by config.

    Google CSE has no OR-of-sites operator that behaves well, so site scoping is
    configured on the Programmable Search Engine itself (see README) and the
    queries stay natural-language.
    """
    p = profile_config(cfg, profile)
    search_cfg = cfg.get("search_api") or {}
    regions = [r for r in p.get("target_regions", []) if str(r).strip()]
    keywords = p.get("query_keywords") or []
    budget_max = p.get("budget_max_aud")

    queries: list[str] = []
    for region in regions:
        for keyword in keywords or [""]:
            terms = [keyword, region, "NSW", "for sale"]
            queries.append(" ".join(t for t in terms if t).strip())
        if profile == "lifestyle_acreage":
            queries.append(f"acreage for sale {region} NSW land size hectares")
        else:
            queries.append(f"house with granny flat for sale {region} NSW under {budget_max}")

    # Deterministic order, deduped, then capped so we stay inside the free tier.
    seen: set[str] = set()
    unique = [q for q in queries if not (q in seen or seen.add(q))]
    cap = int(search_cfg.get("max_queries_per_profile", 8))
    if len(unique) > cap:
        log.info(
            "[%s] %d queries built, capping to %d (search_api.max_queries_per_profile)",
            profile,
            len(unique),
            cap,
        )
    return unique[:cap]


# --------------------------------------------------------------------------- discovery


def search_google_cse(query: str, cfg: dict[str, Any]) -> list[dict[str, str]]:
    search_cfg = cfg.get("search_api") or {}
    params = {
        "key": require_env("GOOGLE_SEARCH_API_KEY"),
        "cx": require_env("GOOGLE_SEARCH_ENGINE_ID"),
        "q": query,
        "num": min(int(search_cfg.get("results_per_query", 10)), 10),
    }
    resp = requests.get(GOOGLE_CSE_ENDPOINT, params=params, timeout=30)
    if resp.status_code == 429:
        raise PipelineError(
            "Google Custom Search returned 429 (daily free quota is 100 queries). "
            "Lower search_api.max_queries_per_profile or enable billing."
        )
    if resp.status_code >= 400:
        raise PipelineError(
            f"Google Custom Search failed ({resp.status_code}): {resp.text[:300]}"
        )
    items = resp.json().get("items") or []
    return [
        {
            "url": it.get("link", ""),
            "title": it.get("title", ""),
            "snippet": it.get("snippet", ""),
        }
        for it in items
        if it.get("link")
    ]


def search_bing(query: str, cfg: dict[str, Any]) -> list[dict[str, str]]:
    """Adapter kept so switching providers is a config change, not a rewrite."""
    search_cfg = cfg.get("search_api") or {}
    sites = search_cfg.get("sites") or []
    scoped = query + " " + " OR ".join(f"site:{s}" for s in sites) if sites else query
    resp = requests.get(
        BING_ENDPOINT,
        params={"q": scoped, "count": int(search_cfg.get("results_per_query", 10)), "mkt": "en-AU"},
        headers={"Ocp-Apim-Subscription-Key": require_env("BING_SEARCH_API_KEY")},
        timeout=30,
    )
    if resp.status_code >= 400:
        raise PipelineError(
            f"Bing Web Search failed ({resp.status_code}): {resp.text[:300]}"
        )
    pages = (resp.json().get("webPages") or {}).get("value") or []
    return [
        {"url": p.get("url", ""), "title": p.get("name", ""), "snippet": p.get("snippet", "")}
        for p in pages
        if p.get("url")
    ]


def run_search(query: str, cfg: dict[str, Any]) -> list[dict[str, str]]:
    provider = (cfg.get("search_api") or {}).get("provider", "google_cse")
    if provider == "google_cse":
        return search_google_cse(query, cfg)
    if provider == "bing":
        return search_bing(query, cfg)
    raise PipelineError(f"unknown search_api.provider {provider!r}")


def looks_like_listing(url: str, cfg: dict[str, Any]) -> bool:
    """Filter out agency landing pages, suburb profiles, and blog posts."""
    lowered = url.lower()
    sites = (cfg.get("search_api") or {}).get("sites") or []
    if sites and not any(s in lowered for s in sites):
        return False
    noise = (
        "/agency/",
        "/agent/",
        "/blog/",
        "/news/",
        "/suburb-profile",
        "/neighbourhoods/",
        "/find-agent",
        "/rent/",
        "/sold/",
    )
    if any(n in lowered for n in noise):
        return False
    return True


# --------------------------------------------------------------------------- enrichment


def robots_allows(url: str, user_agent: str) -> bool:
    """Honour robots.txt. On any doubt we skip the fetch rather than push on."""
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    if origin not in _robots_cache:
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(f"{origin}/robots.txt")
        try:
            parser.read()
            _robots_cache[origin] = parser
        except Exception as exc:
            log.info("robots.txt unreadable for %s (%s) — skipping fetch", origin, exc)
            _robots_cache[origin] = None
    parser = _robots_cache[origin]
    if parser is None:
        return False
    try:
        return parser.can_fetch(user_agent, url)
    except Exception:
        return False


def visible_text(markup: str) -> str:
    text = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", markup, flags=re.S | re.I)
    text = _TAG_RE.sub(" ", text)
    return html.unescape(re.sub(r"\s+", " ", text)).strip()


_ZONING_RE = re.compile(
    r"\b(?:zon(?:e|ing|ed)\s*[:\-]?\s*)?(R[1-5]|RU[1-6]|E[1-4]|C[1-4]|B[1-8]|IN[1-3])\b"
)


def extract_zoning(text: str) -> str:
    """Only accept a zone code that appears near the word 'zon...' — bare 'R2'
    matches far too many bedroom counts and lot references to trust alone."""
    for m in re.finditer(r"zon\w*", text, re.IGNORECASE):
        window = text[m.start() : m.start() + 80]
        code = _ZONING_RE.search(window)
        if code:
            return code.group(1).upper()
    return ""


def enrich_listing(listing: Listing, cfg: dict[str, Any]) -> None:
    """Try to fetch the real page. Every failure path leaves the listing usable.

    On success we set verified=True; on any failure we leave verified=False and
    record why, so build_dashboard can badge it (constraint #3).
    """
    fetch_cfg = (cfg.get("search_api") or {}).get("fetch") or {}
    if not fetch_cfg.get("enabled", True):
        listing.unverified_reason = "page fetch disabled in config"
        return

    user_agent = fetch_cfg.get("user_agent", "nsw-property-watcher/1.0")
    if not robots_allows(listing.url, user_agent):
        listing.unverified_reason = "blocked by robots.txt"
        log.info("robots.txt disallows %s — keeping snippet only", listing.url)
        return

    try:
        resp = requests.get(
            listing.url,
            headers={"User-Agent": user_agent, "Accept-Language": "en-AU,en;q=0.9"},
            timeout=float(fetch_cfg.get("timeout_seconds", 15)),
        )
    except requests.RequestException as exc:
        listing.unverified_reason = f"fetch failed: {type(exc).__name__}"
        log.info("fetch failed for %s: %s", listing.url, exc)
        return

    if resp.status_code >= 400:
        listing.unverified_reason = f"fetch returned HTTP {resp.status_code}"
        log.info("fetch %s returned %s", listing.url, resp.status_code)
        return

    try:
        text = visible_text(resp.text)[:20000]
    except Exception as exc:  # pragma: no cover - defensive
        listing.unverified_reason = f"could not parse page: {type(exc).__name__}"
        return

    if len(text) < 400:
        # Almost certainly a bot-interstitial or a JS-only shell — not real detail.
        listing.unverified_reason = "page returned no readable content"
        return

    listing.raw["page_chars"] = len(text)
    listing.zoning_raw = listing.zoning_raw or extract_zoning(text)
    enrich_from_text(listing, text)
    if not listing.snippet or len(listing.snippet) < 200:
        listing.snippet = text[:600]
    listing.verified = True
    listing.unverified_reason = ""


# --------------------------------------------------------------------------- driver


def fetch_profile(cfg: dict[str, Any], profile: str) -> list[Listing]:
    queries = build_queries(cfg, profile)
    fetch_cfg = (cfg.get("search_api") or {}).get("fetch") or {}
    delay = float(fetch_cfg.get("delay_seconds", 2.0))

    listings: list[Listing] = []
    for query in queries:
        log.info("[%s] search: %s", profile, query)
        for result in run_search(query, cfg):
            if not looks_like_listing(result["url"], cfg):
                continue
            listing = Listing(
                url=result["url"],
                profile=profile,
                source="search_api",
                title=result["title"],
                snippet=result["snippet"],
                verified=False,
                unverified_reason="not yet fetched",
                first_seen=now_iso(),
                last_seen=now_iso(),
                raw={"query": query},
            )
            enrich_from_text(listing, f"{result['title']} {result['snippet']}")
            listings.append(listing)

    listings = dedupe(listings)
    log.info("[%s] %d unique candidates from %d queries", profile, len(listings), len(queries))

    for listing in listings:
        enrich_listing(listing, cfg)
        if delay:
            time.sleep(delay)

    verified = sum(1 for l in listings if l.verified)
    log.info(
        "[%s] enrichment: %d verified, %d unverified (snippet only)",
        profile,
        verified,
        len(listings) - verified,
    )
    return listings


def main() -> int:
    cfg = load_config()
    if "path_c_search_api" not in enabled_sources(cfg):
        log.info("path_c_search_api not in data_sources — writing empty stages")
        for profile in cfg["profiles"]:
            write_stage(profile, "search_api", [])
        return 0

    for profile in cfg["profiles"]:
        write_stage(profile, "search_api", fetch_profile(cfg, profile))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as exc:
        log.error("%s", exc)
        sys.exit(1)
