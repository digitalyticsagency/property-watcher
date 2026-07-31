"""Render docs/index.html — the multi-tab, two-profile dashboard.

Structure (Step 7 of the brief): a profile switcher at the top, and beneath it
five tabs per profile — Overview, All current matches, By fit score, Tag filters,
Archive. The profiles never share a list or a score scale.

Listings that were never verified carry a visible caution badge and a left rule
on the card, so a snippet-only result can't be mistaken for a confirmed one.

The page ships its data inline as JSON and does the tab/filter work in vanilla
JS — no build step, no CDN, and it works straight off GitHub Pages.
"""

from __future__ import annotations

import html
import json
import sys
from datetime import datetime, timezone
from typing import Any

from common import (
    DOCS_DIR,
    Listing,
    PipelineError,
    WORK_DIR,
    load_config,
    log,
    now_iso,
    profile_config,
    read_json,
    read_stage,
)

TABS = [
    ("overview", "Overview"),
    ("all_matches", "All current matches"),
    ("by_score", "By fit score"),
    ("tags", "Tag filters"),
    ("archive", "Archive"),
]


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def listing_payload(listing: Listing, is_new: bool) -> dict[str, Any]:
    return {
        "id": listing.id,
        "url": listing.url,
        "title": listing.title or listing.url,
        "address": listing.address,
        "source": listing.source,
        "verified": listing.verified,
        "unverified_reason": listing.unverified_reason,
        "price_text": listing.price_text,
        "price_aud": listing.price_aud,
        "land_size_m2": listing.land_size_m2,
        "bedrooms": listing.bedrooms,
        "bathrooms": listing.bathrooms,
        "drive_hours": listing.drive_hours,
        "distance_source": listing.distance_source,
        "granny_flat_status": listing.granny_flat_status,
        "granny_flat_reasoning": listing.granny_flat_reasoning,
        "score": listing.score,
        "score_reason": listing.score_reason,
        "tags": listing.tags,
        "region": listing.suburb or _region_guess(listing),
        "is_new": is_new,
        "first_seen": listing.first_seen,
        "last_seen": listing.last_seen,
    }


def _region_guess(listing: Listing) -> str:
    """Fall back to the trailing locality in the address for the region filter."""
    if not listing.address:
        return ""
    parts = [p.strip() for p in listing.address.split(",") if p.strip()]
    return parts[-1] if parts else ""


def build_profile_data(cfg: dict[str, Any], profile: str, commentary: dict[str, Any]) -> dict[str, Any]:
    listings = read_stage(profile, "ranked")
    new_ids = set(commentary.get("new_ids") or [])
    archive_raw = read_json(
        (WORK_DIR.parent / profile / "archive" / "all_listings.json"), {}
    )

    current = [listing_payload(l, l.id in new_ids) for l in listings]
    current_ids = {c["id"] for c in current}
    archive = [
        listing_payload(Listing.from_dict(d), False)
        for key, d in sorted(archive_raw.items())
        if key not in current_ids
    ]

    p = profile_config(cfg, profile)
    return {
        "key": profile,
        "label": p.get("label", profile.replace("_", " ").title()),
        "criteria": {
            "regions": p.get("target_regions", []),
            "budget_max": p.get("budget_max_aud"),
            "budget_min": p.get("budget_min_aud") or 0,
            "min_land": p.get("min_land_size_m2"),
            "max_drive": p.get("max_drive_hours_from_sydney_cbd"),
        },
        "commentary": {
            "weekly_summary": commentary.get("weekly_summary", ""),
            "top_pick_ids": commentary.get("top_pick_ids", []),
            "tab_intros": commentary.get("tab_intros", {}),
        },
        "current": current,
        "archive": archive,
        "stats": {
            "total": len(current),
            "new": sum(1 for c in current if c["is_new"]),
            "verified": sum(1 for c in current if c["verified"]),
            "unverified": sum(1 for c in current if not c["verified"]),
            "archive": len(archive),
        },
    }


