from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, ClassVar

import pytest
from pytoyoda.controller import Controller

from toyota_mcp.config import Settings
from toyota_mcp.gateway import VehicleGateway

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
}

LOLA_VIN = "JTDZARBE0RJ000042"


def load_responses() -> dict[str, Any]:
    return {
        path: json.loads((FIXTURES / filename).read_text()) for path, filename in ROUTES.items()
    }


class FakeControllerBase(Controller):
    responses: ClassVar[dict[str, Any]]
    calls: ClassVar[list[str]]
    failure: ClassVar[Exception | None]
    login_failure: ClassVar[Exception | None]
    login_count: ClassVar[int]

    async def login(self) -> None:
        cls = type(self)
        cls.login_count += 1
        if cls.login_failure is not None:
            raise cls.login_failure

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
        path = endpoint.split("?")[0]
        cls.calls.append(path)
        if cls.failure is not None:
            raise cls.failure
        return copy.deepcopy(cls.responses[path])


@pytest.fixture
def fake_controller_class() -> type[FakeControllerBase]:
    return type(
        "FakeController",
        (FakeControllerBase,),
        {
            "responses": load_responses(),
            "calls": [],
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
