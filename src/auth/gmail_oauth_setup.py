"""ONE-TIME local script: obtain a Gmail refresh token for the GitHub Action.

Run this on your own machine, once. It opens a browser for consent and prints a
refresh token. Store ONLY that token as a GitHub secret — the Action never runs
an interactive flow (constraint #6), and no credential is ever written to source.

    python src/auth/gmail_oauth_setup.py --client-secret ~/Downloads/client_secret.json

Prerequisites (Google Cloud Console, free):
  1. Create a project.
  2. APIs & Services → Library → enable "Gmail API".
  3. APIs & Services → OAuth consent screen → External → add yourself as a Test user.
  4. Credentials → Create credentials → OAuth client ID → Desktop app → download JSON.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--client-secret",
        required=True,
        type=Path,
        help="Path to the OAuth client JSON downloaded from Google Cloud Console",
    )
    args = parser.parse_args()

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print(
            "Missing dependency. Run:\n"
            "  pip install google-auth-oauthlib google-api-python-client",
            file=sys.stderr,
        )
        return 1

    if not args.client_secret.exists():
        print(f"No such file: {args.client_secret}", file=sys.stderr)
        return 1

    with args.client_secret.open(encoding="utf-8") as fh:
        client_config = json.load(fh)
    installed = client_config.get("installed") or client_config.get("web") or {}
    client_id = installed.get("client_id", "")
    client_secret = installed.get("client_secret", "")

    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    # access_type=offline + prompt=consent is what actually returns a refresh token;
    # without prompt=consent Google omits it on repeat authorisations.
    creds = flow.run_local_server(
        port=0, access_type="offline", prompt="consent"
    )

    if not creds.refresh_token:
        print(
            "Google did not return a refresh token. Revoke this app's access at\n"
            "https://myaccount.google.com/permissions and run this script again.",
            file=sys.stderr,
        )
        return 1

    print("\n" + "=" * 72)
    print("Add these three as GitHub repository secrets:")
    print("  Settings → Secrets and variables → Actions → New repository secret")
    print("=" * 72)
    print(f"GMAIL_CLIENT_ID       = {client_id}")
    print(f"GMAIL_CLIENT_SECRET   = {client_secret}")
    print(f"GMAIL_REFRESH_TOKEN   = {creds.refresh_token}")
    print("=" * 72)
    print("Do not commit these. The refresh token grants read access to your inbox.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
