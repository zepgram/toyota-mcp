from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest
from pytoyoda.controller import Controller
from pytoyoda.exceptions import ToyotaApiError

from toyota_mcp.config import Settings
from toyota_mcp.gateway import VehicleGateway
from toyota_mcp.opendata import OpenData

FIXTURES = Path(__file__).parent / "fixtures"

ROUTES = {
    "/v2/vehicle/guid": "vehicle_guid.json",
    "/v3/telemetry": "telemetry.json",
    "/v1/location": "location.json",
    "/v1/vehicle/status": "remote_status.json",
    "/v1/vehiclehealth/status": "vehicle_health.json",
    "/v2/notification/history": "notifications.json",
    "/v1/servicehistory/vehicle/summary": "service_history.json",
    "/v1/trips": "trips.json",
    "/v1/vehicle/climate-settings": "climate_settings.json",
    "/v1/vehicle/climate-status": "climate_status.json",
}

SAMPLE_VIN = "JTDZARBE0RJ000042"
BAN_LABEL = "12 Rue de l'Exemple 31000 Toulouse"
OSM_LABEL = "12, Rue de l'Exemple, Toulouse, Haute-Garonne, Occitanie, France"
FUEL_RECORDS = [
    {
        "adresse": "Route de l'Exemple",
        "cp": "31700",
        "ville": "Blagnac",
        "e10_prix": 1.985,
        "e10_maj": "2026-08-29T08:29:00+00:00",
        "gazole_prix": 2.159,
        "horaires_automate_24_24": "Non",
        "carburants_disponibles": ["Gazole", "E10", "SP98"],
        "geom": {"lat": 52.17, "lon": 0.14},
    },
    {
        "adresse": "Avenue de l'Exemple",
        "cp": "31000",
        "ville": "Toulouse",
        "e10_prix": 2.01,
        "e10_maj": "2026-08-29T06:00:00+00:00",
        "gazole_prix": None,
        "horaires_automate_24_24": "Oui",
        "carburants_disponibles": ["E10"],
        "geom": {"lat": 52.20, "lon": 0.10},
    },
]


class OpenDataStub:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status_code = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status_code != 200:
            return httpx.Response(self.status_code, json={"message": "boom"})
        if request.url.host == "api-adresse.data.gouv.fr":
            return httpx.Response(200, json={"features": [{"properties": {"label": BAN_LABEL}}]})
        if request.url.host == "nominatim.openstreetmap.org":
            return httpx.Response(200, json={"display_name": OSM_LABEL})
        return httpx.Response(200, json={"results": FUEL_RECORDS})


def load_responses() -> dict[str, Any]:
    return {
        path: json.loads((FIXTURES / filename).read_text()) for path, filename in ROUTES.items()
    }


COMMAND_PATH = "/v1/global/remote/command"
CLIMATE_CONTROL_PATH = "/v2/remote/climate-control"
STATUS_PATH = "/v1/vehicle/status"
CLIMATE_STATUS_PATH = "/v1/vehicle/climate-status"
UNSUPPORTED = (
    'Request Failed. 400, {"status":{"messages":[{"responseCode":"CTP-REMOTE-40041",'
    '"description":"Vehicle not supported"}]}}'
)
ACCEPTED = {
    "status": {"messages": [{"responseCode": "CTP-GENERIC-20001", "description": "Success"}]}
}