PAGE_TEMPLATE = """<!doctype html>
<html lang="en-AU">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>NSW Property Watcher</title>
<link rel="stylesheet" href="assets/style.css">
</head>
<body>
<header class="site">
  <div class="wrap">
    <h1>NSW Property Watcher</h1>
    <p class="meta">Last refreshed {generated_human} · {generated_iso}</p>
    <nav class="profiles" role="tablist" aria-label="Buyer profile">{profile_buttons}</nav>
  </div>
</header>

<main class="wrap" id="app"></main>

<footer class="site wrap">
  <p>Listings reached via saved-search email alerts, the Google Custom Search JSON API,
  and (where enabled) the Domain Developer API. Cards marked
  <span class="badge warn">UNVERIFIED</span> come from a search-result snippet only —
  the listing page could not be read, so open the link before trusting any detail.
  Granny-flat assessments are automated reasoning against NSW secondary dwelling
  rules, not planning advice; confirm with the relevant council.</p>
</footer>

<script id="data" type="application/json">{data_json}</script>
<script>
const DATA = JSON.parse(document.getElementById('data').textContent);
const TABS = {tabs_json};
let activeProfile = DATA.profiles[0].key;
let activeTab = 'overview';
let filters = {{ region: '', source: '', tag: '' }};

const profileOf = key => DATA.profiles.find(p => p.key === key);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));

function scoreClass(score) {{
  if (score === null || score === undefined) return 'none';
  if (score >= 8) return 'high';
  if (score >= 5) return 'mid';
  return 'low';
}}

function facts(item) {{
  const bits = [];
  if (item.price_text) bits.push(esc(item.price_text));
  else if (item.price_aud) bits.push('$' + item.price_aud.toLocaleString('en-AU'));
  if (item.land_size_m2) bits.push(item.land_size_m2.toLocaleString('en-AU') + ' m²');
  if (item.bedrooms) bits.push(item.bedrooms + ' bed');
  if (item.bathrooms) bits.push(item.bathrooms + ' bath');
  if (item.drive_hours !== null && item.drive_hours !== undefined) {{
    const suffix = item.distance_source === 'estimated' ? ' (est.)' : '';
    bits.push(item.drive_hours.toFixed(1) + ' h from Sydney' + suffix);
  }}
  return bits.map(b => `<span>${{b}}</span>`).join('');
}}

function badges(item) {{
  const out = [];
  if (item.is_new) out.push('<span class="badge new">NEW THIS WEEK</span>');
  if (!item.verified) {{
    const why = item.unverified_reason ? ' — ' + esc(item.unverified_reason) : '';
    out.push(`<span class="badge warn" title="Open the link to confirm${{why}}">⚠ UNVERIFIED — open link to confirm</span>`);
  }}
  out.push(`<span class="badge">via ${{item.source === 'email_alert' ? 'email alert'
    : item.source === 'search_api' ? 'web search' : 'Domain API'}}</span>`);
  if (item.granny_flat_status) {{
    const kind = item.granny_flat_status.startsWith('confirmed') ? 'gf-confirmed'
      : item.granny_flat_status.startsWith('likely') ? 'gf-likely' : 'gf-unclear';
    out.push(`<span class="badge ${{kind}}">granny flat: ${{esc(item.granny_flat_status)}}</span>`);
  }}
  // granny-flat:* is kept in tags so the Tag filters tab can select on it, but
  // it already has a dedicated badge above — don't render it twice.
  (item.tags || [])
    .filter(t => !t.startsWith('granny-flat:'))
    .forEach(t => out.push(`<span class="badge">${{esc(t)}}</span>`));
  return out.join('');
}}

function card(item) {{
  const score = item.score === null || item.score === undefined
    ? '<div class="score none">n/a</div>'
    : `<div class="score ${{scoreClass(item.score)}}">${{item.score}}</div>`;
  const zoning = item.granny_flat_reasoning
    ? `<p class="zoning"><strong>Zoning reasoning:</strong> ${{esc(item.granny_flat_reasoning)}}</p>` : '';
  return `<article class="card ${{item.verified ? '' : 'is-unverified'}}">
    <div>
      <h3><a href="${{esc(item.url)}}" target="_blank" rel="noopener noreferrer">${{esc(item.title)}}</a></h3>
      <p class="facts">${{facts(item)}}</p>
      <p class="reason">${{esc(item.score_reason || '')}}</p>
      ${{zoning}}
    </div>
    ${{score}}
    <div class="badges">${{badges(item)}}</div>
  </article>`;
}}

function cards(items, emptyMsg) {{
  if (!items.length) return `<div class="empty">${{esc(emptyMsg)}}</div>`;
  return `<div class="cards">${{items.map(card).join('')}}</div>`;
}}

function applyFilters(items) {{
  return items.filter(i =>
    (!filters.region || (i.region || '').toLowerCase().includes(filters.region.toLowerCase())) &&
    (!filters.source || i.source === filters.source) &&
    (!filters.tag || (i.tags || []).includes(filters.tag)));
}}

function filterBar(items, opts) {{
  const regions = [...new Set(items.map(i => i.region).filter(Boolean))].sort();
  const tags = [...new Set(items.flatMap(i => i.tags || []))].sort();
  const parts = [];
  if (opts.region) parts.push(`<label>Region
    <select data-filter="region"><option value="">All</option>
    ${{regions.map(r => `<option ${{filters.region === r ? 'selected' : ''}}>${{esc(r)}}</option>`).join('')}}
    </select></label>`);
  if (opts.source) parts.push(`<label>Source
    <select data-filter="source"><option value="">All</option>
      <option value="email_alert" ${{filters.source === 'email_alert' ? 'selected' : ''}}>Email alert</option>
      <option value="search_api" ${{filters.source === 'search_api' ? 'selected' : ''}}>Web search (unverified)</option>
      <option value="domain_api" ${{filters.source === 'domain_api' ? 'selected' : ''}}>Domain API</option>
    </select></label>`);
  if (opts.tag) parts.push(`<label>Tag
    <select data-filter="tag"><option value="">All</option>
    ${{tags.map(t => `<option ${{filters.tag === t ? 'selected' : ''}}>${{esc(t)}}</option>`).join('')}}
    </select></label>`);
  return parts.length ? `<div class="filters">${{parts.join('')}}</div>` : '';
}}

function render() {{
  const p = profileOf(activeProfile);
  const intro = esc((p.commentary.tab_intros || {{}})[activeTab] || '');
  const tabStrip = TABS.map(([key, label]) => {{
    const n = key === 'archive' ? p.archive.length : (key === 'overview' ? null : p.current.length);
    return `<button role="tab" data-tab="${{key}}" aria-selected="${{key === activeTab}}">
      ${{label}}${{n === null ? '' : ` <span class="count">${{n}}</span>`}}</button>`;
  }}).join('');

  let body = '';
  if (activeTab === 'overview') {{
    const picks = p.commentary.top_pick_ids
      .map(id => p.current.find(c => c.id === id)).filter(Boolean);
    body = `
      <div class="stats">
        <div class="stat"><div class="n">${{p.stats.new}}</div><div class="k">New this week</div></div>
        <div class="stat"><div class="n">${{p.stats.total}}</div><div class="k">Current matches</div></div>
        <div class="stat"><div class="n">${{p.stats.verified}}</div><div class="k">Verified</div></div>
        <div class="stat"><div class="n">${{p.stats.unverified}}</div><div class="k">Unverified</div></div>
        <div class="stat"><div class="n">${{p.stats.archive}}</div><div class="k">In archive</div></div>
      </div>
      <h2>What changed this week</h2>
      <p>${{esc(p.commentary.weekly_summary)}}</p>
      <h2>Top ${{picks.length}} picks</h2>
      ${{cards(picks, 'No standout picks this week.')}}`;
  }} else if (activeTab === 'all_matches') {{
    const items = applyFilters(p.current);
    body = filterBar(p.current, {{region: true, source: true, tag: false}})
      + cards(items, 'No listings match these filters.');
  }} else if (activeTab === 'by_score') {{
    const items = [...p.current].sort((a, b) => (b.score ?? -1) - (a.score ?? -1));
    body = cards(items, 'Nothing scored yet.');
  }} else if (activeTab === 'tags') {{
    const items = applyFilters(p.current);
    body = filterBar(p.current, {{region: false, source: true, tag: true}})
      + cards(items, 'No listings carry that tag.');
  }} else {{
    body = cards(p.archive, 'The archive is empty — nothing has dropped off the current list yet.');
  }}

  document.getElementById('app').innerHTML =
    `<nav class="tabs" role="tablist">${{tabStrip}}</nav>`
    + (intro ? `<p class="intro">${{intro}}</p>` : '')
    + body;

  document.querySelectorAll('.profiles button').forEach(b =>
    b.setAttribute('aria-selected', b.dataset.profile === activeProfile));
}}

document.addEventListener('click', e => {{
  const profileBtn = e.target.closest('.profiles button');
  if (profileBtn) {{
    activeProfile = profileBtn.dataset.profile;
    activeTab = 'overview';
    filters = {{ region: '', source: '', tag: '' }};
    render();
    return;
  }}
  const tabBtn = e.target.closest('.tabs button');
  if (tabBtn) {{
    activeTab = tabBtn.dataset.tab;
    render();
  }}
}});

document.addEventListener('change', e => {{
  const sel = e.target.closest('[data-filter]');
  if (sel) {{
    filters[sel.dataset.filter] = sel.value;
    render();
  }}
}});

render();
</script>
</body>
</html>
"""


