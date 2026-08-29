from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
from typing import Any, Literal

from pydantic import ValidationError
from pytoyoda.controller import Controller
from pytoyoda.exceptions import ToyotaApiError, ToyotaLoginError

from toyota_mcp.config import Settings
from toyota_mcp.doctor import EXIT_API, EXIT_AUTH, EXIT_CONFIG, EXIT_NO_VEHICLE, EXIT_OK

EXIT_REJECTED = 6

GUID_PATH = "/v2/vehicle/guid"
COMMAND_PATH = "/v1/global/remote/command"
WAKE_PATH = "/v1/remote/status"
STATUS_PATH = "/v1/vehicle/status"

Watch = Literal["lights", "doors"]
WATCHABLE: tuple[Watch, ...] = ("lights", "doors")
POLL_INTERVAL = 5.0


def run(
    arguments: list[str],
    controller_class: type[Controller] = Controller,
    poll_interval: float = POLL_INTERVAL,
) -> int:
    args = _parser().parse_args(arguments)
    try:
        settings = Settings()
    except ValidationError as exc:
        for error in exc.errors():
            variable = "TOYOTA_" + "_".join(str(part) for part in error["loc"]).upper()
            print(f"CONFIG  {variable}: {error['msg']}")
        return EXIT_CONFIG
    return asyncio.run(
        _probe(
            settings,
            controller_class,
            command=args.command,
            beeps=args.beeps,
            watch=args.watch,
            watch_seconds=args.watch_seconds,
            poll_interval=poll_interval,
        )
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="toyota-mcp probe",
        description=(
            "Send one raw remote command to the vehicle and print Toyota's answer. "
            "Every command is physical: run it next to the car."
        ),
    )
    parser.add_argument("command", help="raw command string, e.g. headlight-on or find-vehicle")
    parser.add_argument("--beeps", type=int, default=0, help="beepCount to send (omitted when 0)")
    parser.add_argument("--watch", choices=WATCHABLE, help="poll this status block afterwards")
    parser.add_argument("--watch-seconds", type=int, default=20, help="how long to poll")
    return parser


async def _probe(
    settings: Settings,
    controller_class: type[Controller],
    *,
    command: str,
    beeps: int,
    watch: Watch | None,
    watch_seconds: int,
    poll_interval: float,
) -> int:
    controller = controller_class(
        settings.username, settings.password.get_secret_value(), brand=settings.brand
    )
    try:
        try:
            vehicles = (await controller.request_json("GET", GUID_PATH)).get("payload") or []
        except ToyotaLoginError as exc:
            print(f"AUTH    login failed: {exc}")
            return EXIT_AUTH
        except ToyotaApiError as exc:
            print(f"API     vehicle list failed: {exc}")
            return EXIT_API
        vin = _select_vin(vehicles, settings.vin)
        if vin is None:
            print("VEHICLE no vehicle matched — set TOYOTA_VIN when several share the account.")
            return EXIT_NO_VEHICLE
        body: dict[str, Any] = {"command": command}
        if beeps:
            body["beepCount"] = beeps
        try:
            response = await controller.request_json("POST", COMMAND_PATH, vin=vin, body=body)
        except ToyotaApiError as exc:
            print(f"REJECTED {body}: {str(exc).replace(vin, 'VIN')}")
            return EXIT_REJECTED
        payload = response.get("payload") or {}
        messages = (response.get("status") or {}).get("messages") or [{}]
        print(
            f"ACCEPTED {body}: returnCode={payload.get('returnCode')} "
            f"({messages[0].get('description')})"
        )
        if watch is not None:
            await _watch(controller, vin, watch, watch_seconds, poll_interval)
        return EXIT_OK
    finally:
        await controller.aclose()


def _select_vin(vehicles: list[dict[str, Any]], wanted: str | None) -> str | None:
    vins = [str(vehicle["vin"]) for vehicle in vehicles if vehicle.get("vin")]
    if wanted is not None:
        return wanted if wanted in vins else None
    return vins[0] if len(vins) == 1 else None


async def _watch(
    controller: Controller, vin: str, block: Watch, seconds: int, interval: float
) -> None:
    with contextlib.suppress(ToyotaApiError):
        await controller.request_json("POST", WAKE_PATH, vin=vin)
    elapsed = 0.0
    while elapsed < seconds:
        await asyncio.sleep(interval)
        elapsed += interval or seconds
        status = await controller.request_json("GET", STATUS_PATH, vin=vin)
        print(f"t+{int(elapsed)}s {block}: {json.dumps((status.get('payload') or {}).get(block))}")
