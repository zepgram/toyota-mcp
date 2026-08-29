from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

CONSENT_PATH = "/consent"
CODE_LIFETIME = 300
ACCESS_TOKEN_LIFETIME = 3600
PENDING_LIFETIME = 900
SCOPE = "vehicle"


def state_file() -> Path:
    """Where the issued grants live, following the platform's convention."""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "toyota-mcp" / "oauth.json"


@dataclass
class Grants:
    """Registered clients and issued tokens, kept across restarts."""

    path: Path
    clients: dict[str, dict[str, Any]] = field(default_factory=dict)
    refresh_tokens: dict[str, dict[str, Any]] = field(default_factory=dict)
    access_tokens: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Grants:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return cls(path=path)
        return cls(
            path=path,
            clients=data.get("clients", {}),
            refresh_tokens=data.get("refresh_tokens", {}),
            access_tokens=data.get("access_tokens", {}),
        )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "clients": self.clients,
                    "refresh_tokens": self.refresh_tokens,
                    "access_tokens": self.access_tokens,
                }
            )
        )
        temporary.chmod(0o600)
        temporary.replace(self.path)

    def forget_expired(self) -> None:
        now = time.time()
        self.access_tokens = {
            token: value
            for token, value in self.access_tokens.items()
            if (value.get("expires_at") or now + 1) > now
        }


@dataclass
class _Pending:
    client_id: str
    params: AuthorizationParams
    created_at: float
    signed_in: bool = False


