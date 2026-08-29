from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest
from pytoyoda.controller import Controller

from toyota_mcp.config import Settings
from toyota_mcp.gateway import VehicleGateway
from toyota_mcp.opendata import FrenchOpenData

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

LOLA_VIN = "JTDZARBE0RJ000042"
BAN_LABEL = "12 Rue de l'Exemple 31000 Toulouse"
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
        return httpx.Response(200, json={"results": FUEL_RECORDS})


def load_responses() -> dict[str, Any]:
    return {
        path: json.loads((FIXTURES / filename).read_text()) for path, filename in ROUTES.items()
    }


COMMAND_PATH = "/v1/global/remote/command"
CLIMATE_CONTROL_PATH = "/v2/remote/climate-control"
ACCEPTED = {
    "status": {"messages": [{"responseCode": "CTP-GENERIC-20001", "description": "Success"}]}
}


class FakeControllerBase(Controller):
    responses: ClassVar[dict[str, Any]]
    calls: ClassVar[list[str]]
    commands: ClassVar[list[tuple[str, dict[str, Any]]]]
    commands_take_effect: ClassVar[bool]
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
            cls.commands.append((path, body or {}))
            if cls.commands_take_effect:
                _apply_command(cls.responses, path, body or {})
            return copy.deepcopy(ACCEPTED)
        return copy.deepcopy(cls.responses[path])


def _apply_command(responses: dict[str, Any], path: str, body: dict[str, Any]) -> None:
    if path == COMMAND_PATH and body.get("command") in ("door-lock", "door-unlock"):
        state = "locked" if body["command"] == "door-lock" else "unlocked"
        doors = responses["/v1/vehicle/status"]["payload"]["doors"]
        for door in doors.values():
            if "lockStatus" in door:
                door["lockStatus"]["status"] = state
    if path == CLIMATE_CONTROL_PATH:
        status = "running" if body.get("command") == "start" else "stopped"
        responses["/v1/vehicle/climate-status"]["payload"]["status"] = status


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
    settings = Settings(
        username="user@example.com", password="secret", use_metric=True, remote_commands=True
    )
    return VehicleGateway(
        settings,
        controller_class=fake_controller_class,
        command_poll_interval=0,
        command_timeout=0,
    )


@pytest.fixture
def opendata_stub() -> OpenDataStub:
    return OpenDataStub()


@pytest.fixture
def opendata(opendata_stub: OpenDataStub) -> FrenchOpenData:
    return FrenchOpenData(transport=httpx.MockTransport(opendata_stub.handler))
