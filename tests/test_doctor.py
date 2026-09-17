from __future__ import annotations

from typing import Any

import pytest

from toyota_mcp import doctor
from toyota_mcp.doctor import EXIT_NO_VEHICLE


class FakeController:
    """Controller stub whose raw response is what the test wants to prove."""

    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.calls: list[str] = []

    async def request_json(self, method: str, endpoint: str, **_: Any) -> dict[str, Any]:
        self.calls.append(endpoint)
        return {"payload": self.payload}


class FakeApi:
    def __init__(self, controller: FakeController) -> None:
        self.controller = controller


class FakeClient:
    """A client that signs in fine and parses no vehicle, whatever Toyota returned."""

    def __init__(self, payload: Any) -> None:
        self._api = FakeApi(FakeController(payload))

    async def login(self) -> None:
        return None

    async def get_vehicles(self) -> list[Any]:
        return []

    async def aclose(self) -> None:
        return None


@pytest.fixture
def signed_in(monkeypatch: pytest.MonkeyPatch, isolated_session: Any) -> None:
    monkeypatch.setenv("TOYOTA_USERNAME", "user@example.com")
    monkeypatch.setenv("TOYOTA_PASSWORD", "secret")
    monkeypatch.delenv("TOYOTA_VIN", raising=False)


def _run_with(monkeypatch: pytest.MonkeyPatch, payload: Any) -> None:
    client = FakeClient(payload)
    monkeypatch.setattr(doctor, "MyT", lambda **_: client)


def test_empty_account_points_at_the_mobile_app(
    signed_in: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _run_with(monkeypatch, [])
    assert doctor.run() == EXIT_NO_VEHICLE
    assert "no vehicles on this account" in capsys.readouterr().out


def test_unparsed_vehicles_point_at_the_library_not_the_account(
    signed_in: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Toyota answers with two cars and pytoyoda yields none: the account is not the problem.
    _run_with(monkeypatch, [{"vin": "A"}, {"vin": "B"}])
    assert doctor.run() == EXIT_NO_VEHICLE
    out = capsys.readouterr().out
    assert "Toyota returned 2 vehicle(s) but pytoyoda parsed none" in out
    assert "no vehicles on this account" not in out


def test_a_failing_probe_falls_back_to_the_account_message(
    signed_in: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = FakeClient([])

    async def boom(*_: Any, **__: Any) -> dict[str, Any]:
        raise RuntimeError("network down")

    monkeypatch.setattr(client._api.controller, "request_json", boom)
    monkeypatch.setattr(doctor, "MyT", lambda **_: client)
    assert doctor.run() == EXIT_NO_VEHICLE
    assert "no vehicles on this account" in capsys.readouterr().out