class FakeControllerBase(Controller):
    responses: ClassVar[dict[str, Any]]
    calls: ClassVar[list[str]]
    commands: ClassVar[list[tuple[str, dict[str, Any]]]]
    commands_take_effect: ClassVar[bool]
    effect_delay: ClassVar[int]
    fail_reads_after_command: ClassVar[int]
    climate_return_code: ClassVar[str]
    unsupported_commands: ClassVar[set[str]]
    pending: ClassVar[tuple[str, dict[str, Any], int] | None]
    failure: ClassVar[Exception | None]
    login_failure: ClassVar[Exception | None]
    login_count: ClassVar[int]

    async def login(self) -> None:
        cls = self._state()
        cls.login_count += 1
        if cls.login_failure is not None:
            raise cls.login_failure

    def _state(self) -> type[FakeControllerBase]:
        return next(klass for klass in type(self).__mro__ if "login_count" in vars(klass))

    async def request_json(
        self,
        method: str,
        endpoint: str,
        vin: str | None = None,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        cls = self._state()
        path = endpoint.split("?")[0]
        cls.calls.append(path)
        if cls.failure is not None:
            raise cls.failure
        if method == "POST":
            if path == COMMAND_PATH and (body or {}).get("command") in cls.unsupported_commands:
                raise ToyotaApiError(UNSUPPORTED)
            if path in (COMMAND_PATH, CLIMATE_CONTROL_PATH):
                cls.commands.append((path, body or {}))
                if cls.commands_take_effect:
                    cls.pending = (path, body or {}, cls.effect_delay)
            response: dict[str, Any] = copy.deepcopy(ACCEPTED)
            response["payload"] = {
                "returnCode": cls.climate_return_code if path == CLIMATE_CONTROL_PATH else "000000"
            }
            return response
        if cls.commands and cls.fail_reads_after_command > 0:
            cls.fail_reads_after_command -= 1
            raise ToyotaApiError("Request Failed. 429, slow down.")
        if cls.pending is not None and path in (STATUS_PATH, CLIMATE_STATUS_PATH):
            pending_path, pending_body, delay = cls.pending
            if delay <= 0:
                _apply_command(cls.responses, pending_path, pending_body)
                cls.pending = None
            else:
                cls.pending = (pending_path, pending_body, delay - 1)
        return copy.deepcopy(cls.responses[path])


def _apply_command(responses: dict[str, Any], path: str, body: dict[str, Any]) -> None:
    if path == CLIMATE_CONTROL_PATH:
        status = "running" if body.get("command") == "start" else "stopped"
        responses[CLIMATE_STATUS_PATH]["payload"]["status"] = status
        return
    command = body.get("command")
    reported_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    payload = responses[STATUS_PATH]["payload"]
    payload["lastUpdateTimestamp"] = reported_at
    if command in ("door-lock", "door-unlock"):
        state = "locked" if command == "door-lock" else "unlocked"
        for name, door in payload["doors"].items():
            if "lockStatus" in door and name != "rearBack":
                door["lockStatus"] = {"status": state, "lastUpdateTimestamp": reported_at}
    if command in ("trunk-lock", "trunk-unlock"):
        state = "locked" if command == "trunk-lock" else "unlocked"
        payload["doors"]["rearBack"]["lockStatus"] = {
            "status": state,
            "lastUpdateTimestamp": reported_at,
        }
    if command == "power-window-close":
        for window in payload["windows"].values():
            window.update({"status": "close", "lastUpdateTimestamp": reported_at})


@pytest.fixture
def fake_controller_class() -> type[FakeControllerBase]:
    return type(
        "FakeController",
        (FakeControllerBase,),
        {
            "responses": load_responses(),
            "calls": [],
            "commands": [],
            "commands_take_effect": True,
            "effect_delay": 0,
            "fail_reads_after_command": 0,
            "climate_return_code": "000000",
            "unsupported_commands": set(),
            "pending": None,
            "failure": None,
            "login_failure": None,
            "login_count": 0,
        },
    )


@pytest.fixture
def settings() -> Settings:
    return Settings(
        username="user@example.com",
        password="secret",
        vin=None,
        brand="T",
        use_metric=True,
    )


@pytest.fixture
def gateway(settings: Settings, fake_controller_class: type[FakeControllerBase]) -> VehicleGateway:
    return VehicleGateway(settings, controller_class=fake_controller_class)


@pytest.fixture
def command_gateway(
    fake_controller_class: type[FakeControllerBase],
) -> VehicleGateway:
    return VehicleGateway(
        Settings(username="user@example.com", password="secret", use_metric=True),
        controller_class=fake_controller_class,
        command_poll_interval=0,
        command_timeout=0.2,
    )


@pytest.fixture
def opendata_stub() -> OpenDataStub:
    return OpenDataStub()


@pytest.fixture
def opendata(opendata_stub: OpenDataStub) -> OpenData:
    return OpenData("fr", transport=httpx.MockTransport(opendata_stub.handler))
