"""Path A — read realestate.com.au / domain.com.au saved-search alert emails.

This is the sanctioned route to the two big portals: we read the alert emails
they send us, rather than scraping their sites on a schedule (constraint #2).

Listings from here are marked verified=True: the portal itself asserted the
price/address in the email, so it isn't a snippet guess.
"""

from __future__ import annotations

import base64
import html
import os
import quopri
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlsplit

from common import (
    Listing,
    PipelineError,
    dedupe,
    enrich_from_text,
    enabled_sources,
    load_config,
    log,
    now_iso,
    profile_config,
    require_env,
    run_source,
    write_stage,
)

PORTAL_HOSTS = (
    "realestate.com.au",
    "domain.com.au",
    "allhomes.com.au",
)

# Alert emails wrap every listing link in a click-tracker; the real URL is a
# query parameter on the redirect.
TRACKER_PARAMS = ("url", "u", "target", "destination", "redirect", "link")

_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


def gmail_service(cfg: dict[str, Any]):
    """Build a Gmail client from the stored refresh token. No interactive auth."""
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise PipelineError(
            "google-api-python-client / google-auth are not installed"
        ) from exc

    creds = Credentials(
        token=None,
        refresh_token=require_env("GMAIL_REFRESH_TOKEN"),
        client_id=require_env("GMAIL_CLIENT_ID"),
        client_secret=require_env("GMAIL_CLIENT_SECRET"),
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
    )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _decode_part(data: str) -> str:
    raw = base64.urlsafe_b64decode(data.encode("utf-8") + b"==")
    try:
        return raw.decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover - defensive
        return ""


def extract_bodies(payload: dict[str, Any]) -> list[str]:
    """Walk the MIME tree and collect every text/plain and text/html part."""
    bodies: list[str] = []
    mime = payload.get("mimeType", "")
    body = payload.get("body") or {}
    if body.get("data") and mime.startswith("text/"):
        bodies.append(_decode_part(body["data"]))
    for part in payload.get("parts") or []:
        bodies.extend(extract_bodies(part))
    return bodies


def unwrap_tracking_url(url: str) -> str:
    """Follow one level of click-tracking redirect by reading the query string.

    We deliberately resolve this offline rather than issuing a request — no need
    to touch the portal, and it keeps the fetch count at zero for Path A.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    qs = parse_qs(parts.query)
    for key in TRACKER_PARAMS:
        for candidate in qs.get(key, []):
            if candidate.startswith("http") and any(
                h in candidate for h in PORTAL_HOSTS
            ):
                return candidate
    return url


def is_listing_url(url: str) -> bool:
    if not url.startswith("http"):
        return False
    lowered = url.lower()
    if not any(h in lowered for h in PORTAL_HOSTS):
        return False
    # Portal listing pages have a property slug; everything else in the email is
    # navigation, unsubscribe links, or agent profiles.
    return any(
        marker in lowered
        for marker in ("/property-", "/property/", "/listing", "-p", "/sale/")
    )


def html_to_text(markup: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", markup, flags=re.S | re.I)
    text = _TAG_RE.sub(" ", text)
    return html.unescape(re.sub(r"\s+", " ", text)).strip()


def context_for(url: str, text_bodies: list[str], html_bodies: list[str]) -> str:
    """Grab the text near a listing link — that's where price/beds/land live."""
    slug = urlsplit(url).path.rstrip("/").split("/")[-1]
    chunks: list[str] = []
    for markup in html_bodies:
        idx = markup.find(slug)
        if idx != -1:
            chunks.append(html_to_text(markup[max(0, idx - 1200) : idx + 1200]))
    for plain in text_bodies:
        idx = plain.find(slug)
        if idx != -1:
            chunks.append(plain[max(0, idx - 600) : idx + 600])
    if not chunks:
        # Fall back to the whole email; imprecise but better than nothing.
        chunks = [html_to_text(m) for m in html_bodies] + text_bodies
    return " ".join(chunks)[:4000]


