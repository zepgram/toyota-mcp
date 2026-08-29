from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any

import jwt
import pytest
from keyring.errors import NoKeyringError
from pytoyoda.controller import TokenInfo
from pytoyoda.exceptions import ToyotaLoginError

from toyota_mcp import login
from toyota_mcp.session import EXPIRED, Session, SessionController, SessionStore, sign_in

REFRESH = "refresh-token-value"


class FakeKeyring:
    def __init__(self) -> None:
        self.entries: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, account: str) -> str | None:
        return self.entries.get((service, account))

    def set_password(self, service: str, account: str, value: str) -> None:
        self.entries[(service, account)] = value

    def delete_password(self, service: str, account: str) -> None:
        from keyring.errors import PasswordDeleteError

        if (service, account) not in self.entries:
            raise PasswordDeleteError(account)
        del self.entries[(service, account)]


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> SessionStore:
    fake = FakeKeyring()
    monkeypatch.setattr("toyota_mcp.session.keyring", fake)
    return SessionStore()


def _id_token(**claims: Any) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return jwt.encode({"uuid": "u-1", **claims}, "a" * 32, algorithm="HS256")


def test_a_file_store_round_trips_and_is_private(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    store = SessionStore(file=path)
    assert store.load() is None
    store.save(Session(username="driver@example.com", refresh_token=REFRESH))
    assert oct(path.stat().st_mode)[-3:] == "600"
    assert SessionStore(file=path).load() == Session("driver@example.com", REFRESH)
    assert store.clear() is True
    assert store.clear() is False


def test_a_headless_machine_falls_back_to_a_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class NoKeyring:
        def get_password(self, *_: str) -> str:
            raise NoKeyringError

        def set_password(self, *_: str) -> None:
            raise NoKeyringError

    monkeypatch.setattr("toyota_mcp.session.keyring", NoKeyring())
    monkeypatch.setattr("toyota_mcp.session.session_file", lambda: tmp_path / "session.json")
    store = SessionStore()
    assert store.load() is None
    store.save(Session(username=None, refresh_token=REFRESH))
    assert (tmp_path / "session.json").exists()
    assert store.load() == Session(None, REFRESH)


def test_the_session_file_can_be_pointed_at_explicitly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TOYOTA_SESSION_FILE", str(tmp_path / "elsewhere.json"))
    store = SessionStore()
    store.save(Session(username=None, refresh_token=REFRESH))
    assert (tmp_path / "elsewhere.json").exists()
    assert store.location.endswith("elsewhere.json")


def test_store_round_trip_and_clear(store: SessionStore) -> None:
    assert store.load() is None
    store.save(Session(username="driver@example.com", refresh_token=REFRESH))
    loaded = store.load()
    assert loaded == Session(username="driver@example.com", refresh_token=REFRESH)
    assert store.clear() is True
    assert store.load() is None
    assert store.clear() is False


def test_store_ignores_corrupted_entries(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.json"
    path.write_text(json.dumps({"nope": 1}))
    assert SessionStore(file=path).load() is None
    path.write_text("not json at all")
    assert SessionStore(file=path).load() is None


async def test_sign_in_keeps_only_the_refresh_token(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str] = {}

    class FakeController:
        def __init__(self, username: str, password: str, brand: str = "T") -> None:
            seen["username"], seen["password"], seen["brand"] = username, password, brand
            self._refresh_token = REFRESH

        async def login(self) -> None:
            return None

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr("toyota_mcp.session.Controller", FakeController)
    session = await sign_in("  driver@example.com  ", "hunter2")
    assert session == Session(username="driver@example.com", refresh_token=REFRESH)
    assert seen == {"username": "driver@example.com", "password": "hunter2", "brand": "T"}


@pytest.mark.parametrize(("username", "password"), [("", "pw"), ("a@b.c", ""), ("   ", "pw")])
async def test_sign_in_needs_both_fields(username: str, password: str) -> None:
    with pytest.raises(ToyotaLoginError, match="email address and the password"):
        await sign_in(username, password)


async def test_sign_in_never_reuses_a_cached_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wrong password must fail even when that account already signed in in this process."""
    attempted: list[str] = []

    class Cached:
        def __init__(self, username: str, password: str, brand: str = "T") -> None:
            self._password = password
            self._token_info = "a token from an earlier sign-in"
            self._refresh_token = REFRESH

        async def login(self) -> None:
            if self._token_info is not None:
                return  # what pytoyoda does when a cached token is still valid
            attempted.append(self._password)
            if self._password != "hunter2":
                raise ToyotaLoginError("Authentication Failed. 401, denied.")

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr("toyota_mcp.session.Controller", Cached)
    with pytest.raises(ToyotaLoginError, match="denied"):
        await sign_in("driver@example.com", "wrong-password")
    assert attempted == ["wrong-password"]


async def test_sign_in_reports_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    class Refusing:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def login(self) -> None:
            raise ToyotaLoginError("Authentication Failed. 401, denied.")

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr("toyota_mcp.session.Controller", Refusing)
    with pytest.raises(ToyotaLoginError, match="denied"):
        await sign_in("driver@example.com", "wrong")


async def test_controller_signs_in_from_the_saved_session(store: SessionStore) -> None:
    refreshed: list[str] = []

    class Fake(SessionController):
        async def _refresh_tokens(self) -> None:
            refreshed.append(self._refresh_token or "")
            self._token_info = TokenInfo(
                access_token="new", refresh_token="rotated", uuid="u", expiration=EXPIRED.max
            )

        async def _authenticate(self) -> None:
            raise AssertionError("must not fall back to the password")

    Fake.store = store
    store.save(Session(username="driver@example.com", refresh_token=REFRESH))
    controller = Fake("", "", brand="T")
    await controller.login()

    assert refreshed == [REFRESH]
    assert store.load() == Session(username="driver@example.com", refresh_token="rotated", vin=None)


async def test_controller_without_session_or_password_says_what_to_do(store: SessionStore) -> None:
    class Fake(SessionController):
        async def _authenticate(self) -> None:
            raise AssertionError("must not try")

    Fake.store = store
    with pytest.raises(ToyotaLoginError, match="toyota-mcp login"):
        await Fake("", "", brand="T").login()


def test_login_command_saves_the_session(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "toyota_mcp.login.sign_in",
        lambda username, password: _coroutine(Session(username, REFRESH)),
    )
    code = login.run(store, ask=lambda _: "driver@example.com", ask_secret=lambda _: "hunter2")
    assert code == login.EXIT_OK
    assert store.load() == Session("driver@example.com", REFRESH)
    out = capsys.readouterr().out
    assert "Signed in as driver@example.com" in out
    assert "not written anywhere" in out


def test_login_command_reports_a_refusal(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(username: str, password: str) -> object:
        raise ToyotaLoginError("Authentication Failed. 401, denied.")

    monkeypatch.setattr("toyota_mcp.login.sign_in", refuse)
    assert login.run(store, ask=lambda _: "a@b.c", ask_secret=lambda _: "x") == login.EXIT_AUTH
    assert "refused the sign-in" in capsys.readouterr().out
    assert store.load() is None


def test_login_command_is_cancellable(store: SessionStore) -> None:
    def interrupted(_: str) -> str:
        raise KeyboardInterrupt

    assert login.run(store, ask=interrupted) == login.EXIT_CANCELLED
    assert store.load() is None


def test_logout_command(store: SessionStore, capsys: pytest.CaptureFixture[str]) -> None:
    store.save(Session(username=None, refresh_token=REFRESH))
    assert login.logout(store) == login.EXIT_OK
    assert store.load() is None
    assert login.logout(store) == login.EXIT_OK
    assert "no saved session" in capsys.readouterr().out


async def _coroutine(value: Session) -> Session:
    return value
