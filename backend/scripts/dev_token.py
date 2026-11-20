"""LOCAL DEVELOPMENT ONLY: sign in to local Supabase Auth and print an access token for the API docs page.

docker compose -f infra/docker-compose.dev.yml exec api python -m scripts.dev_token you@example.com password
"""

from __future__ import annotations

import os
import sys

import httpx


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: python -m scripts.dev_token <email> <password>", file=sys.stderr)
        return 2
    if os.environ.get("GAZI_ENVIRONMENT") != "development":
        print("dev_token only runs when GAZI_ENVIRONMENT=development", file=sys.stderr)
        return 2
    gotrue = os.environ["GAZI_GOTRUE_URL"].rstrip("/")
    anon_key = os.environ.get("GAZI_DEV_ANON_KEY", "")
    if not anon_key:
        print("Set GAZI_DEV_ANON_KEY in infra/.env (the anon key from `supabase status`).", file=sys.stderr)
        return 2
    response = httpx.post(
        f"{gotrue}/token",
        params={"grant_type": "password"},
        json={"email": sys.argv[1], "password": sys.argv[2]},
        headers={"apikey": anon_key},
        timeout=10,
    )
    if response.status_code != 200:
        print(f"Sign-in failed ({response.status_code}): {response.text}", file=sys.stderr)
        return 1
    print(response.json()["access_token"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
