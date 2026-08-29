from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any, ClassVar

from pydantic import ValidationError
from pytoyoda.client import MyT
from pytoyoda.controller import Controller
from pytoyoda.exceptions import ToyotaLoginError
from pytoyoda.models.vehicle import Vehicle

from toyota_mcp.config import Settings
from toyota_mcp.gateway import vehicle_label

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_AUTH = 3
EXIT_NO_VEHICLE = 4
EXIT_API = 5

DUMP_DIR = Path("toyota-mcp-doctor-dump")

_SENSITIVE_KEY_PARTS = (
    "vin",
    "lat",
    "lon",
    "guid",
    "contract",
    "account",
    "subscription",
    "phone",
    "email",
    "address",
    "imei",
    "katashiki",
)
_VIN_PATTERN = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")


class RecordingController(Controller):
    captured: ClassVar[dict[str, Any]] = {}

    async def request_json(
        self,
        method: str,
        endpoint: str,
        vin: str | None = None,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = await super().request_json(
            method, endpoint, vin=vin, body=body, params=params, headers=headers
        )
        type(self).captured[endpoint.split("?")[0]] = payload
        return payload


def run(dump: bool = False) -> int:
    try:
        settings = Settings()
    except ValidationError as exc:
        for error in exc.errors():
            variable = "TOYOTA_" + "_".join(str(part) for part in error["loc"]).upper()
            print(f"CONFIG  {variable}: {error['msg']}")
        return EXIT_CONFIG
    return asyncio.run(_diagnose(settings, dump))


async def _diagnose(settings: Settings, dump: bool) -> int:
    RecordingController.captured.clear()
    client = MyT(
        username=settings.username,
        password=settings.password.get_secret_value(),
        use_metric=settings.use_metric,
        brand=settings.brand,
        controller_class=RecordingController if dump else Controller,
    )
    try:
        started = time.perf_counter()
        try:
            await client.login()
        except ToyotaLoginError as exc:
            print(f"AUTH    login failed: {exc}")
            print("AUTH    check TOYOTA_USERNAME / TOYOTA_PASSWORD; MFA accounts are unsupported.")
            return EXIT_AUTH
        except Exception as exc:
            print(f"API     login unreachable ({type(exc).__name__}): {exc}")
            return EXIT_API
        print(f"OK      login succeeded in {time.perf_counter() - started:.1f}s")

        try:
            vehicles = [vehicle for vehicle in await client.get_vehicles() if vehicle is not None]
        except Exception as exc:
            print(f"API     vehicle list failed ({type(exc).__name__}): {exc}")
            return EXIT_API
        if not vehicles:
            print("VEHICLE no vehicles on this account — check the MyToyota mobile app.")
            return EXIT_NO_VEHICLE
        for vehicle in vehicles:
            print(
                f"OK      vehicle: {vehicle_label(vehicle)} — "
                f"{vehicle.type or 'unknown powertrain'}"
            )

        selected = _pick(vehicles, settings.vin)
        if selected is None:
            print("VEHICLE TOYOTA_VIN does not match any vehicle on this account.")
            return EXIT_NO_VEHICLE

        started = time.perf_counter()
        try:
            await selected.update()
        except Exception as exc:
            print(f"API     data fetch failed ({type(exc).__name__}): {exc}")
            return EXIT_API
        print(f"OK      full data fetch in {time.perf_counter() - started:.1f}s")
        _print_tool_table(selected, settings)

        if dump:
            _write_dump(RecordingController.captured)
        return EXIT_OK
    finally:
        await client.aclose()


def _pick(vehicles: list[Vehicle[Any]], vin: str | None) -> Vehicle[Any] | None:
    if vin is None:
        return vehicles[0]
    for vehicle in vehicles:
        if vehicle.vin == vin:
            return vehicle
    return None


def _print_tool_table(vehicle: Vehicle[Any], settings: Settings) -> None:
    dashboard = vehicle.dashboard
    climate = vehicle.climate_settings
    rows = [
        ("toyota_get_vehicle_info", True, ""),
        (
            "toyota_get_status",
            vehicle.lock_status is not None and vehicle.lock_status.doors is not None,
            "remote status not supported",
        ),
        (
            "toyota_get_energy",
            dashboard is not None and dashboard.fuel_level is not None,
            "telemetry not supported",
        ),
        (
            "toyota_get_odometer",
            dashboard is not None and dashboard.odometer_with_unit is not None,
            "telemetry not supported",
        ),
        (
            "toyota_get_location",
            vehicle.location is not None and vehicle.location.latitude is not None,
            "no parked location reported yet",
        ),
        ("toyota_get_health", True, ""),
        ("toyota_get_last_trip / trips / trip_summary", True, "verified on first call"),
        (
            "toyota_get_climate",
            climate is not None and climate.temperature is not None,
            "remote climate not supported",
        ),
        (
            "toyota_find_fuel_stations",
            settings.open_data == "fr",
            "set TOYOTA_OPEN_DATA=fr (French fuel prices)",
        ),
        ("toyota_refresh_data", True, ""),
        (
            "toyota_lock_doors / unlock_doors",
            settings.remote_commands,
            "set TOYOTA_REMOTE_COMMANDS=true (opt-in)",
        ),
        (
            "toyota_start_climate / stop_climate",
            settings.remote_commands and climate is not None and climate.temperature is not None,
            "TOYOTA_REMOTE_COMMANDS=true and a remote-climate-enabled vehicle",
        ),
    ]
    for tool, available, reason in rows:
        marker = "OK     " if available else "MISSING"
        suffix = "" if available else f" — {reason}"
        print(f"{marker} {tool}{suffix}")
    if vehicle.type == "full_hybrid":
        print(
            "NOTE    battery/charging data: not applicable on a self-charging full hybrid; "
            "EV share is available in the trip tools."
        )


def _write_dump(captured: dict[str, Any]) -> None:
    DUMP_DIR.mkdir(exist_ok=True)
    for endpoint, payload in captured.items():
        slug = endpoint.strip("/").replace("/", "_") or "root"
        path = DUMP_DIR / f"{slug}.json"
        path.write_text(json.dumps(_redact(payload), indent=2, default=str))
        print(f"DUMP    {path}")
    print(
        "DUMP    payloads are recursively redacted, but review them manually "
        "before sharing or committing."
    )


def _redact(node: Any) -> Any:
    if isinstance(node, dict):
        return {key: _redact_value(key, value) for key, value in node.items()}
    if isinstance(node, list):
        return [_redact(item) for item in node]
    if isinstance(node, str):
        return _VIN_PATTERN.sub("REDACTED_VIN", node)
    return node


def _redact_value(key: str, value: Any) -> Any:
    lowered = key.lower()
    if any(part in lowered for part in _SENSITIVE_KEY_PARTS):
        return "REDACTED"
    return _redact(value)
