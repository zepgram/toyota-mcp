from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Literal, TypeVar, cast

from mcp.server.mcpserver.exceptions import ToolError
from pytoyoda.client import MyT
from pytoyoda.controller import Controller
from pytoyoda.exceptions import ToyotaLoginError
from pytoyoda.models.climate import ClimateSettings, ClimateStatus
from pytoyoda.models.dashboard import Dashboard
from pytoyoda.models.endpoints.climate import (
    HeatingOptionsModel,
    SeatOptionsModel,
    V2RemoteClimateControlRequestModel,
)
from pytoyoda.models.endpoints.common import UnitValueModel
from pytoyoda.models.location import Location
from pytoyoda.models.lock_status import LockStatus
from pytoyoda.models.nofication import Notification
from pytoyoda.models.service_history import ServiceHistory
from pytoyoda.models.summary import Summary, SummaryType
from pytoyoda.models.trips import Trip
from pytoyoda.models.vehicle import Vehicle

from toyota_mcp import errors
from toyota_mcp.cache import (
    COMMAND_COOLDOWN,
    LOGIN_COOLDOWN,
    REFRESH_FLOOR,
    TTL_HEALTH_BUNDLE,
    TTL_LOCATION,
    TTL_STATUS,
    TTL_TELEMETRY,
    Snapshot,
    SnapshotCache,
)
from toyota_mcp.config import Settings
from toyota_mcp.models import CalendarPeriod, Freshness, Powertrain, StatusExtras
from toyota_mcp.opendata import OpenData
from toyota_mcp.places import Places

T = TypeVar("T")

logger = logging.getLogger(__name__)

KNOWN_POWERTRAINS: tuple[Powertrain, ...] = (
    "full_hybrid",
    "plug_in_hybrid",
    "electric",
    "fuel_only",
)

NO_STATUS_REPORTED = (
    "The vehicle did not report door/window status — "
    "this vehicle or account may not support remote status."
)
NO_TELEMETRY_REPORTED = (
    "The vehicle did not report telemetry — "
    "this vehicle or account may not support connected telemetry."
)
NO_SNAPSHOT_NOTE = "No snapshot is cached — the vehicle reported no data on the last refresh."
STATUS_PATH = "/v1/vehicle/status"
HEALTH_PATH = "/v1/vehiclehealth/status"
COMMAND_PATH = "/v1/global/remote/command"
NO_CLIMATE_REPORTED = (
    "The vehicle did not report climate data — remote climate may not be supported."
)
NO_CLIMATE_PRESET = (
    "The vehicle has no saved climate preset and no temperature was given — "
    "pass temperature_celsius explicitly."
)
ACCEPTED_CODE = "000000"


class CommandRejected(Exception):
    pass


@dataclass(frozen=True)
class HealthBundle:
    warning_lights: list[str]
    engine_oil_indicators: list[str]
    notifications: list[Notification]
    service_history: list[ServiceHistory[Any]]
    service_history_enabled: bool


@dataclass(frozen=True)
class StatusBundle:
    lock_status: LockStatus
    extras: StatusExtras


@dataclass(frozen=True)
class ClimateBundle:
    status: ClimateStatus
    settings: ClimateSettings


@dataclass
class AppContext:
    gateway: VehicleGateway
    opendata: OpenData | None = None
    places: Places = field(default_factory=Places)


