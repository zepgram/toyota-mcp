from __future__ import annotations

import json
import webbrowser
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import keyring
from keyring.errors import KeyringError
from pytoyoda.const import ACCESS_TOKEN_URL, AUTHORIZE_URL
from pytoyoda.controller import Controller, TokenInfo
from pytoyoda.exceptions import ToyotaLoginError

SERVICE = "toyota-mcp"
ACCOUNT = "session"
CLIENT_AUTHORIZATION = "basic b25lYXBwOm9uZWFwcA=="
CLIENT_ID = "oneapp"
REDIRECT_URI = "com.toyota.oneapp:/oauth2Callback"
CODE_VERIFIER = "plain"
EXPIRED = datetime.min.replace(tzinfo=UTC)

# pytoyoda requires an email-shaped username even when a refresh token makes it unused.
PLACEHOLDER_USERNAME = "session@toyota-mcp.invalid"
NO_CREDENTIALS = (
    "No saved session and no credentials. Run `toyota-mcp login` to sign in through your "
    "browser (the password stays with Toyota), or set TOYOTA_USERNAME and TOYOTA_PASSWORD."
)


@dataclass(frozen=True)
class Session:
    username: str | None
    refresh_token: str


class SessionStore:
    """The saved Toyota session, in the operating system's credential store."""

    def __init__(self, service: str = SERVICE, account: str = ACCOUNT) -> None:
        self._service = service
        self._account = account

    def load(self) -> Session | None:
        try:
            raw = keyring.get_password(self._service, self._account)
        except KeyringError:
            return None
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return Session(username=data.get("username"), refresh_token=data["refresh_token"])
        except (ValueError, KeyError):
            return None

    def save(self, session: Session) -> None:
        keyring.set_password(
            self._service,
            self._account,
            json.dumps({"username": session.username, "refresh_token": session.refresh_token}),
        )

    def clear(self) -> bool:
        try:
            keyring.delete_password(self._service, self._account)
        except KeyringError:
            return False
        return True


class SessionController(Controller):
    """A pytoyoda controller that signs in with a saved refresh token.

    pytoyoda refreshes whenever it holds a refresh token and only falls back to the
    password otherwise, so seeding an expired token entry authenticates without one.
    """

    store: SessionStore = SessionStore()

    async def login(self) -> None:
        if self._token_info is None and (session := self.store.load()) is not None:
            self._token_info = TokenInfo(
                access_token="",
                refresh_token=session.refresh_token,
                uuid="",
                expiration=EXPIRED,
            )
        if self._token_info is None and not self._password:
            raise ToyotaLoginError(NO_CREDENTIALS)
        await super().login()
        self._persist()

    def _persist(self) -> None:
        refresh_token = self._refresh_token
        if not refresh_token or self.store.load() is None:
            return
        self.store.save(Session(username=self._username or None, refresh_token=refresh_token))


def account_username(configured: str | None, store: SessionStore | None = None) -> str:
    if configured:
        return configured
    session = (store or SessionStore()).load()
    if session is not None and session.username:
        return session.username
    return PLACEHOLDER_USERNAME


def authorize_url() -> str:
    return str(AUTHORIZE_URL)


def open_browser(url: str) -> bool:
    try:
        return webbrowser.open(url)
    except webbrowser.Error:
        return False


def authorization_code(pasted: str) -> str:
    """Read the code out of the redirect the browser could not follow."""
    candidate = pasted.strip().strip("\"'")
    if not candidate:
        raise ValueError("nothing pasted")
    if "code=" in candidate:
        query = parse_qs(urlparse(candidate).query) or parse_qs(candidate.split("?", 1)[-1])
        codes = query.get("code")
        if not codes or not codes[0]:
            raise ValueError("that URL carries no authorization code")
        return codes[0]
    if "://" in candidate or candidate.startswith("com.toyota"):
        raise ValueError("that URL carries no authorization code")
    return candidate


async def exchange(code: str, client: httpx.AsyncClient | None = None) -> Session:
    owned = client is None
    http = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0))
    try:
        response = await http.post(
            str(ACCESS_TOKEN_URL),
            headers={"authorization": CLIENT_AUTHORIZATION},
            data={
                "client_id": CLIENT_ID,
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "grant_type": "authorization_code",
                "code_verifier": CODE_VERIFIER,
            },
        )
    except httpx.HTTPError as exc:
        raise ToyotaLoginError(f"Could not reach Toyota's token endpoint: {exc}") from exc
    finally:
        if owned:
            await http.aclose()
    if response.status_code != httpx.codes.OK:
        raise ToyotaLoginError(
            f"Toyota rejected the authorization code ({response.status_code}). "
            "Codes are single-use and short-lived — start `toyota-mcp login` again."
        )
    payload = response.json()
    if "refresh_token" not in payload:
        raise ToyotaLoginError("Toyota's answer carried no refresh token.")
    return Session(
        username=_username_from(payload.get("id_token")), refresh_token=payload["refresh_token"]
    )


def _username_from(id_token: str | None) -> str | None:
    if not id_token:
        return None
    try:
        claims: dict[str, Any] = jwt.decode(
            id_token,
            algorithms=["RS256"],
            options={"verify_signature": False},
            audience="oneappsdkclient",
        )
    except jwt.PyJWTError:
        return None
    for claim in ("email", "preferred_username", "sub"):
        value = claims.get(claim)
        if isinstance(value, str) and value:
            return value
    return None
