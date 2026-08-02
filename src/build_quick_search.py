"""Generate docs/search.html — searches you can retune in the browser.

The criteria in config.yaml are a starting point, not a cage. This page ships
them as defaults and then lets you move price, land size and bedrooms with the
controls at the top; every portal link rebuilds instantly from whatever the
controls currently say. Nothing is recomputed on a server, so there is no run to
wait for and nothing to redeploy when you change your mind.

Drive times are the exception: they are measured once at build time via OSRM,
because they depend on the region rather than on your filters.

Regenerate after editing config.yaml:
    PYTHONPATH=src python src/build_quick_search.py
"""

from __future__ import annotations

import html
import json
import sys
from datetime import datetime, timezone

from common import (
    DOCS_DIR,
    PipelineError,
    load_config,
    log,
    profile_config,
    read_json,
    write_json,
)
from geocode_distance import GEOCODE_CACHE, ROUTE_CACHE, drive_hours, geocode


def esc(v: object) -> str:
    return html.escape(str(v if v is not None else ""), quote=True)


# Features worth searching for, chosen per profile. These are the words agents
# actually write in listing copy — "creek frontage", not "waterway adjacency" —
# because the search only matches what is really in the ad.
FEATURES = {
    "lifestyle_acreage": [
        "creek", "dam", "spring water", "water views", "bush outlook",
        "north facing", "level block", "cleared paddocks", "usable land",
        "shed", "workshop", "stables", "orchard", "vegetable garden",
        "chicken coop", "town water", "rainwater tank", "solar",
        "granny flat", "second dwelling", "subdividable", "off grid",
        "no through road", "private", "views",
    ],
    "house_with_granny_flat": [
        "granny flat", "approved granny flat", "self contained", "dual living",
        "dual income", "separate entrance", "side access", "tenanted",
        "renovated", "original condition", "level block", "north facing",
        "walk to station", "walk to schools", "walk to shops", "quiet street",
        "cul de sac", "parkland", "creek", "reserve", "green outlook",
        "large backyard", "pool", "shed", "subdividable",
    ],
}


def region_drive_hours(region: str, cfg: dict) -> tuple[float | None, str]:
    """Drive time from Sydney CBD to the region centroid, measured at build time."""
    geo_cache = read_json(GEOCODE_CACHE, {})
    route_cache = read_json(ROUTE_CACHE, {})
    origin = tuple((cfg.get("geocoding") or {}).get("sydney_cbd", [-33.8688, 151.2093]))

    coords = geocode(region, cfg, geo_cache)
    write_json(GEOCODE_CACHE, geo_cache)
    if not coords:
        return None, "unknown"

    hours, source = drive_hours(origin, coords, cfg, route_cache)
    write_json(ROUTE_CACHE, route_cache)
    return round(hours, 1), source