def _capturing(
    base: type[Controller],
) -> tuple[type[Controller], dict[str, Any], dict[str, Controller]]:
    raw: dict[str, Any] = {}
    holder: dict[str, Controller] = {}

    class CapturingController(base):  # type: ignore[valid-type,misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            holder["controller"] = self

        async def request_json(
            self,
            method: str,
            endpoint: str,
            vin: str | None = None,
            body: dict[str, Any] | None = None,
            params: dict[str, Any] | None = None,
            headers: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            payload: dict[str, Any] = await super().request_json(
                method, endpoint, vin=vin, body=body, params=params, headers=headers
            )
            raw[endpoint.split("?")[0]] = payload
            return payload

    return CapturingController, raw, holder


class VehicleGateway:
    def __init__(
        self,
        settings: Settings,
        controller_class: type[Controller] = Controller,
        command_poll_interval: float = 5.0,
        command_timeout: float = 40.0,
    ) -> None:
        self._settings = settings
        self._command_poll_interval = command_poll_interval
        self._command_timeout = command_timeout
        self._last_command_at = 0.0
        capturing_class, self._raw, holder = _capturing(controller_class)
        self._client = MyT(
            username=settings.username,
            password=settings.password.get_secret_value(),
            use_metric=settings.use_metric,
            brand=settings.brand,
            controller_class=capturing_class,
        )
        self._controller = holder["controller"]
        self._cache = SnapshotCache()
        self._lock = asyncio.Lock()
        self._vehicle: Vehicle[Any] | None = None
        self._login_error: ToolError | None = None
        self._login_blocked_until = 0.0
        self._last_refresh_at: datetime | None = None

    @property
    def use_metric(self) -> bool:
        return self._settings.use_metric

    async def status(self) -> tuple[StatusBundle, Freshness]:
        return await self._snapshot("status", TTL_STATUS, ["status"], self._status_bundle)

    async def climate(self) -> tuple[ClimateBundle, Freshness]:
        return await self._snapshot(
            "climate", TTL_STATUS, ["climate_settings", "climate_status"], _extract_climate
        )

    async def vehicle(self) -> Vehicle[Any]:
        async with self._lock:
            return await self._ensure_vehicle()

    async def dashboard(self) -> tuple[Dashboard[Any], Freshness]:
        return await self._snapshot(
            "telemetry", TTL_TELEMETRY, ["telemetry", "electric_status"], _extract_dashboard
        )

    async def location(self) -> tuple[Location, Freshness]:
        return await self._snapshot("location", TTL_LOCATION, ["location"], _extract_location)

    async def health(self) -> tuple[HealthBundle, Freshness]:
        return await self._snapshot(
            "health_bundle",
            TTL_HEALTH_BUNDLE,
            ["health_status", "notifications", "service_history"],
            self._health_bundle,
        )

    async def calendar_summary(
        self, period: CalendarPeriod
    ) -> tuple[Summary[Any] | None, Freshness]:
        return await self._live(lambda vehicle: _CALENDAR_SUMMARIES[period](vehicle))

    async def powertrain(self) -> Powertrain:
        async with self._lock:
            vehicle = await self._ensure_vehicle()
        kind = vehicle.type
        if kind in KNOWN_POWERTRAINS:
            return kind
        return "unknown"

    async def last_trip(self) -> tuple[Trip[Any] | None, Freshness]:
        return await self._live(lambda vehicle: vehicle.get_last_trip())

    async def trips(self, from_date: date, to_date: date) -> tuple[list[Trip[Any]], Freshness]:
        found, freshness = await self._live(lambda vehicle: vehicle.get_trips(from_date, to_date))
        return found or [], freshness

    async def daily_summaries(
        self, from_date: date, to_date: date
    ) -> tuple[list[Summary[Any]], Freshness]:
        found, freshness = await self._live(
            lambda vehicle: vehicle.get_summary(from_date, to_date, SummaryType.DAILY)
        )
        return found or [], freshness

    async def refresh(self) -> tuple[bool, str, Freshness]:
        async with self._lock:
            if self._last_refresh_at is not None:
                elapsed = (datetime.now(UTC) - self._last_refresh_at).total_seconds()
                if elapsed < REFRESH_FLOOR:
                    note = (
                        f"Refresh skipped — data was already refreshed {int(elapsed)}s ago and "
                        "Toyota only receives new data when the car parks."
                    )
                    return False, note, self._skipped_refresh_freshness(self._last_refresh_at)
            self._check_cooldown()
            vehicle = await self._ensure_vehicle()
            try:
                await vehicle.update(only=["telemetry", "electric_status", "location", "status"])
            except ToolError:
                raise
            except Exception as exc:
                raise self._translated(exc) from exc
            if vehicle.dashboard is not None:
                self._cache.store("telemetry", vehicle.dashboard)
            if vehicle.location is not None:
                self._cache.store("location", vehicle.location)
            if vehicle.lock_status is not None:
                self._cache.store("status", self._status_bundle(vehicle))
            self._last_refresh_at = datetime.now(UTC)
            return (
                True,
                "Refreshed from Toyota's cloud.",
                Freshness(fetched_at=self._last_refresh_at, age_seconds=0, source="live"),
            )

    async def send_command(self, command: str) -> datetime:
        body = {"command": command}
        return await self._command(
            "status",
            send=lambda vehicle: self._controller.request_json(
                "POST", COMMAND_PATH, vin=vehicle.vin, body=body
            ),
            wake=lambda vehicle: vehicle.refresh_status(),
        )

    async def wake(self) -> tuple[StatusBundle, StatusBundle | None, Freshness | None, bool]:
        before, _ = await self.status()
        async with self._lock:
            self._check_cooldown()
            vehicle = await self._ensure_vehicle()
            with contextlib.suppress(Exception):
                await vehicle.refresh_status()
            with contextlib.suppress(Exception):
                await vehicle.refresh_climate_status()
            for key in ("status", "telemetry", "location", "climate"):
                self._cache.drop(key)
        after, freshness, reported = await self.wait_for_status(
            lambda now: now.extras.is_newer_than(before.extras)
        )
        return before, after, freshness, reported

    async def start_climate(self, temperature_celsius: float | None) -> datetime:
        async def send(vehicle: Vehicle[Any]) -> object:
            await vehicle.update(only=["climate_settings"])
            settings = vehicle.climate_settings
            if settings is None:
                raise ToolError(NO_CLIMATE_REPORTED)
            return await vehicle.set_climate(_climate_start_request(settings, temperature_celsius))

        return await self._command(
            "climate", send=send, wake=lambda vehicle: vehicle.refresh_climate_status()
        )

    async def stop_climate(self) -> datetime:
        async def send(vehicle: Vehicle[Any]) -> object:
            return await vehicle.set_climate(V2RemoteClimateControlRequestModel(command="stop"))

        return await self._command(
            "climate", send=send, wake=lambda vehicle: vehicle.refresh_climate_status()
        )

    async def wait_for_status(
        self, satisfied: Callable[[StatusBundle], bool]
    ) -> tuple[StatusBundle | None, Freshness | None, bool]:
        return await self._wait_for("status", ["status"], self._status_bundle, satisfied)

    async def wait_for_climate(
        self, satisfied: Callable[[ClimateBundle], bool]
    ) -> tuple[ClimateBundle | None, Freshness | None, bool]:
        return await self._wait_for("climate", ["climate_status"], _extract_climate, satisfied)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _command(
        self,
        key: str,
        send: Callable[[Vehicle[Any]], Awaitable[object]],
        wake: Callable[[Vehicle[Any]], Awaitable[object]],
    ) -> datetime:
        async with self._lock:
            elapsed = time.monotonic() - self._last_command_at
            if self._last_command_at and elapsed < COMMAND_COOLDOWN:
                raise ToolError(
                    f"Another command was sent {int(elapsed)}s ago — wait "
                    f"{COMMAND_COOLDOWN - int(elapsed)}s before sending a new one."
                )
            self._check_cooldown()
            vehicle = await self._ensure_vehicle()
            sent_at = datetime.now(UTC)
            try:
                response = await send(vehicle)
            except ToolError:
                raise
            except Exception as exc:
                raise self._translated(exc) from exc
            finally:
                self._cache.drop(key)
                self._last_command_at = time.monotonic()
                self._last_refresh_at = None
            rejection = _rejection(response)
            if rejection is not None:
                raise CommandRejected(rejection)
            with contextlib.suppress(Exception):
                await wake(vehicle)
            return sent_at

    async def _wait_for(
        self,
        key: str,
        endpoints: list[str],
        extract: Callable[[Vehicle[Any]], T],
        satisfied: Callable[[T], bool],
    ) -> tuple[T | None, Freshness | None, bool]:
        deadline = time.monotonic() + self._command_timeout
        latest: tuple[T, Freshness] | None = None
        while True:
            try:
                latest = await self._fetch(endpoints, extract)
            except ToolError as exc:
                logger.warning("verification read failed: %s", exc)
            if latest is not None and satisfied(latest[0]):
                self._cache.store(key, latest[0])
                return latest[0], latest[1], True
            if time.monotonic() + self._command_poll_interval > deadline:
                break
            await asyncio.sleep(self._command_poll_interval)
        if latest is None:
            return None, None, False
        return latest[0], latest[1], False

    async def _fetch(
        self, endpoints: list[str], extract: Callable[[Vehicle[Any]], T]
    ) -> tuple[T, Freshness]:
        async with self._lock:
            self._check_cooldown()
            vehicle = await self._ensure_vehicle()
            try:
                await vehicle.update(only=endpoints)
                value = extract(vehicle)
            except ToolError:
                raise
            except Exception as exc:
                raise self._translated(exc) from exc
            return value, Freshness(fetched_at=datetime.now(UTC), age_seconds=0, source="live")

    async def _snapshot(
        self,
        key: str,
        ttl: int,
        endpoints: list[str],
        extract: Callable[[Vehicle[Any]], T],
    ) -> tuple[T, Freshness]:
        snapshot = self._cache.fresh(key, ttl)
        if snapshot is not None:
            return cast(T, snapshot.value), _freshness(snapshot, "cache")
        async with self._lock:
            snapshot = self._cache.fresh(key, ttl)
            if snapshot is not None:
                return cast(T, snapshot.value), _freshness(snapshot, "cache")
            self._check_cooldown()
            vehicle = await self._ensure_vehicle()
            started = time.monotonic()
            try:
                await vehicle.update(only=endpoints)
                value = extract(vehicle)
            except ToolError:
                raise
            except Exception as exc:
                stale = self._cache.any(key)
                if stale is not None and errors.is_transient(exc):
                    logger.warning("serving stale %s snapshot after %s", key, type(exc).__name__)
                    return cast(T, stale.value), _stale_freshness(stale)
                raise self._translated(exc) from exc
            logger.info("fetched %s in %.1fs", key, time.monotonic() - started)
            stored = self._cache.store(key, value)
            return value, _freshness(stored, "live")

    async def _live(self, operation: Callable[[Vehicle[Any]], Awaitable[T]]) -> tuple[T, Freshness]:
        async with self._lock:
            self._check_cooldown()
            vehicle = await self._ensure_vehicle()
            started = time.monotonic()
            try:
                value = await operation(vehicle)
            except ToolError:
                raise
            except Exception as exc:
                raise self._translated(exc) from exc
            logger.info("live call answered in %.1fs", time.monotonic() - started)
            return value, Freshness(fetched_at=datetime.now(UTC), age_seconds=0, source="live")

    async def _ensure_vehicle(self) -> Vehicle[Any]:
        if self._vehicle is not None:
            return self._vehicle
        self._check_cooldown()
        try:
            await self._client.login()
            vehicles = [
                vehicle for vehicle in await self._client.get_vehicles() if vehicle is not None
            ]
        except Exception as exc:
            raise self._translated(exc) from exc
        self._vehicle = self._select_vehicle(vehicles)
        return self._vehicle

    def _select_vehicle(self, vehicles: list[Vehicle[Any]]) -> Vehicle[Any]:
        if not vehicles:
            raise ToolError(errors.NO_VEHICLES)
        available = [vehicle_label(vehicle) for vehicle in vehicles]
        if self._settings.vin is None:
            if len(vehicles) == 1:
                return vehicles[0]
            raise ToolError(
                "Multiple vehicles are attached to this account — set TOYOTA_VIN to pick one. "
                f"Available vehicles: {', '.join(available)}."
            )
        for vehicle in vehicles:
            if vehicle.vin == self._settings.vin:
                return vehicle
        raise errors.vin_not_found(self._settings.vin, available)

    def _check_cooldown(self) -> None:
        if self._login_error is None:
            return
        if time.monotonic() < self._login_blocked_until:
            raise self._login_error
        self._login_error = None

    def _translated(self, exc: Exception) -> ToolError:
        logger.exception("upstream call failed (%s)", type(exc).__name__)
        error = errors.translate(exc)
        if isinstance(exc, ToyotaLoginError):
            self._login_error = error
            self._login_blocked_until = time.monotonic() + LOGIN_COOLDOWN
        return error

    def _health_bundle(self, vehicle: Vehicle[Any]) -> HealthBundle:
        dashboard = vehicle.dashboard
        raw_health = (self._raw.get(HEALTH_PATH) or {}).get("payload") or {}
        history = vehicle.service_history
        return HealthBundle(
            warning_lights=[
                str(light) for light in (dashboard.warning_lights if dashboard else None) or []
            ],
            engine_oil_indicators=[
                str(item) for item in raw_health.get("quantityOfEngOilIcon") or []
            ],
            notifications=list(vehicle.notifications or []),
            service_history=sorted(
                history or [], key=lambda service: service.service_date or date.min, reverse=True
            ),
            service_history_enabled=history is not None,
        )

    def _status_bundle(self, vehicle: Vehicle[Any]) -> StatusBundle:
        if vehicle.lock_status is None:
            raise ToolError(NO_STATUS_REPORTED)
        raw_status = self._raw.get(STATUS_PATH) or {}
        return StatusBundle(
            lock_status=vehicle.lock_status,
            extras=StatusExtras.from_payload(raw_status.get("payload")),
        )

    def _skipped_refresh_freshness(self, refreshed_at: datetime) -> Freshness:
        newest = self._newest_volatile_snapshot()
        if newest is not None:
            return _freshness(newest, "cache")
        return Freshness(
            fetched_at=refreshed_at,
            age_seconds=round((datetime.now(UTC) - refreshed_at).total_seconds(), 1),
            source="cache",
            note=NO_SNAPSHOT_NOTE,
        )

    def _newest_volatile_snapshot(self) -> Snapshot | None:
        snapshots = [
            snapshot
            for key in ("telemetry", "location", "status")
            if (snapshot := self._cache.any(key)) is not None
        ]
        if not snapshots:
            return None
        return max(snapshots, key=lambda snapshot: snapshot.fetched_at)


def vehicle_label(vehicle: Vehicle[Any]) -> str:
    alias = vehicle.alias if vehicle.alias and vehicle.alias != vehicle.vin else "unnamed"
    return f"{alias} (…{(vehicle.vin or '????')[-4:]})"


_CALENDAR_SUMMARIES: dict[
    CalendarPeriod, Callable[[Vehicle[Any]], Awaitable[Summary[Any] | None]]
] = {
    "today": lambda vehicle: vehicle.get_current_day_summary(),
    "this_week": lambda vehicle: vehicle.get_current_week_summary(),
    "this_month": lambda vehicle: vehicle.get_current_month_summary(),
    "this_year": lambda vehicle: vehicle.get_current_year_summary(),
}


def _rejection(response: object) -> str | None:
    payload = response.get("payload") if isinstance(response, dict) else None
    if isinstance(payload, dict):
        return_code = payload.get("returnCode")
    else:
        return_code = getattr(getattr(response, "payload", None), "return_code", None)
    if return_code is None or return_code == ACCEPTED_CODE:
        return None
    return f"Toyota rejected the command (code {return_code}) — nothing was executed."


def _climate_start_request(
    settings: ClimateSettings, temperature_celsius: float | None
) -> V2RemoteClimateControlRequestModel:
    preset = settings.temperature
    if temperature_celsius is not None:
        temperature = UnitValueModel(value=temperature_celsius, unit="C")
    elif preset is not None and preset.value is not None:
        temperature = UnitValueModel(value=preset.value, unit=preset.unit or "C")
    else:
        raise ToolError(NO_CLIMATE_PRESET)
    heating = settings.heating_options
    seats = settings.seat_options
    return V2RemoteClimateControlRequestModel(
        command="start",
        temperature=temperature,
        duration=int(settings.duration.total_seconds() // 60) if settings.duration else None,
        heating_options=(
            HeatingOptionsModel(
                front_defroster=_switch(heating.front_defroster),
                rear_defogger=_switch(heating.rear_defogger),
                steering_heater=_switch(heating.steering_heater),
            )
            if heating
            else None
        ),
        seat_options=(
            SeatOptionsModel(
                driver_seat=seats.driver_seat,
                passenger_seat=seats.passenger_seat,
                rear_driver_seat=seats.rear_driver_seat,
                rear_passenger_seat=seats.rear_passenger_seat,
            )
            if seats
            else None
        ),
        save_settings=False,
    )


def _switch(enabled: bool | None) -> str | None:
    if enabled is None:
        return None
    return "on" if enabled else "off"


def _extract_climate(vehicle: Vehicle[Any]) -> ClimateBundle:
    status = vehicle.climate_status
    settings = vehicle.climate_settings
    if status is None or settings is None:
        raise ToolError(NO_CLIMATE_REPORTED)
    if status.status is None and settings.temperature is None:
        raise ToolError(NO_CLIMATE_REPORTED)
    return ClimateBundle(status=status, settings=settings)


def _extract_dashboard(vehicle: Vehicle[Any]) -> Dashboard[Any]:
    if vehicle.dashboard is None:
        raise ToolError(NO_TELEMETRY_REPORTED)
    return vehicle.dashboard


def _extract_location(vehicle: Vehicle[Any]) -> Location:
    location = vehicle.location
    if location is None or location.latitude is None or location.longitude is None:
        raise ToolError(errors.LOCATION_NEVER_REPORTED)
    return location


def _freshness(snapshot: Snapshot, source: Literal["live", "cache"]) -> Freshness:
    return Freshness(
        fetched_at=snapshot.fetched_at,
        age_seconds=round(snapshot.age_seconds(), 1),
        source=source,
    )


def _stale_freshness(snapshot: Snapshot) -> Freshness:
    minutes = int(snapshot.age_seconds() // 60)
    return Freshness(
        fetched_at=snapshot.fetched_at,
        age_seconds=round(snapshot.age_seconds(), 1),
        source="stale_cache",
        note=(
            f"Toyota's API is temporarily unavailable — showing data from {minutes} minute(s) ago."
        ),
    )
