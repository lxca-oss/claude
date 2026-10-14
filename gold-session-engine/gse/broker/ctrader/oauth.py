"""cTrader Open API OAuth2 (authorization code flow).

1. Register an application at https://openapi.ctrader.com/apps and add the redirect URI
   (default ``http://localhost:8050/oauth/callback``, served by the terminal).
2. The terminal sends you to the authorization URL; after you log in with your cTrader
   ID and pick the accounts, cTrader redirects back with ``?code=...``.
3. The code is exchanged for an access token (valid ~30 days) and a refresh token; the
   bot refreshes automatically before expiry.
"""

from __future__ import annotations

import time
from urllib.parse import urlencode

import httpx

AUTH_URL = "https://openapi.ctrader.com/apps/auth"
TOKEN_URL = "https://openapi.ctrader.com/apps/token"


def authorize_url(client_id: str, redirect_uri: str, scope: str = "trading") -> str:
    return f"{AUTH_URL}?{urlencode({'client_id': client_id, 'redirect_uri': redirect_uri, 'scope': scope})}"


class OAuthError(Exception):
    pass


def normalize_tokens(data: dict) -> dict:
    """Accept both the camelCase and snake_case token responses."""
    err = data.get("errorCode") or data.get("error")
    if err:
        raise OAuthError(f"{err}: {data.get('description') or data.get('error_description') or ''}".strip())
    access = data.get("accessToken") or data.get("access_token")
    if not access:
        raise OAuthError(f"no access token in response: {data}")
    expires_in = data.get("expiresIn") or data.get("expires_in") or 2_592_000
    return {
        "access_token": access,
        "refresh_token": data.get("refreshToken") or data.get("refresh_token"),
        "token_expires_at": int(time.time() + int(expires_in)),
    }


async def exchange_code(client_id: str, client_secret: str, code: str, redirect_uri: str) -> dict:
    params = {
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
        "client_id": client_id, "client_secret": client_secret,
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(TOKEN_URL, params=params, headers={"Accept": "application/json"})
    try:
        return normalize_tokens(r.json())
    except ValueError as exc:
        raise OAuthError(f"token endpoint returned HTTP {r.status_code}: {r.text[:200]}") from exc


async def refresh_tokens(client_id: str, client_secret: str, refresh_token: str) -> dict:
    params = {
        "grant_type": "refresh_token", "refresh_token": refresh_token,
        "client_id": client_id, "client_secret": client_secret,
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(TOKEN_URL, params=params, headers={"Accept": "application/json"})
    try:
        return normalize_tokens(r.json())
    except ValueError as exc:
        raise OAuthError(f"token endpoint returned HTTP {r.status_code}: {r.text[:200]}") from exc
