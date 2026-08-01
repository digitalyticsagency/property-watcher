# NSW Property Watcher

On-demand property search across NSW for **two independent buyer profiles**,
published as a static dashboard on GitHub Pages. Runs entirely on GitHub Actions —
nothing needs to stay on your machine.

| Profile | What it looks for |
|---|---|
| `lifestyle_acreage` | Larger rural/lifestyle blocks within a longer drive of Sydney |
| `house_with_granny_flat` | Houses with an existing or feasible secondary dwelling, closer in |

The two profiles are kept **completely separate** end to end — separate queries,
filters, scores, AI commentary, and dashboard sections. Nothing is ever merged
into a single combined list.

---

## How listings are found

Search-based discovery, plus optional Domain API, each tagged so the dashboard can show provenance.

| Path | Source | Verified? |
|---|---|---|
| **C** | Google Custom Search JSON API — discovery across the *wider* NSW property web (allhomes, ratemyagent, regional agency sites), then a polite fetch of each listing page | ⚠️ Only if the page fetch succeeded |
| **B** | Domain Developer API (optional, off by default) | ✅ Yes |

### What this deliberately does not do

- **Never scrapes a Google or Bing results page.** Discovery goes through the
  official Custom Search JSON API, which returns structured JSON.
- **Never scrapes realestate.com.au or domain.com.au on a schedule.** Those
  portals are reached only via Domain's own developer API (Path B), if enabled.
- **Never invents listing detail.** When the Path C page fetch fails — robots.txt
  disallows it, a 403, a JS-only shell — the listing is kept with just its search
  title/snippet/URL and badged **⚠ UNVERIFIED — open link to confirm** on the
  dashboard. It is never silently dropped, and never filled in with guesses.
- **Never promises a granny flat is permissible.** See below.

### Granny-flat assessment

For `house_with_granny_flat` only, `zoning_check.py` reasons about NSW secondary
dwelling rules — SEPP (Housing) 2021 Ch.3 Pt.5 permits a secondary dwelling with
consent in R1–R5, RU1–RU6 and C4, capped at the greater of 60 m² or 5% of the
lot, with the complying-development pathway wanting roughly a 450 m² minimum lot.

Every listing gets one of three statuses, with the reasoning shown on the card:

- **confirmed** — the (verified) listing states an existing secondary dwelling.
- **likely** — the zone permits it and the lot is big enough.
- **unclear — check with council** — everything else.

This is automated reasoning, **not planning advice**. Zone codes alone don't
settle it: the LEP for the specific lot can impose its own minimum lot size, and
heritage/flood/bushfire/sewer overlays can defeat an otherwise-permissible
proposal. Always confirm with the council and the NSW Planning Portal.

---

## Quick search — works with no setup at all

`docs/search.html` turns your criteria into pre-filtered links for every region,
and lets you retune them in the browser: price, minimum block size, bedrooms, and
whether to hide regions past your drive-time limit. Links rebuild as you move the
controls; nothing is saved and nothing re-runs, so trying a bigger budget costs
nothing. Drive times are measured once at build time via OSRM.

This needs no API key and no workflow run — it is the fastest way to actually
look at listings.

---

## Setup

### 0. Install dependencies first

Everything below runs from the **repo root** (`cd ~/property-watcher`) — `PYTHONPATH=src`
is a relative path and won't resolve from anywhere else. macOS has no bare `python`,
so use the venv's interpreter (or `python3`):

