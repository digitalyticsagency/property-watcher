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

from common import DOCS_DIR, PipelineError, load_config, log, profile_config


def esc(v: object) -> str:
    return html.escape(str(v if v is not None else ""), quote=True)


def slug(region: str) -> str:
    return quote(region.lower().replace(" ", "-"))


def portal_links(region: str, profile: str, p: dict) -> list[tuple[str, str]]:
    """Deep links that land on a filtered result list, not a blank search form.

    Deliberately conservative URL shapes. Richer filters (land size, property
    type) are expressible in each portal's URL grammar, but that grammar is
    undocumented and changes — and I can't verify it automatically, because both
    portals return 403/429 to any non-browser request. A link that lands on the
    right suburb with the right price cap and needs one extra click is strictly
    better than a clever link that 404s. An allhomes.com.au pattern was dropped
    for exactly that reason: it was verified returning HTTP 404.
    """
    budget = p["budget_max_aud"]
    return [
        (
            "realestate.com.au",
            f"https://www.realestate.com.au/buy/in-{slug(region)},+nsw/list-1"
            f"?maxPrice={budget}",
        ),
        (
            "domain.com.au",
            f"https://www.domain.com.au/sale/{slug(region)}-nsw/?price=0-{budget}",
        ),
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


def build() -> str:
    cfg = load_config()
    now = datetime.now(timezone.utc)
    sections = []

    for key in cfg["profiles"]:
        p = profile_config(cfg, key)
        rows = []
        for region in p.get("target_regions", []):
            links = "".join(
                f'<a href="{esc(u)}" target="_blank" rel="noopener noreferrer">{esc(n)}</a>'
                for n, u in portal_links(region, key, p)
            )
            rows.append(
                f'<div class="region"><span class="name">{esc(region)}</span>{links}</div>'
            )
        crit = (
            f"Up to ${p['budget_max_aud']:,} · land from {p['min_land_size_m2']:,} m² · "
            f"within {p['max_drive_hours_from_sydney_cbd']} h of Sydney CBD"
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
<div class="note"><strong>No setup, no keys, no waiting.</strong> Each link opens a live,
pre-filtered result list on that portal, scoped to the suburb and your price cap.
Bookmark this page and click through whenever you want to look. Land size and property
type are <em>not</em> pre-applied — the portals' URL formats for those are undocumented
and break easily, so set them once in the portal's own filter panel and it will
remember them for that session.</div>
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
