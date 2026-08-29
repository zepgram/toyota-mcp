from __future__ import annotations

import contextlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import keyring
from keyring.errors import KeyringError
from pytoyoda.controller import Controller, TokenInfo
from pytoyoda.exceptions import ToyotaLoginError

SERVICE = "toyota-mcp"
ACCOUNT = "session"
EXPIRED = datetime.min.replace(tzinfo=UTC)

# pytoyoda requires an email-shaped username even when a refresh token makes it unused.
PLACEHOLDER_USERNAME = "session@toyota-mcp.invalid"
NOT_SIGNED_IN = (
    "This server is not connected to a Toyota account yet. Over HTTP, connecting a client "
    "signs in; otherwise run `toyota-mcp login`."
)


@dataclass(frozen=True)
class Session:
    username: str | None
    refresh_token: str
    vin: str | None = None


class SessionStore:
    """The saved Toyota session.

    Kept in the operating system's credential store, or in a file when there is
    none — a container or a headless server has no keyring.
    """

    def __init__(
        self, service: str = SERVICE, account: str = ACCOUNT, file: Path | None = None
    ) -> None:
        self._service = service
        self._account = account
        self._file = file or _configured_file()

    @property
    def location(self) -> str:
        return str(self._file) if self._file else f"the {self._service} credential store"

    def load(self) -> Session | None:
        raw = self._read_file() if self._file else self._read_keyring()
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return Session(
                username=data.get("username"),
                refresh_token=data["refresh_token"],
                vin=data.get("vin"),
            )
        except (ValueError, KeyError):
            return None

    def save(self, session: Session) -> None:
        raw = json.dumps(
            {
                "username": session.username,
                "refresh_token": session.refresh_token,
                "vin": session.vin,
            }
        )
        if self._file is None:
            try:
                keyring.set_password(self._service, self._account, raw)
                return
            except KeyringError:
                self._file = session_file()
        self._write_file(raw)

    def clear(self) -> bool:
        if self._file is not None:
            try:
                self._file.unlink()
            except OSError:
                return False
            return True
        try:
            keyring.delete_password(self._service, self._account)
        except KeyringError:
            return False
        return True

    def _read_keyring(self) -> str | None:
        try:
            return keyring.get_password(self._service, self._account)
        except KeyringError:
            self._file = session_file()
            return self._read_file()

    def _read_file(self) -> str | None:
        try:
            return self._file.read_text() if self._file else None
        except OSError:
            return None

    def _write_file(self, raw: str) -> None:
        assert self._file is not None
        self._file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._file.with_suffix(".tmp")
        temporary.write_text(raw)
        temporary.chmod(0o600)
        temporary.replace(self._file)


def session_file() -> Path:
    from toyota_mcp.oauth import state_file

    return state_file().with_name("session.json")


def _configured_file() -> Path | None:
    configured = os.environ.get("TOYOTA_SESSION_FILE")
    return Path(configured) if configured else None


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
            raise ToyotaLoginError(NOT_SIGNED_IN)
        await super().login()
        self._persist()

    def _persist(self) -> None:
        refresh_token = self._refresh_token
        saved = self.store.load()
        if not refresh_token or saved is None:
            return
        self.store.save(
            Session(username=saved.username, refresh_token=refresh_token, vin=saved.vin)
        )


def account_username(configured: str | None, store: SessionStore | None = None) -> str:
    if configured:
        return configured
    session = (store or SessionStore()).load()
    if session is not None and session.username:
        return session.username
    return PLACEHOLDER_USERNAME


async def sign_in(username: str, password: str, brand: str = "T") -> Session:
    """Authenticate with Toyota and keep only the refresh token it hands back.

    Toyota's web login ends on a mobile deep link a browser cannot follow — a
    phone opens the MyToyota app instead — so the credentials are posted to
    Toyota from here. The password is never written anywhere.
    """
    if not username.strip() or not password:
        raise ToyotaLoginError("Both the MyToyota email address and the password are needed.")
    controller = Controller(username.strip(), password, brand=brand)
    # pytoyoda caches tokens per username on the class: without this, a wrong password
    # would silently succeed whenever that account already authenticated in this process.
    controller._token_info = None
    try:
        await controller.login()
        refresh_token = controller._refresh_token
    finally:
        with contextlib.suppress(Exception):
            await controller.aclose()
    if not refresh_token:
        raise ToyotaLoginError("Toyota accepted the sign-in but returned no refresh token.")
    return Session(username=username.strip(), refresh_token=refresh_token)