class OwnerAuthorizationServer(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    """Issues MCP access tokens to whoever can sign in to the vehicle's Toyota account.

    Toyota has no third-party OAuth programme, so this server is its own
    authorization server — and signing in to Toyota is what proves ownership,
    which is also how the server obtains the session it needs.
    """

    def __init__(self, grants: Grants) -> None:
        self._grants = grants
        self._pending: dict[str, _Pending] = {}
        self._codes: dict[str, AuthorizationCode] = {}

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        raw = self._grants.clients.get(client_id)
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._grants.clients[client_info.client_id] = client_info.model_dump(mode="json")
        self._grants.save()

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        self._forget_stale_pending()
        request_id = secrets.token_urlsafe(16)
        self._pending[request_id] = _Pending(
            client_id=client.client_id, params=params, created_at=time.time()
        )
        return f"{CONSENT_PATH}?{urlencode({'request': request_id})}"

    def pending_client(self, request_id: str) -> str | None:
        """The display name of the client waiting for approval, if any."""
        pending = self._pending.get(request_id)
        if pending is None:
            return None
        registered = self._grants.clients.get(pending.client_id) or {}
        return str(registered.get("client_name") or pending.client_id)

    def mark_signed_in(self, request_id: str) -> None:
        pending = self._pending.get(request_id)
        if pending is not None:
            pending.signed_in = True

    def is_signed_in(self, request_id: str) -> bool:
        pending = self._pending.get(request_id)
        return bool(pending and pending.signed_in)

    def approve(self, request_id: str) -> str | None:
        """Return the client's redirect target once the Toyota sign-in is done."""
        pending = self._pending.get(request_id)
        if pending is None or not pending.signed_in:
            return None
        del self._pending[request_id]
        code = AuthorizationCode(
            code=secrets.token_urlsafe(32),
            scopes=pending.params.scopes or [SCOPE],
            expires_at=time.time() + CODE_LIFETIME,
            client_id=pending.client_id,
            code_challenge=pending.params.code_challenge,
            redirect_uri=pending.params.redirect_uri,
            redirect_uri_provided_explicitly=pending.params.redirect_uri_provided_explicitly,
            resource=pending.params.resource,
            subject="owner",
        )
        self._codes[code.code] = code
        query = {"code": code.code}
        if pending.params.state:
            query["state"] = pending.params.state
        separator = "&" if "?" in str(code.redirect_uri) else "?"
        return f"{code.redirect_uri}{separator}{urlencode(query)}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self._codes.get(authorization_code)
        if code is None or code.client_id != client.client_id:
            return None
        if code.expires_at < time.time():
            del self._codes[authorization_code]
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        self._codes.pop(authorization_code.code, None)
        return self._issue(
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            resource=authorization_code.resource,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        raw = self._grants.refresh_tokens.get(refresh_token)
        if raw is None or raw["client_id"] != client.client_id:
            return None
        return RefreshToken.model_validate(raw)

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        self._grants.refresh_tokens.pop(refresh_token.token, None)
        return self._issue(
            client_id=client.client_id,
            scopes=scopes or refresh_token.scopes,
            resource=None,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        raw = self._grants.access_tokens.get(token)
        if raw is None:
            return None
        access = AccessToken.model_validate(raw)
        if access.expires_at is not None and access.expires_at < time.time():
            self._grants.access_tokens.pop(token, None)
            self._grants.save()
            return None
        return access

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self._grants.access_tokens.pop(token.token, None)
        self._grants.refresh_tokens.pop(token.token, None)
        self._grants.save()

    def _issue(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        access = AccessToken(
            token=secrets.token_urlsafe(32),
            client_id=client_id,
            scopes=scopes,
            expires_at=int(time.time()) + ACCESS_TOKEN_LIFETIME,
            resource=resource,
            subject="owner",
        )
        refresh = RefreshToken(token=secrets.token_urlsafe(32), client_id=client_id, scopes=scopes)
        self._grants.forget_expired()
        self._grants.access_tokens[access.token] = access.model_dump(mode="json")
        self._grants.refresh_tokens[refresh.token] = refresh.model_dump(mode="json")
        self._grants.save()
        return OAuthToken(
            access_token=access.token,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_LIFETIME,
            scope=" ".join(scopes),
            refresh_token=refresh.token,
        )

    def _forget_stale_pending(self) -> None:
        cutoff = time.time() - PENDING_LIFETIME
        self._pending = {
            key: value for key, value in self._pending.items() if value.created_at > cutoff
        }


MARK = """<svg viewBox="0 0 64 40" role="img" aria-label="Vehicle" width="56" height="35">
 <ellipse cx="32" cy="20" rx="30" ry="18" fill="none" stroke="currentColor" stroke-width="3"/>
 <ellipse cx="32" cy="13" rx="11" ry="7" fill="none" stroke="currentColor" stroke-width="3"/>
 <ellipse cx="32" cy="24" rx="19" ry="8" fill="none" stroke="currentColor" stroke-width="3"
          transform="rotate(90 32 24)"/>
</svg>"""

LEGAL = """
 <hr>
 <footer>
  <p><strong>Nothing you type here is kept.</strong> The email address and password are sent
  to Toyota to obtain a session token, and are never written to disk, logged, or shared. Only
  that token is stored, so this server can talk to your vehicle without asking again; remove
  it at any time and the access is gone.</p>
  <p>Toyota, Lexus, MyToyota and MyLexus, together with their logos and marks, are the
  exclusive property of Toyota Motor Corporation and its affiliates. All rights reserved.
  This is an independent, unofficial tool: it is not affiliated with, endorsed by, sponsored
  by, or supported by Toyota, and the names are used only to say which service it works with.
  Toyota's own terms govern your account and your vehicle.</p>
  <p class="who">Served by <a href="https://github.com/zepgram/toyota-mcp" target="_blank"
   rel="noopener">toyota-mcp</a>, running on the machine you or its operator control.</p>
 </footer>
"""

PAGE = """<!doctype html>
<html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>{title} · toyota-mcp</title>
<style>
 :root {{ color-scheme: light dark;
   --bg: #f4f4f5; --card: #fff; --ink: #18181b; --muted: #52525b; --line: #e4e4e7;
   --accent: #18181b; --accent-ink: #fff; --danger: #b91c1c; --danger-bg: #fef2f2 }}
 @media (prefers-color-scheme: dark) {{ :root {{
   --bg: #09090b; --card: #18181b; --ink: #fafafa; --muted: #a1a1aa; --line: #27272a;
   --accent: #fafafa; --accent-ink: #18181b; --danger: #fca5a5; --danger-bg: #2a1215 }} }}
 * {{ box-sizing: border-box }}
 body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 1.5rem;
   background: var(--bg); color: var(--ink);
   font: 15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif }}
 main {{ width: min(30rem, 100%); background: var(--card); border: 1px solid var(--line);
   border-radius: 16px; padding: 2rem; box-shadow: 0 1px 2px rgba(0,0,0,.06) }}
 .mark {{ color: var(--ink); opacity: .9; margin-bottom: 1rem }}
 h1 {{ font-size: 1.375rem; line-height: 1.3; margin: 0 0 .5rem; letter-spacing: -.01em }}
 .intro {{ margin: 0 0 1.5rem; color: var(--muted) }}
 label.field {{ display: block; margin-bottom: .75rem }}
 label.field span {{ display: block; font-size: .8125rem; font-weight: 600; margin-bottom: .3rem }}
 input[type=text], input[type=email], input[type=password] {{ width: 100%; padding: .7rem .8rem;
   font: inherit; color: var(--ink); background: var(--bg);
   border: 1px solid var(--line); border-radius: 10px }}
 input:focus {{ outline: 2px solid var(--accent); outline-offset: 1px }}
 button {{ width: 100%; margin-top: .5rem; padding: .8rem; font: inherit; font-weight: 600;
   border: 0; border-radius: 10px; background: var(--accent); color: var(--accent-ink);
   cursor: pointer }}
 button:hover {{ opacity: .9 }}
 .choice {{ display: flex; gap: .75rem; align-items: center; padding: .8rem;
   border: 1px solid var(--line); border-radius: 10px; margin-bottom: .5rem; cursor: pointer }}
 .choice:hover {{ border-color: var(--accent) }}
 .choice strong {{ display: block }}
 .choice small {{ color: var(--muted) }}
 .error {{ padding: .75rem .9rem; margin-bottom: 1rem; border-radius: 10px;
   background: var(--danger-bg); color: var(--danger); font-size: .9375rem }}
 hr {{ border: 0; border-top: 1px solid var(--line); margin: 1.75rem 0 1rem }}
 footer {{ font-size: .75rem; line-height: 1.55; color: var(--muted) }}
 footer p {{ margin: 0 0 .6rem }}
 footer a {{ color: inherit }}
 .who {{ opacity: .8 }}
</style>
<body>
<main>
 <div class="mark">{mark}</div>
 <h1>{title}</h1>
 <p class="intro">{intro}</p>
 {error}
 {body}
 {legal}
</main>
</body></html>
"""
ERROR = '<p class="error">{message}</p>'

SIGN_IN_BODY = """
 <form method="post">
  <label class="field"><span>MyToyota email address</span>
   <input type="email" name="username" autocomplete="username" autofocus required></label>
  <label class="field"><span>Password</span>
   <input type="password" name="password" autocomplete="current-password" required></label>
  <button type="submit">Sign in and continue</button>
 </form>
"""

VEHICLE_BODY = """
 <form method="post">
  {choices}
  <button type="submit">Allow access</button>
 </form>
"""
VEHICLE_CHOICE = """
  <label class="choice"><input type="radio" name="vin" value="{vin}" {checked} required>
   <span><strong>{name}</strong><small>{model} · VIN …{suffix}</small></span></label>
"""
