"""Auth profiles: bearer, basic, api_key, oauth2 client credentials, or none.

Secrets come from ``${ENV:NAME}``; they are masked in every result. OAuth tokens are cached
for the run.
"""

from __future__ import annotations

import base64
import time

import httpx

from .values import Vars

_cache: dict[str, tuple[str, float]] = {}


def headers(profile_name: str | None, profiles: dict, v: Vars, timeout: float = 20) -> dict[str, str]:
    if not profile_name or profile_name == "none":
        return {}
    if profile_name not in profiles:
        if profile_name == "default":
            return {}
        raise ValueError(f"Unknown auth profile '{profile_name}'. Known: {sorted(profiles) or ['(none)']}")
    p = v.sub(profiles[profile_name])
    kind = p.get("type", "bearer")
    if kind == "bearer":
        return {"Authorization": f"Bearer {p['token']}"}
    if kind == "basic":
        raw = f"{p['username']}:{p['password']}".encode()
        token = base64.b64encode(raw).decode()
        v.secrets.add(token)
        return {"Authorization": f"Basic {token}"}
    if kind == "api_key":
        return {p.get("header", "X-API-Key"): str(p["value"])}
    if kind in ("oauth2", "oauth2_client_credentials"):
        key = f"{p['token_url']}|{p['client_id']}|{p.get('scope', '')}"
        tok, exp = _cache.get(key, ("", 0.0))
        if not tok or exp < time.time() + 30:
            data = {"grant_type": "client_credentials", "client_id": p["client_id"], "client_secret": p["client_secret"]}
            if p.get("scope"):
                data["scope"] = p["scope"]
            if p.get("audience"):
                data["audience"] = p["audience"]
            r = httpx.post(p["token_url"], data=data, timeout=timeout)
            if r.status_code >= 400:
                raise ValueError(f"OAuth token request failed with HTTP {r.status_code}")
            body = r.json()
            tok = body["access_token"]
            _cache[key] = (tok, time.time() + float(body.get("expires_in", 300)))
        v.secrets.add(tok)
        return {"Authorization": f"Bearer {tok}"}
    raise ValueError(f"Unknown auth type '{kind}' (bearer, basic, api_key, oauth2)")