CSS = """
body{margin:0;background:var(--bg);color:var(--ink);
  font:16px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:940px;margin:0 auto;padding:32px 20px 72px}
h1{font-size:1.5rem;margin:0 0 4px;letter-spacing:-.01em}
.meta{color:var(--ink-faint);font-size:.85rem;margin-bottom:22px}
.nav{margin-bottom:18px;font-size:.9rem}.nav a{color:var(--accent)}
.note{background:var(--accent-soft);border-radius:var(--radius);padding:14px 18px;
  font-size:.92rem;color:var(--ink-soft);margin:20px 0 26px}

/* Controls. Deliberately at the top and always visible — they are the point of
   the page, not a settings drawer you have to go find. */
.controls{background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius);padding:18px 20px;margin:0 0 10px;box-shadow:var(--shadow)}
.controls h2{font-size:1.02rem;margin:0 0 2px}
.controls .hint{font-size:.82rem;color:var(--ink-faint);margin:0 0 16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:16px 20px}
.field label{display:block;font-size:.74rem;letter-spacing:.05em;text-transform:uppercase;
  color:var(--ink-faint);margin-bottom:6px}
.field .val{font-size:1.12rem;font-weight:650;font-variant-numeric:tabular-nums;
  margin-bottom:6px;color:var(--ink)}
input[type=range]{width:100%;accent-color:var(--accent);cursor:pointer}
select{width:100%;font:inherit;font-size:.9rem;padding:7px 10px;border:1px solid var(--border);
  border-radius:8px;background:var(--bg);color:var(--ink)}
.reset{margin-top:16px;font:inherit;font-size:.84rem;padding:7px 14px;cursor:pointer;
  border:1px solid var(--border);border-radius:999px;background:var(--bg);color:var(--ink-soft)}
.reset:hover{border-color:var(--accent);color:var(--accent)}
.changed{color:var(--warn);font-size:.8rem;margin-left:10px}

h2.profile{font-size:1.15rem;margin:30px 0 4px}
.crit{color:var(--ink-soft);font-size:.88rem;margin:0 0 16px}
.region{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  padding:14px 16px;margin-bottom:10px;box-shadow:var(--shadow);
  display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.region .name{font-weight:650;min-width:160px}
.region .drive{font-size:.8rem;font-variant-numeric:tabular-nums;min-width:128px;color:var(--ink-faint)}
.region .drive.over{color:var(--warn);font-weight:600}
.region .drive.unk{font-style:italic}
.region a{font-size:.86rem;text-decoration:none;color:var(--accent);
  border:1px solid var(--border);border-radius:999px;padding:5px 12px;background:var(--bg)}
.region a:hover{border-color:var(--accent)}
.region.hidden{display:none}
@media (max-width:620px){.region .name,.region .drive{min-width:0;flex-basis:100%}}
"""