def listings_from_message(
    message: dict[str, Any], profile: str
) -> list[Listing]:
    payload = message.get("payload") or {}
    bodies = extract_bodies(payload)
    html_bodies = [b for b in bodies if "<" in b and ">" in b]
    text_bodies = [b for b in bodies if b not in html_bodies]
    # Some senders quoted-printable-encode the HTML part.
    html_bodies = [_maybe_qp(b) for b in html_bodies]

    urls: list[str] = []
    for markup in html_bodies:
        urls.extend(_HREF_RE.findall(markup))
    for plain in text_bodies:
        urls.extend(re.findall(r"https?://\S+", plain))

    seen: set[str] = set()
    out: list[Listing] = []
    for raw_url in urls:
        url = unwrap_tracking_url(html.unescape(raw_url)).split("#")[0]
        if not is_listing_url(url) or url in seen:
            continue
        seen.add(url)
        context = context_for(url, text_bodies, html_bodies)
        listing = Listing(
            url=url,
            profile=profile,
            source="email_alert",
            title=_title_from_context(context) or url,
            snippet=context[:600],
            verified=True,
            first_seen=now_iso(),
            last_seen=now_iso(),
            raw={"gmail_message_id": message.get("id", "")},
        )
        listing.address = _address_from_context(context)
        listing.price_text = _price_text_from_context(context)
        enrich_from_text(listing, context)
        out.append(listing)
    return out


def _maybe_qp(body: str) -> str:
    if "=3D" not in body:
        return body
    try:
        return quopri.decodestring(body.encode("utf-8")).decode(
            "utf-8", errors="replace"
        )
    except Exception:
        return body


_ADDRESS_RE = re.compile(
    r"\b\d+[A-Za-z]?[/\-]?\d*\s+[A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-]+){0,3}\s+"
    r"(?:St|Street|Rd|Road|Ave|Avenue|Dr|Drive|Ln|Lane|Cl|Close|Ct|Court|Pl|Place|"
    r"Cres|Crescent|Pde|Parade|Way|Tce|Terrace|Hwy|Highway)\b[^,]{0,40}"
)


def _address_from_context(context: str) -> str:
    m = _ADDRESS_RE.search(context)
    return m.group(0).strip() if m else ""


def _price_text_from_context(context: str) -> str:
    m = re.search(r"\$[\d,][^|<\n]{0,40}", context)
    return m.group(0).strip() if m else ""


def _title_from_context(context: str) -> str:
    address = _address_from_context(context)
    if address:
        return address
    return context[:110].strip()


def fetch_profile(service, cfg: dict[str, Any], profile: str) -> list[Listing]:
    gmail_cfg = cfg.get("gmail") or {}
    label = (gmail_cfg.get("labels") or {}).get(profile)
    if not label:
        raise PipelineError(f"gmail.labels.{profile} is not configured")
    lookback = int(gmail_cfg.get("lookback_days", 8))
    after = (datetime.now(timezone.utc) - timedelta(days=lookback)).strftime("%Y/%m/%d")
    query = f'label:"{label}" after:{after}'

    log.info("[%s] Gmail query: %s", profile, query)
    message_ids: list[str] = []
    page_token: str | None = None
    while True:
        resp = (
            service.users()
            .messages()
            .list(userId="me", q=query, pageToken=page_token, maxResults=100)
            .execute()
        )
        message_ids.extend(m["id"] for m in resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    if not message_ids:
        log.warning(
            "[%s] no alert emails matched label %r in the last %d days — "
            "check the Gmail filter is routing alerts into that label",
            profile,
            label,
            lookback,
        )

    listings: list[Listing] = []
    for mid in message_ids:
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=mid, format="full")
            .execute()
        )
        listings.extend(listings_from_message(msg, profile))

    listings = dedupe(listings)
    log.info(
        "[%s] %d listings from %d alert emails", profile, len(listings), len(message_ids)
    )
    return listings


def main() -> int:
    cfg = load_config()
    if "path_a_email_alerts" not in enabled_sources(cfg):
        log.info("path_a_email_alerts not in data_sources — writing empty stages")
        for profile in cfg["profiles"]:
            write_stage(profile, "email_alerts", [])
        return 0

    profiles = list(cfg["profiles"])
    return run_source(
        "email_alerts",
        "email_alerts",
        profiles,
        lambda p: fetch_profile(gmail_service(cfg), cfg, p),
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as exc:
        log.error("%s", exc)
        sys.exit(1)
