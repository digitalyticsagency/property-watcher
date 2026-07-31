"""Local diagnostic: test the Google Custom Search key + engine ID directly.

Takes GitHub Actions out of the picture so you can tell whether the credentials
themselves work. The key is read from the environment and never printed.

    export GOOGLE_SEARCH_API_KEY='...'
    export GOOGLE_SEARCH_ENGINE_ID='...'
    PYTHONPATH=src python src/check_search_key.py
"""

from __future__ import annotations

import json
import os
import sys

import requests

ENDPOINT = "https://www.googleapis.com/customsearch/v1"


def main() -> int:
    key = os.environ.get("GOOGLE_SEARCH_API_KEY", "").strip()
    cx = os.environ.get("GOOGLE_SEARCH_ENGINE_ID", "").strip()

    if not key or not cx:
        print("Set both GOOGLE_SEARCH_API_KEY and GOOGLE_SEARCH_ENGINE_ID first.")
        return 2

    # Show only enough to confirm you exported what you think you did.
    print(f"key:  {key[:6]}...{key[-4:]}  (length {len(key)})")
    print(f"cx:   {cx}")
    print(f"      key looks like a Google API key: {key.startswith('AIza')}")
    print()

    resp = requests.get(
        ENDPOINT, params={"key": key, "cx": cx, "q": "granny flat Wilton NSW"}, timeout=30
    )
    print(f"HTTP {resp.status_code}")

    if resp.status_code == 200:
        items = resp.json().get("items") or []
        print(f"✅ WORKING — {len(items)} results")
        for it in items[:3]:
            print(f"   · {it.get('title', '')[:80]}")
            print(f"     {it.get('link', '')}")
        return 0

    try:
        err = resp.json().get("error", {})
    except ValueError:
        print(resp.text[:500])
        return 1

    reason = (err.get("errors") or [{}])[0].get("reason", "")
    print(f"❌ {err.get('status', '')} / reason={reason}")
    print(f"   {err.get('message', '')}")
    print()

    hints = {
        "accessNotConfigured": "The Custom Search API is not enabled on this key's project. "
                               "The message above names the project — enable it there.",
        "forbidden": "Usually one of: (a) the key has API restrictions that exclude Custom "
                     "Search API, (b) the key has an Application restriction (HTTP referrer "
                     "or IP) that blocks server-side calls, or (c) the key belongs to a "
                     "project without Custom Search access. Check the key's Edit page in "
                     "Cloud Console: Application restrictions should be 'None', and API "
                     "restrictions either 'Don't restrict key' or including 'Custom Search API'.",
        "keyInvalid": "The key string itself is wrong — re-copy it from Cloud Console.",
        "dailyLimitExceeded": "Free quota (100/day) is used up. Wait for the reset or enable billing.",
        "rateLimitExceeded": "Too many requests too quickly — retry shortly.",
    }
    print("Likely cause:", hints.get(reason, "See the raw response below."))
    print()
    print(json.dumps(err, indent=2)[:800])
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