PAGE = """<!doctype html>
<html lang="en-AU"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>Quick search — NSW Property Watcher</title>
<link rel="stylesheet" href="assets/style.css"><style>{css}</style></head>
<body><div class="wrap">
<h1>Quick search</h1>
<p class="meta">Built {built} · move the sliders, the links update as you go</p>
<p class="nav"><a href="index.html">← back to the dashboard</a></p>

<div class="note"><strong>No setup, no waiting.</strong> Change anything below and every
link rebuilds instantly — nothing is saved, nothing re-runs, so you can try a bigger budget
or a smaller block and see what it opens up. Domain links carry price, land size and
property type, using place slugs taken from Google's index so they resolve. realestate.com.au
is reached through a scoped search rather than a direct link — its URL format cannot be
verified from here, and guessing it produced broken links. Drive times are measured to the centre
of each region, so a large area can read as over your limit while its near edge is well
inside it.</div>

<div id="app"></div>
</div>

<script id="data" type="application/json">{data}</script>
<script>
const DATA = JSON.parse(document.getElementById('data').textContent);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
const slug = r => encodeURIComponent(r.toLowerCase().replace(/ /g, '-'));
const money = n => '$' + n.toLocaleString('en-AU');

/* Live filter state, seeded from config.yaml and reset back to it on demand. */
const state = {{}};
DATA.profiles.forEach(p => state[p.key] = {{
  budget: p.budget_max, land: p.min_land, beds: 0, drive: p.max_drive,
  text: '', picked: [],
}});

/* Everything the user asked for, free text plus tapped chips, de-duplicated. */
function terms(s) {{
  const typed = (s.text || '').split(/[,;]+/).map(t => t.trim()).filter(Boolean);
  return [...new Set([...s.picked, ...typed])];
}}

/* A site-scoped Google query the user clicks. The portals reject automated
   requests and accept no keyword parameter in a link, so a human-clicked search
   is the only route free text has. Nothing here fetches or parses results. */
function keywordSearchUrl(p, s) {{
  const sites = ['realestate.com.au','domain.com.au','allhomes.com.au','ratemyagent.com.au'];
  const regions = p.regions.filter(r => r.hours === null || r.hours <= s.drive)
                           .map(r => r.name);
  const words = terms(s).map(t => `"${{t}}"`).join(' ');
  const q = [
    p.acreage ? 'acreage OR "rural lifestyle"' : '"granny flat" OR "dual living"',
    words,
    '(' + regions.map(r => `"${{r}}"`).join(' OR ') + ')',
    'NSW "for sale"',
    '(' + sites.map(x => 'site:' + x).join(' OR ') + ')',
  ].join(' ');
  return 'https://www.google.com/search?q=' + encodeURIComponent(q);
}}

/* Domain, built from a slug taken out of Google's index. Property type is a
   PATH segment on Domain, not a query parameter — /sale/<place>/house/ — which
   is what the earlier version got wrong along with the missing postcode. */
function domainUrl(p, r, s) {{
  if (!r.slug) return null;
  const type = p.acreage ? 'acreage-semi-rural' : 'house';
  const beds = s.beds ? `&bedrooms=${{s.beds}}-any` : '';
  return `https://www.domain.com.au/sale/${{r.slug}}/${{type}}/`
       + `?price=0-${{s.budget}}&landsize=${{s.land}}-any&landsizeunit=m2${{beds}}`
       + `&excludeunderoffer=1`;
}}

/* allhomes publishes suburb pages only, on the same slug shape. */
function allhomesUrl(p, r, s) {{
  if (!r.slug || !r.is_suburb) return null;
  return `https://www.allhomes.com.au/sale/${{r.slug}}/`;
}}

/* realestate.com.au is deliberately NOT linked directly. Its URL grammar is
   undocumented, it answers 429 to curl, it is blocked in the browser here, and
   it is excluded from the search index this project can reach — so there is no
   way to confirm a URL before shipping it. Guessing produced the 404s. A
   site-scoped search always resolves and lands on the same listings. */
function reaSearchUrl(p, r, s) {{
  const words = terms(s).map(t => `"${{t}}"`).join(' ');
  const q = `${{p.acreage ? 'acreage' : '"granny flat" OR house'}} "${{r.name}}" NSW `
          + `"for sale" ${{words}} site:realestate.com.au`;
  return 'https://www.google.com/search?q=' + encodeURIComponent(q);
}}

function controls(p) {{
  const s = state[p.key];
  const dirty = s.budget !== p.budget_max || s.land !== p.min_land || s.beds !== 0;
  return `<div class="controls">
    <h2>Change what you are looking for</h2>
    <p class="hint">Starts from your saved criteria. Nothing here is saved — it only changes
      the links below.${{dirty ? '<span class="changed">· changed from your saved criteria</span>' : ''}}</p>
    <div class="grid">
      <div class="field">
        <label for="b-${{p.key}}">Most you would pay</label>
        <div class="val">${{money(s.budget)}}</div>
        <input id="b-${{p.key}}" type="range" data-p="${{p.key}}" data-k="budget"
          min="${{p.budget_floor}}" max="${{p.budget_ceiling}}" step="25000" value="${{s.budget}}">
      </div>
      <div class="field">
        <label for="l-${{p.key}}">Smallest block</label>
        <div class="val">${{s.land.toLocaleString('en-AU')}} m²</div>
        <input id="l-${{p.key}}" type="range" data-p="${{p.key}}" data-k="land"
          min="${{p.land_floor}}" max="${{p.land_ceiling}}" step="${{p.land_step}}" value="${{s.land}}">
      </div>
      <div class="field">
        <label for="d-${{p.key}}">Bedrooms, at least</label>
        <select id="d-${{p.key}}" data-p="${{p.key}}" data-k="beds">
          ${{[0,1,2,3,4,5].map(n => `<option value="${{n}}" ${{s.beds===n?'selected':''}}>`
            + (n ? n + '+' : 'Any') + '</option>').join('')}}
        </select>
      </div>
      <div class="field">
        <label for="h-${{p.key}}">Furthest you would drive</label>
        <div class="val">${{s.drive}} h</div>
        <input id="h-${{p.key}}" type="range" data-p="${{p.key}}" data-k="drive"
          min="0.5" max="6" step="0.5" value="${{s.drive}}">
      </div>
    </div>
    <div class="extras">
      <label for="k-${{p.key}}">Anything else that matters to you</label>
      <p class="hint">Type what you want — "backs onto a creek", "big shed", "green outlook" —
        or tap the suggestions. This searches the words agents actually write in listings.</p>
      <input id="k-${{p.key}}" type="text" class="kw" data-p="${{p.key}}" data-k="text"
        placeholder="e.g. creek, level block, big shed" value="${{esc(s.text)}}">
      <div class="chips">${{p.features.map(f =>
        `<button class="chip ${{s.picked.includes(f) ? 'on' : ''}}"
           data-chip="${{p.key}}" data-f="${{esc(f)}}">${{esc(f)}}</button>`).join('')}}</div>
      ${{terms(s).length ? `<div class="kwgo">
        <button class="openall" data-kw="${{p.key}}">Search these words across your areas</button>
        <span class="kwnote">Opens Google, scoped to the property sites. The portals accept
          no keyword in a link, so this is the only route free text has. Expect a mix of
          individual listings and the portals' own category pages — scroll past the category
          pages, or add a suburb name to sharpen it.</span>
      </div>` : ''}}
    </div>
    <button class="reset" data-reset="${{p.key}}">Back to my saved criteria</button>
  </div>`;
}}

function regions(p) {{
  const s = state[p.key];
  return p.regions.map(r => {{
    const over = r.hours !== null && r.hours > s.drive;
    const drive = r.hours === null
      ? '<span class="drive unk">drive time unknown</span>'
      : `<span class="drive ${{over ? 'over' : ''}}">${{r.hours}} h to centre${{
          r.estimated ? ' approx.' : ''}}${{over ? ' — further than you said' : ''}}</span>`;

    const links = [];
    const dom = domainUrl(p, r, s);
    if (dom) links.push(`<a href="${{esc(dom)}}" target="_blank" rel="noopener noreferrer"
      title="Domain — price, property type, land size${{s.beds ? ', bedrooms' : ''}}">domain.com.au</a>`);
    const ah = allhomesUrl(p, r, s);
    if (ah) links.push(`<a href="${{esc(ah)}}" target="_blank" rel="noopener noreferrer"
      title="allhomes — suburb listings, set filters on their page">allhomes</a>`);
    links.push(`<a href="${{esc(reaSearchUrl(p, r, s))}}" target="_blank" rel="noopener noreferrer"
      class="viasearch" title="Searches realestate.com.au via Google — their link format cannot be verified, so a direct link risks a 404">realestate.com.au ↗</a>`);

    return `<div class="region"><span class="name">${{esc(r.name)}}</span>${{drive}}${{links.join('')}}</div>`;
  }}).join('');
}}

function render() {{
  document.getElementById('app').innerHTML = DATA.profiles.map(p => {{
    const s = state[p.key];
    return `<h2 class="profile">${{esc(p.label)}}</h2>
      <p class="crit">Showing: up to ${{money(s.budget)}} · land from ${{s.land.toLocaleString('en-AU')}} m²
        ${{s.beds ? ' · ' + s.beds + '+ bedrooms' : ''}} · within ${{s.drive}} h of Sydney CBD
        <button class="openall" data-openall="${{p.key}}">Open all ${{
          p.regions.filter(r => (r.hours === null || r.hours <= s.drive) && domainUrl(p, r, s)).length
        }} searches</button></p>
      ${{controls(p)}}${{regions(p)}}`;
  }}).join('');
}}

document.addEventListener('input', e => {{
  const el = e.target.closest('[data-p]');
  if (!el) return;
  if (el.dataset.k === 'text') {{
    // Re-rendering on every keystroke would steal focus mid-word, so update
    // state quietly and only refresh the button that depends on it.
    state[el.dataset.p].text = el.value;
    const box = el.closest('.extras').querySelector('.kwgo');
    const need = terms(state[el.dataset.p]).length > 0;
    if (need && !box) render();
    else if (!need && box) render();
    return;
  }}
  state[el.dataset.p][el.dataset.k] = Number(el.value);
  render();
}});

document.addEventListener('click', e => {{
  const chip = e.target.closest('[data-chip]');
  if (chip) {{
    const s = state[chip.dataset.chip], f = chip.dataset.f;
    s.picked = s.picked.includes(f) ? s.picked.filter(x => x !== f) : [...s.picked, f];
    render();
    return;
  }}
  const kw = e.target.closest('[data-kw]');
  if (kw) {{
    const p = DATA.profiles.find(x => x.key === kw.dataset.kw);
    window.open(keywordSearchUrl(p, state[p.key]), '_blank', 'noopener');
  }}
}});
document.addEventListener('change', e => {{
  const el = e.target.closest('select[data-p]');
  if (el) {{ state[el.dataset.p][el.dataset.k] = Number(el.value); render(); }}
}});

/* Opens one tab per region on the portal that carries the most filters.
   Browsers block bulk window.open unless it is inside a real click handler,
   which this is; some still cap it, hence the note in the button's title. */
document.addEventListener('click', e => {{
  const btn = e.target.closest('[data-openall]');
  if (!btn) return;
  const p = DATA.profiles.find(x => x.key === btn.dataset.openall);
  const s = state[p.key];
  p.regions
    .filter(r => (r.hours === null || r.hours <= s.drive) && domainUrl(p, r, s))
    .forEach(r => window.open(domainUrl(p, r, s), '_blank', 'noopener'));
}});
document.addEventListener('click', e => {{
  const btn = e.target.closest('[data-reset]');
  if (!btn) return;
  const p = DATA.profiles.find(x => x.key === btn.dataset.reset);
  state[p.key] = {{ budget: p.budget_max, land: p.min_land, beds: 0, drive: p.max_drive,
    text: '', picked: [] }};
  render();
}});

render();
</script>
</body></html>
"""


