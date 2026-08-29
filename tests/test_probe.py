from __future__ import annotations

import copy
import json
from typing import Any, ClassVar

import pytest
from pytoyoda.controller import Controller
from pytoyoda.exceptions import ToyotaApiError

from tests.conftest import ACCEPTED, SAMPLE_VIN, load_responses
from toyota_mcp import probe
from toyota_mcp.doctor import EXIT_CONFIG, EXIT_NO_VEHICLE, EXIT_OK

REJECTION = (
    'Request Failed. 400, {"responseCode":"CTP-REMOTE-40006",'
    '"description":"Missing/Invalid remote command request"}'
)


class ProbeController(Controller):
    responses: ClassVar[dict[str, Any]]
    requests: ClassVar[list[tuple[str, str, dict[str, Any] | None]]]
    rejected: ClassVar[set[str]]

    async def request_json(
        self,
        method: str,
        endpoint: str,
        vin: str | None = None,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        cls = type(self)
        cls.requests.append((method, endpoint, body))
        if method == "POST":
            if endpoint == probe.COMMAND_PATH and (body or {}).get("command") in cls.rejected:
                raise ToyotaApiError(REJECTION)
            return copy.deepcopy(ACCEPTED) | {"payload": {"returnCode": "000000"}}
        return copy.deepcopy(cls.responses[endpoint])

    async def aclose(self) -> None:
        return None


@pytest.fixture
def controller_class(monkeypatch: pytest.MonkeyPatch) -> type[ProbeController]:
    monkeypatch.setenv("TOYOTA_USERNAME", "user@example.com")
    monkeypatch.setenv("TOYOTA_PASSWORD", "secret")
    monkeypatch.delenv("TOYOTA_VIN", raising=False)
    return type(
        "FakeProbeController",
        (ProbeController,),
        {"responses": load_responses(), "requests": [], "rejected": {"find-vehicle"}},
    )


def _posted(controller_class: type[ProbeController]) -> list[tuple[str, dict[str, Any] | None]]:
    return [(path, body) for method, path, body in controller_class.requests if method == "POST"]


def test_accepted_command_prints_return_code(
    controller_class: type[ProbeController], capsys: pytest.CaptureFixture[str]
) -> None:
    assert probe.run(["headlight-on"], controller_class) == EXIT_OK
    out = capsys.readouterr().out
    assert "ACCEPTED {'command': 'headlight-on'}: returnCode=000000" in out
    assert _posted(controller_class) == [(probe.COMMAND_PATH, {"command": "headlight-on"})]


def test_rejected_command_prints_toyota_error_without_vin(
    controller_class: type[ProbeController], capsys: pytest.CaptureFixture[str]
) -> None:
    assert probe.run(["find-vehicle"], controller_class) == probe.EXIT_REJECTED
    out = capsys.readouterr().out
    assert "REJECTED {'command': 'find-vehicle'}" in out
    assert "40006" in out
    assert SAMPLE_VIN not in out


def test_beeps_are_sent_only_when_requested(controller_class: type[ProbeController]) -> None:
    probe.run(["sound-horn", "--beeps", "2"], controller_class)
    assert _posted(controller_class) == [
        (probe.COMMAND_PATH, {"command": "sound-horn", "beepCount": 2})
    ]


def test_watch_wakes_the_car_and_polls_the_block(
    controller_class: type[ProbeController], capsys: pytest.CaptureFixture[str]
) -> None:
    code = probe.run(
        ["headlight-on", "--watch", "lights", "--watch-seconds", "10"],
        controller_class,
        poll_interval=0,
    )
    assert code == EXIT_OK
    assert (probe.WAKE_PATH, None) in _posted(controller_class)
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("t+")]
    assert len(lines) == 1
    assert json.loads(lines[0].split("lights: ", 1)[1])["head"]["status"] == "off"


def test_two_vehicles_need_a_vin(
    controller_class: type[ProbeController], capsys: pytest.CaptureFixture[str]
) -> None:
    second = copy.deepcopy(controller_class.responses[probe.GUID_PATH]["payload"][0])
    second["vin"] = "JTDZARBE0RJ000099"
    controller_class.responses[probe.GUID_PATH]["payload"].append(second)
    assert probe.run(["headlight-on"], controller_class) == EXIT_NO_VEHICLE
    assert "TOYOTA_VIN" in capsys.readouterr().out
    assert _posted(controller_class) == []


def test_missing_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("TOYOTA_USERNAME", raising=False)
    monkeypatch.delenv("TOYOTA_PASSWORD", raising=False)
    monkeypatch.chdir("/")
    assert probe.run(["headlight-on"]) == EXIT_CONFIG
    assert "CONFIG" in capsys.readouterr().out
