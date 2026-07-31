"""Generate docs/search.html — one-click, pre-filtered searches per profile.

Why this exists: the automated pipeline needs API keys and OAuth that turned out
to be more friction than the results were worth. This page needs none of that.
It reads the same config.yaml criteria and turns them into deep links that open
each portal already filtered to your budget, land size and suburb — so the
"search" step is one click instead of a form you re-fill every time.

Regenerate after editing config.yaml:
    PYTHONPATH=src python src/build_quick_search.py
"""

from __future__ import annotations

import html
import sys
from datetime import datetime, timezone
from urllib.parse import quote

from common import DOCS_DIR, PipelineError, load_config, log, profile_config, read_json, write_json
from geocode_distance import GEOCODE_CACHE, ROUTE_CACHE, drive_hours, geocode


def esc(v: object) -> str:
    return html.escape(str(v if v is not None else ""), quote=True)


def slug(region: str) -> str:
    return quote(region.lower().replace(" ", "-"))


def portal_links(region: str, profile: str, p: dict) -> list[tuple[str, str, str]]:
    """Deep links per portal: (name, url, which filters that link actually applies).

    The two portals get different treatment on purpose.

    Domain documents its query-string filters (price, landsize + landsizeunit,
    ptype, excludeunderoffer), so the full criteria go in and the link lands on a
    correctly filtered list.

    realestate.com.au uses a path-slug grammar that is undocumented, and it can
    be verified from neither curl (429) nor a browser here (blocked by policy).
    So it gets only the parts of that grammar that are well attested — property
    type and price band — and no invented land-size segment. Overstating what a
    link filters is worse than under-filtering, because you would trust a result
    list that had quietly ignored your land requirement.
    """
    budget = p["budget_max_aud"]
    land = p["min_land_size_m2"]
    acreage = profile == "lifestyle_acreage"

    rea_type = "property-acreage+semi-rural" if acreage else "property-house"
    rea = (
        f"https://www.realestate.com.au/buy/{rea_type}-between-0-{budget}"
        f"-in-{slug(region)},+nsw/list-1"
    )

    dom_type = "acreage-semi-rural,rural" if acreage else "house,duplex,semi-detached"
    dom = (
        f"https://www.domain.com.au/sale/{slug(region)}-nsw/"
        f"?price=0-{budget}&landsize={land}-any&landsizeunit=m2"
        f"&ptype={dom_type}&excludeunderoffer=1"
    )

    return [
        ("realestate.com.au", rea, "price + type"),
        ("domain.com.au", dom, "price + type + land size"),
    ]


CSS = """
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:900px;margin:0 auto;padding:32px 20px 72px}
h1{font-size:1.5rem;margin:0 0 4px;letter-spacing:-.01em}
.meta{color:var(--ink-faint);font-size:.85rem;margin-bottom:28px}
h2{font-size:1.15rem;margin:34px 0 4px}
.crit{color:var(--ink-soft);font-size:.88rem;margin:0 0 16px}
.region{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  padding:14px 16px;margin-bottom:10px;box-shadow:var(--shadow);
  display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.region .name{font-weight:650;min-width:170px}
.region a{font-size:.86rem;text-decoration:none;color:var(--accent);
  border:1px solid var(--border);border-radius:999px;padding:5px 12px;background:var(--bg)}
.region a:hover{border-color:var(--accent)}
.note{background:var(--accent-soft);border-radius:var(--radius);padding:14px 18px;
  font-size:.92rem;color:var(--ink-soft);margin:20px 0 8px}
.nav{margin-bottom:18px;font-size:.9rem}
.nav a{color:var(--accent)}
"""


def region_drive_hours(region: str, cfg: dict) -> tuple[float | None, str]:
    """Drive time from Sydney CBD to the region centroid.

    No portal can filter on drive time, so it is resolved here instead and shown
    per region. That turns the third criterion from decoration into something
    that actually tells you which regions clear your limit.
    """
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


def build() -> str:
    cfg = load_config()
    now = datetime.now(timezone.utc)
    sections = []

    for key in cfg["profiles"]:
        p = profile_config(cfg, key)
        limit = float(p["max_drive_hours_from_sydney_cbd"])
        rows = []
        for region in p.get("target_regions", []):
            links = "".join(
                f'<a href="{esc(u)}" target="_blank" rel="noopener noreferrer" '
                f'title="Applies: {esc(f)}">{esc(n)}</a>'
                for n, u, f in portal_links(region, key, p)
            )
            hours, source = region_drive_hours(region, cfg)
            if hours is None:
                drive = '<span class="drive unk">drive time unknown</span>'
            else:
                over = hours > limit
                approx = " approx." if source == "estimated" else ""
                drive = (
                    f'<span class="drive {"over" if over else "ok"}">{hours} h to centre'
                    f'{approx}{" — over limit" if over else ""}</span>'
                )
            rows.append(
                f'<div class="region"><span class="name">{esc(region)}</span>'
                f"{drive}{links}</div>"
            )
        crit = (
            f"Up to ${p['budget_max_aud']:,} · land from {p['min_land_size_m2']:,} m² · "
            f"within {limit} h of Sydney CBD"
        )
        sections.append(
            f"<h2>{esc(p.get('label', key))}</h2>"
            f'<p class="crit">{esc(crit)}</p>{"".join(rows)}'
        )

    return f"""<!doctype html>
<html lang="en-AU"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>Quick search — NSW Property Watcher</title>
<link rel="stylesheet" href="assets/style.css"><style>{CSS}</style></head>
<body><div class="wrap">
<h1>Quick search</h1>
<p class="meta">Generated {now.strftime('%d %b %Y')} from config.yaml · every link opens
that portal already filtered to the criteria below</p>
<p class="nav"><a href="index.html">← back to the dashboard</a></p>
<div class="note"><strong>No setup, no keys, no waiting.</strong> Every link carries your
price cap and property type. <strong>Domain links also apply your land-size minimum</strong>;
realestate.com.au's land-size URL format is undocumented and could not be verified, so set
that one filter in their panel once and it sticks for the session. Hover a link to see exactly
which filters it applies. Neither portal can filter on drive time, so it is measured here per
region and flagged when a region is outside your limit. Those times are to the
<em>centre</em> of each region, so a large LGA can read as over the limit while its near
edge is well inside it — treat a flag as "check where in this region", not "rule it out".</div>
{''.join(sections)}
</div></body></html>
"""


def main() -> int:
    out = build()
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / "search.html").write_text(out, encoding="utf-8")
    log.info("wrote %s", DOCS_DIR / "search.html")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as exc:
        log.error("%s", exc)
        sys.exit(1)