def build() -> str:
    cfg = load_config()
    commentary_all = read_json(WORK_DIR / "commentary.json", {}).get("profiles", {})

    profiles = [
        build_profile_data(cfg, profile, commentary_all.get(profile, {}))
        for profile in cfg["profiles"]
    ]

    generated = datetime.now(timezone.utc)
    payload = {"generated_at": generated.isoformat(timespec="seconds"), "profiles": profiles}

    buttons = "".join(
        f'<button role="tab" data-profile="{esc(p["key"])}" '
        f'aria-selected="{"true" if i == 0 else "false"}">{esc(p["label"])}</button>'
        for i, p in enumerate(profiles)
    )

    return PAGE_TEMPLATE.format(
        generated_human=generated.strftime("%d %b %Y, %H:%M UTC"),
        generated_iso=generated.isoformat(timespec="seconds"),
        profile_buttons=buttons,
        data_json=json.dumps(payload, ensure_ascii=False).replace("</", "<\\/"),
        tabs_json=json.dumps(TABS),
    )


def main() -> int:
    html_out = build()
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / "index.html").write_text(html_out, encoding="utf-8")
    (DOCS_DIR / ".nojekyll").write_text("", encoding="utf-8")
    log.info("wrote %s (%d bytes)", DOCS_DIR / "index.html", len(html_out))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as exc:
        log.error("%s", exc)
        sys.exit(1)