```bash
cd ~/property-watcher
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

With the venv activated, `python` works for the rest of this README. Without it,
substitute `python3` everywhere.

### 1. Fill in `config.yaml` — required

The repo ships with `FILL_ME` placeholders and zero budgets for the values that
must not be guessed. **`src/common.py` validates the config and exits non-zero if
any placeholder remains**, so a half-configured repo fails loudly instead of
publishing a dashboard built on invented criteria.

You must set, for **both** profiles:

- `target_regions`
- `budget_min_aud` / `budget_max_aud`
- `min_land_size_m2`

…plus a real contact address in `geocoding.nominatim_user_agent` (OpenStreetMap's
usage policy requires one).

Check it before pushing (from the repo root, venv activated):

```bash
PYTHONPATH=src python -c "import common; common.load_config(); print('config OK')"
```

Until you've filled it in, that command **exits 1 and lists every value still
missing** — that's the guard doing its job, not a broken setup. You want it to
print `config OK`.

### 2. Google Custom Search (Path C)

1. **Programmable Search Engine** → <https://programmablesearchengine.google.com/>
   → Add. Under *Sites to search*, add each domain from `search_api.sites` in
   `config.yaml` (realestate.com.au, domain.com.au, allhomes.com.au,
   ratemyagent.com.au, onthehouse.com.au, property.com.au, realestateview.com.au,
   homely.com.au). Copy the **Search engine ID** → `GOOGLE_SEARCH_ENGINE_ID`.
2. **Custom Search JSON API key** → <https://developers.google.com/custom-search/v1/introduction>
   → *Get a Key* → copy → `GOOGLE_SEARCH_API_KEY`.

Free tier is **100 queries/day**. `search_api.max_queries_per_profile` is set to
24, so a run uses ~48 queries — comfortably inside the free tier.

### 3. Anthropic API key

<https://console.anthropic.com/> → API keys → `ANTHROPIC_API_KEY`.
The model is set in `config.yaml` (`claude-opus-5` by default) and is used only
for scoring, tagging and writing commentary.

### 4. Domain API (optional)

Only if you enable `path_b_domain_api` in `data_sources`. Register at
<https://developer.domain.com.au/> → `DOMAIN_API_KEY`.

---

## Secrets to add before the workflow can run

**Settings → Secrets and variables → Actions → New repository secret**

| Secret | Required? | Used by |
|---|---|---|
| `ANTHROPIC_API_KEY` | ✅ Yes | `rank_and_filter.py` |
| `GOOGLE_SEARCH_API_KEY` | ✅ Yes | `fetch_search_api.py` |
| `GOOGLE_SEARCH_ENGINE_ID` | ✅ Yes | `fetch_search_api.py` |
| `DOMAIN_API_KEY` | Optional | `fetch_domain_api.py` (only if Path B enabled) |

No key, token, or secret is ever written to source or committed. `.gitignore`
blocks `client_secret*.json`, `token.json`, and `.env*`.

---

## Enable GitHub Pages

**Settings → Pages → Build and deployment → Source: Deploy from a branch →
Branch: `main`, folder: `/docs`.**

⚠️ **This repo is public** (your choice), so note: `config.yaml` contains your
budget, target regions, and criteria in plain text, and the Pages URL is
reachable by anyone who has it. If you'd rather not publish those, either move
the repo to private (Pages from a private repo needs a paid GitHub plan) or move
the sensitive values into secrets and read them from the environment.

---

## First run — test before trusting the schedule

Don't wait for Sunday. Trigger it manually:

**Actions → Weekly refresh → Run workflow**

Then check the published dashboard for all of:

- [ ] Both profiles appear, with separate listings and separate scores
- [ ] All five tabs render for each profile
- [ ] Search-discovered listings that couldn't be fetched carry the amber
      **⚠ UNVERIFIED** badge and left rule, visually distinct from confirmed ones
- [ ] Granny-flat cards show confirmed / likely / unclear with reasoning

Only once that looks right should you re-enable the weekly cron (commented out in the workflow).

---

## Schedule

The weekly cron is **commented out** — runs are on demand via
**Actions → Weekly refresh → Run workflow**. To re-enable it, uncomment the
`schedule:` block in the workflow. It was set to `0 20 * * 6` (UTC) — **20:00 Saturday UTC**.

GitHub cron is UTC-only and does not follow daylight saving. Sydney is UTC+10
(AEST) in winter and UTC+11 (AEDT) from the first Sunday in October to the first
Sunday in April, so this fires at **06:00 Sunday AEST in winter and 07:00 Sunday
AEDT in summer**. That one-hour drift is harmless for a weekly digest; if you
need exactly 6am year-round, add a second cron at `0 19 * * 6` and gate the job
on the Sydney local hour.

---

## Pipeline

```
fetch_search_api.py    ─┐
                        ├─→ geocode_distance.py ─→ zoning_check.py ─→ rank_and_filter.py ─→ build_dashboard.py
fetch_domain_api.py   ─┘   (merge + drive time)   (granny flat)      (filters + AI)        (docs/index.html)
```

Stages hand off through `data/_work/<profile>/<stage>.json` (gitignored).
Persistent state is committed:

- `data/<profile>/seen_listings.json` — per-profile, per-URL dedupe across runs
- `data/<profile>/archive/all_listings.json` — all-time archive per profile

Dedupe is by canonical URL (scheme/host/path, query and fragment stripped), so
the same property arriving from both an email alert and the search API collapses
to one card — keeping whichever copy is verified and has more detail.

**Any step failing fails the whole run** (`bash -euo pipefail`, plus an explicit
check that `docs/index.html` was produced and contains profile data). A stale or
empty dashboard is never published.

---

## Running locally

Run every command from the repo root with the venv from step 0 activated
(`cd ~/property-watcher && source .venv/bin/activate`).

```bash
export ANTHROPIC_API_KEY=... GOOGLE_SEARCH_API_KEY=... GOOGLE_SEARCH_ENGINE_ID=...

export PYTHONPATH=src
python src/fetch_search_api.py
python src/fetch_domain_api.py
python src/geocode_distance.py
python src/zoning_check.py
python src/rank_and_filter.py
python src/build_dashboard.py
open docs/index.html
```

---

## Cost

| Service | Weekly cost |
|---|---|
| GitHub Actions (public repo) | Free |
| GitHub Pages | Free |
| Google Custom Search JSON API | Free (~48 of 100 daily queries) |
| Nominatim geocoding + OSRM routing | Free (results cached in `data/cache/`) |
| Anthropic API | A few cents per run, scaling with listing count |
