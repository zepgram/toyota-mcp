from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from keyring.errors import NoKeyringError
from pytoyoda.controller import TokenInfo
from pytoyoda.exceptions import ToyotaLoginError

from toyota_mcp import login
from toyota_mcp.session import (
    EXPIRED,
    Session,
    SessionController,
    SessionStore,
    authorization_code,
    exchange,
)

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


@pytest.mark.parametrize(
    ("pasted", "expected"),
    [
        ("com.toyota.oneapp:/oauth2Callback?code=ABC&iss=x", "ABC"),
        ("  ABC  ", "ABC"),
        ('"com.toyota.oneapp:/oauth2Callback?code=ABC"', "ABC"),
    ],
)
def test_authorization_code_accepts_url_or_bare_code(pasted: str, expected: str) -> None:
    assert authorization_code(pasted) == expected


@pytest.mark.parametrize("pasted", ["", "   ", "com.toyota.oneapp:/oauth2Callback?error=denied"])
def test_authorization_code_rejects_useless_input(pasted: str) -> None:
    with pytest.raises(ValueError, match=r"code|nothing"):
        authorization_code(pasted)


async def test_exchange_returns_the_session_with_the_account_email() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        seen["body"] = dict(httpx.QueryParams(request.content.decode()))
        return httpx.Response(
            200,
            json={
                "access_token": "a",
                "refresh_token": REFRESH,
                "expires_in": 3600,
                "id_token": _id_token(email="driver@example.com"),
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        session = await exchange("THE-CODE", client)

    assert session == Session(username="driver@example.com", refresh_token=REFRESH)
    assert seen["body"]["code"] == "THE-CODE"
    assert seen["body"]["grant_type"] == "authorization_code"
    assert seen["body"]["code_verifier"] == "plain"
    assert seen["headers"]["authorization"].startswith("basic ")


async def test_exchange_reports_a_spent_code() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(400, json={"error": "invalid_grant"})
        )
    ) as client:
        with pytest.raises(ToyotaLoginError, match="single-use"):
            await exchange("STALE", client)


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
    with pytest.raises(ToyotaLoginError, match="toyota_sign_in"):
        await Fake("", "", brand="T").login()


def test_login_command_saves_the_session(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("toyota_mcp.login.open_browser", lambda _: True)
    monkeypatch.setattr(
        "toyota_mcp.login.exchange",
        lambda code: _coroutine(Session(username="driver@example.com", refresh_token=REFRESH)),
    )
    code = login.run(store, prompt=lambda _: "com.toyota.oneapp:/oauth2Callback?code=ABC")
    assert code == login.EXIT_OK
    assert store.load() == Session(username="driver@example.com", refresh_token=REFRESH)
    out = capsys.readouterr().out
    assert "driver@example.com" in out
    assert "password was never seen" in out


def test_login_command_prints_the_url_when_no_browser_opens(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("toyota_mcp.login.open_browser", lambda _: False)
    login.run(store, prompt=lambda _: "")
    assert "b2c-login.toyota-europe.com" in capsys.readouterr().out


def test_login_command_is_cancellable(store: SessionStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("toyota_mcp.login.open_browser", lambda _: True)

    def interrupted(_: str) -> str:
        raise KeyboardInterrupt

    assert login.run(store, prompt=interrupted) == login.EXIT_CANCELLED
    assert store.load() is None


def test_logout_command(store: SessionStore, capsys: pytest.CaptureFixture[str]) -> None:
    store.save(Session(username=None, refresh_token=REFRESH))
    assert login.logout(store) == login.EXIT_OK
    assert store.load() is None
    assert login.logout(store) == login.EXIT_OK
    assert "no saved session" in capsys.readouterr().out


async def _coroutine(value: Session) -> Session:
    return value
