from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from pytoyoda.exceptions import ToyotaApiError, ToyotaLoginError

from tests.conftest import LOLA_VIN, FakeControllerBase
from toyota_mcp import errors
from toyota_mcp.cache import Snapshot
from toyota_mcp.config import Settings
from toyota_mcp.gateway import VehicleGateway

STATUS_PATH = "/v1/vehicle/status"
TELEMETRY_PATH = "/v3/telemetry"


def _expire(gateway: VehicleGateway, key: str, seconds: float) -> None:
    snapshot = gateway._cache.any(key)
    assert snapshot is not None
    gateway._cache._entries[key] = Snapshot(
        value=snapshot.value,
        fetched_at=datetime.now(timezone.utc) - timedelta(seconds=seconds),
    )


async def test_single_flight_collapses_concurrent_calls(
    gateway: VehicleGateway, fake_controller_class: type[FakeControllerBase]
) -> None:
    results = await asyncio.gather(*(gateway.lock_status() for _ in range(10)))
    assert fake_controller_class.calls.count(STATUS_PATH) == 1
    live_count = sum(1 for _, freshness in results if freshness.source == "live")
    assert live_count == 1
    assert all(freshness.source in ("live", "cache") for _, freshness in results)


async def test_snapshot_isolated_per_endpoint(
    gateway: VehicleGateway, fake_controller_class: type[FakeControllerBase]
) -> None:
    await gateway.lock_status()
    assert TELEMETRY_PATH not in fake_controller_class.calls


async def test_second_call_served_from_cache(
    gateway: VehicleGateway, fake_controller_class: type[FakeControllerBase]
) -> None:
    _, first = await gateway.lock_status()
    _, second = await gateway.lock_status()
    assert first.source == "live"
    assert second.source == "cache"
    assert fake_controller_class.calls.count(STATUS_PATH) == 1


async def test_stale_serve_on_transient_failure(
    gateway: VehicleGateway, fake_controller_class: type[FakeControllerBase]
) -> None:
    value, _ = await gateway.lock_status()
    _expire(gateway, "status", seconds=10_000)
    fake_controller_class.failure = ToyotaApiError("Request Failed. 429, slow down.")
    stale_value, freshness = await gateway.lock_status()
    assert stale_value is value
    assert freshness.source == "stale_cache"
    assert freshness.age_seconds > 9_000
    assert freshness.note is not None
    assert "temporarily unavailable" in freshness.note


async def test_cold_cache_transient_failure_raises_translated(
    gateway: VehicleGateway, fake_controller_class: type[FakeControllerBase]
) -> None:
    await gateway.dashboard()
    fake_controller_class.failure = ToyotaApiError("Request Failed. 429, slow down.")
    with pytest.raises(ToolError) as excinfo:
        await gateway.lock_status()
    assert excinfo.value.args[0] == errors.API_UNAVAILABLE


async def test_permanent_failure_never_serves_stale(
    gateway: VehicleGateway, fake_controller_class: type[FakeControllerBase]
) -> None:
    await gateway.lock_status()
    _expire(gateway, "status", seconds=10_000)
    fake_controller_class.failure = ToyotaApiError("Request Failed. 404, gone.")
    with pytest.raises(ToolError) as excinfo:
        await gateway.lock_status()
    assert excinfo.value.args[0] == errors.ENDPOINT_CHANGED


async def test_rate_limit_never_touches_login(
    gateway: VehicleGateway, fake_controller_class: type[FakeControllerBase]
) -> None:
    await gateway.dashboard()
    fake_controller_class.failure = ToyotaApiError("Request Failed. 429, slow down.")
    with pytest.raises(ToolError):
        await gateway.lock_status()
    fake_controller_class.failure = None
    await gateway.lock_status()
    assert fake_controller_class.login_count == 1


async def test_login_failure_arms_cooldown(
    gateway: VehicleGateway, fake_controller_class: type[FakeControllerBase]
) -> None:
    fake_controller_class.login_failure = ToyotaLoginError("Authentication Failed. 401, denied.")
    with pytest.raises(ToolError) as first:
        await gateway.lock_status()
    assert first.value.args[0] == errors.LOGIN_FAILED
    with pytest.raises(ToolError) as second:
        await gateway.dashboard()
    assert second.value.args[0] == errors.LOGIN_FAILED
    assert fake_controller_class.login_count == 1


async def test_powertrain(gateway: VehicleGateway) -> None:
    assert await gateway.powertrain() == "full_hybrid"


async def test_vin_selection(fake_controller_class: type[FakeControllerBase]) -> None:
    settings = Settings(
        username="user@example.com", password="secret", vin=LOLA_VIN, use_metric=True
    )
    gateway = VehicleGateway(settings, controller_class=fake_controller_class)
    assert await gateway.powertrain() == "full_hybrid"


async def test_unknown_vin_lists_available(
    fake_controller_class: type[FakeControllerBase],
) -> None:
    settings = Settings(
        username="user@example.com", password="secret", vin="WRONGVIN123456789", use_metric=True
    )
    gateway = VehicleGateway(settings, controller_class=fake_controller_class)
    with pytest.raises(ToolError) as excinfo:
        await gateway.powertrain()
    message = excinfo.value.args[0]
    assert "…6789" in message
    assert "Lola" in message


async def test_empty_account(
    settings: Settings, fake_controller_class: type[FakeControllerBase]
) -> None:
    fake_controller_class.responses["/v2/vehicle/guid"]["payload"] = []
    gateway = VehicleGateway(settings, controller_class=fake_controller_class)
    with pytest.raises(ToolError) as excinfo:
        await gateway.lock_status()
    assert excinfo.value.args[0] == errors.NO_VEHICLES


async def test_trips_live(
    gateway: VehicleGateway, fake_controller_class: type[FakeControllerBase]
) -> None:
    trips, freshness = await gateway.trips(date(2023, 9, 1), date(2023, 11, 21))
    assert len(trips) == 3
    assert freshness.source == "live"


async def test_daily_summaries_live(gateway: VehicleGateway) -> None:
    summaries, _ = await gateway.daily_summaries(date(2023, 9, 1), date(2023, 11, 21))
    assert len(summaries) > 0


async def test_refresh_floor(
    gateway: VehicleGateway, fake_controller_class: type[FakeControllerBase]
) -> None:
    refreshed, note, freshness = await gateway.refresh()
    assert refreshed is True
    assert freshness.source == "live"
    calls_after_first = len(fake_controller_class.calls)
    refreshed_again, note, freshness = await gateway.refresh()
    assert refreshed_again is False
    assert "skipped" in note
    assert len(fake_controller_class.calls) == calls_after_first


async def test_refresh_skip_without_snapshot_says_so(gateway: VehicleGateway) -> None:
    await gateway.refresh()
    gateway._cache.clear()
    refreshed, _, freshness = await gateway.refresh()
    assert refreshed is False
    assert freshness.note is not None
    assert "No snapshot" in freshness.note


async def test_refresh_restocks_snapshots(
    gateway: VehicleGateway, fake_controller_class: type[FakeControllerBase]
) -> None:
    await gateway.refresh()
    calls_after_refresh = len(fake_controller_class.calls)
    await gateway.lock_status()
    await gateway.dashboard()
    await gateway.location()
    assert len(fake_controller_class.calls) == calls_after_refresh