def build() -> str:
    cfg = load_config()
    profiles = []

    for key in cfg["profiles"]:
        p = profile_config(cfg, key)
        budget = p["budget_max_aud"]
        land = p["min_land_size_m2"]
        acreage = key == "lifestyle_acreage"

        regions = []
        for region in p.get("target_regions", []):
            hours, source = region_drive_hours(region, cfg)
            regions.append(
                {"name": region, "hours": hours, "estimated": source == "estimated"}
            )

        profiles.append({
            "key": key,
            "label": p.get("label", key.replace("_", " ").title()),
            "acreage": acreage,
            "budget_max": budget,
            "min_land": land,
            "max_drive": p["max_drive_hours_from_sydney_cbd"],
            # Slider ranges bracket the saved value so it is always adjustable in
            # both directions, without offering silly extremes.
            "budget_floor": max(200_000, int(budget * 0.5 // 25_000) * 25_000),
            "budget_ceiling": int(budget * 1.75 // 25_000) * 25_000,
            # 450 m² is the floor for a reason: it is roughly the lot size the
            # complying-development pathway wants for a secondary dwelling, so a
            # slider that went below it would offer searches where a granny flat
            # is the hard path. Acreage starts at 1,000 m² for the same reason —
            # anything smaller isn't the thing that profile is looking for.
            "land_floor": 450 if not acreage else 1_000,
            "land_ceiling": max(2_000, land * 5),
            "land_step": 50 if not acreage else 1_000,
            "regions": regions,
            "features": FEATURES.get(key, []),
        })

    slugs = cfg.get("portal_slugs") or {}
    for prof in profiles:
        for r in prof["regions"]:
            slug = slugs.get(r["name"], "")
            r["slug"] = slug
            # A postcode in the slug means it is a suburb, which is the only
            # level allhomes publishes a page for.
            r["is_suburb"] = bool(slug) and slug.rsplit("-", 1)[-1].isdigit()

    return PAGE.format(
        css=CSS,
        built=datetime.now(timezone.utc).strftime("%d %b %Y"),
        data=json.dumps({"profiles": profiles}, ensure_ascii=False).replace("</", "<\\/"),
    )


def main() -> int:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / "search.html").write_text(build(), encoding="utf-8")
    log.info("wrote %s", DOCS_DIR / "search.html")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as exc:
        log.error("%s", exc)
        sys.exit(1)
