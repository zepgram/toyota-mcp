from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Literal, TypeVar, cast

from mcp.server.mcpserver.exceptions import ToolError
from pytoyoda.client import MyT
from pytoyoda.controller import Controller
from pytoyoda.exceptions import ToyotaLoginError
from pytoyoda.models.dashboard import Dashboard
from pytoyoda.models.location import Location
from pytoyoda.models.lock_status import LockStatus
from pytoyoda.models.nofication import Notification
from pytoyoda.models.service_history import ServiceHistory
from pytoyoda.models.summary import Summary, SummaryType
from pytoyoda.models.trips import Trip
from pytoyoda.models.vehicle import Vehicle

from toyota_mcp import errors
from toyota_mcp.cache import (
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
from toyota_mcp.models import Freshness, Powertrain

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


@dataclass(frozen=True)
class HealthBundle:
    warning_lights: list[str]
    notifications: list[Notification]
    latest_service: ServiceHistory[Any] | None
    service_history_enabled: bool


@dataclass
class AppContext:
    gateway: VehicleGateway


class VehicleGateway:
    def __init__(
        self,
        settings: Settings,
        controller_class: type[Controller] = Controller,
    ) -> None:
        self._settings = settings
        self._client = MyT(
            username=settings.username,
            password=settings.password.get_secret_value(),
            use_metric=settings.use_metric,
            brand=settings.brand,
            controller_class=controller_class,
        )
        self._cache = SnapshotCache()
        self._lock = asyncio.Lock()
        self._vehicle: Vehicle[Any] | None = None
        self._login_error: ToolError | None = None
        self._login_blocked_until = 0.0
        self._last_refresh_at: datetime | None = None

    @property
    def use_metric(self) -> bool:
        return self._settings.use_metric

    async def lock_status(self) -> tuple[LockStatus, Freshness]:
        return await self._snapshot("status", TTL_STATUS, ["status"], _extract_lock_status)

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
            _extract_health,
        )

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
                self._cache.store("status", vehicle.lock_status)
            self._last_refresh_at = datetime.now(UTC)
            return (
                True,
                "Refreshed from Toyota's cloud.",
                Freshness(fetched_at=self._last_refresh_at, age_seconds=0, source="live"),
            )

    async def aclose(self) -> None:
        await self._client.aclose()

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
        available = [
            f"{vehicle.alias or 'unnamed'} (…{(vehicle.vin or '????')[-4:]})"
            for vehicle in vehicles
        ]
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


def _extract_lock_status(vehicle: Vehicle[Any]) -> LockStatus:
    if vehicle.lock_status is None:
        raise ToolError(NO_STATUS_REPORTED)
    return vehicle.lock_status


def _extract_dashboard(vehicle: Vehicle[Any]) -> Dashboard[Any]:
    if vehicle.dashboard is None:
        raise ToolError(NO_TELEMETRY_REPORTED)
    return vehicle.dashboard


def _extract_location(vehicle: Vehicle[Any]) -> Location:
    location = vehicle.location
    if location is None or location.latitude is None or location.longitude is None:
        raise ToolError(errors.LOCATION_NEVER_REPORTED)
    return location


def _extract_health(vehicle: Vehicle[Any]) -> HealthBundle:
    dashboard = vehicle.dashboard
    raw_lights = (dashboard.warning_lights if dashboard else None) or []
    warning_lights = [str(light) for light in raw_lights]
    history = vehicle.service_history
    return HealthBundle(
        warning_lights=warning_lights,
        notifications=list(vehicle.notifications or []),
        latest_service=_latest_service(history),
        service_history_enabled=history is not None,
    )


def _latest_service(history: list[ServiceHistory[Any]] | None) -> ServiceHistory[Any] | None:
    if not history:
        return None
    return max(history, key=lambda service: service.service_date or date.min)


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
