from __future__ import annotations

from typing import Any

import pytest
from mcp.client import Client
from mcp.server import MCPServer
from pytoyoda.exceptions import ToyotaApiError

from tests.conftest import (
    BAN_LABEL,
    CLIMATE_CONTROL_PATH,
    CLIMATE_STATUS_PATH,
    COMMAND_PATH,
    STATUS_PATH,
    FakeControllerBase,
)
from toyota_mcp import errors
from toyota_mcp.gateway import VehicleGateway
from toyota_mcp.models import FULL_HYBRID_BATTERY_NOTE
from toyota_mcp.opendata import FrenchOpenData
from toyota_mcp.server import create_server

EXPECTED_TOOLS = {
    "toyota_get_vehicle_info",
    "toyota_get_climate",
    "toyota_find_fuel_stations",
    "toyota_get_status",
    "toyota_get_energy",
    "toyota_get_odometer",
    "toyota_get_location",
    "toyota_get_last_trip",
    "toyota_get_trips",
    "toyota_get_trip_summary",
    "toyota_get_health",
    "toyota_refresh_data",
}


@pytest.fixture
def server(gateway: VehicleGateway, opendata: FrenchOpenData) -> MCPServer:
    return create_server(gateway, opendata)


@pytest.fixture
def offline_server(gateway: VehicleGateway) -> MCPServer:
    return create_server(gateway)


@pytest.fixture
def command_server(command_gateway: VehicleGateway) -> MCPServer:
    return create_server(command_gateway, remote_commands=True)


COMMAND_TOOLS = {
    "toyota_lock_doors",
    "toyota_unlock_doors",
    "toyota_start_climate",
    "toyota_stop_climate",
}


async def _call(server: MCPServer, tool: str, arguments: dict[str, Any] | None = None) -> Any:
    async with Client(server) as client:
        return await client.call_tool(tool, arguments or {})


async def test_tools_list_contract(server: MCPServer) -> None:
    async with Client(server) as client:
        listing = await client.list_tools()
    tools = {tool.name: tool for tool in listing.tools}
    assert set(tools) == EXPECTED_TOOLS
    for tool in tools.values():
        assert tool.name.startswith("toyota_")
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.open_world_hint is False
        assert tool.output_schema is not None
        assert tool.description


async def test_commands_are_opt_in(server: MCPServer, command_server: MCPServer) -> None:
    async with Client(server) as client:
        default_tools = {tool.name for tool in (await client.list_tools()).tools}
    async with Client(command_server) as client:
        listing = (await client.list_tools()).tools
    command_tools = {tool.name: tool for tool in listing if tool.name in COMMAND_TOOLS}
    assert not default_tools & COMMAND_TOOLS
    assert set(command_tools) == COMMAND_TOOLS
    for name, tool in command_tools.items():
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is False
        assert tool.annotations.destructive_hint is (
            name in {"toyota_unlock_doors", "toyota_start_climate"}
        )


async def test_lock_preview_sends_nothing(
    command_server: MCPServer, fake_controller_class: type[FakeControllerBase]
) -> None:
    result = await _call(command_server, "toyota_lock_doors")
    assert not result.is_error
    content = result.structured_content
    assert content["status"] == "needs_confirmation"
    assert "confirm=true" in content["detail"]
    assert content["doors"]["all_locked"] == "locked"
    assert fake_controller_class.commands == []


async def test_unlock_verified_from_state_change(
    command_server: MCPServer, fake_controller_class: type[FakeControllerBase]
) -> None:
    result = await _call(command_server, "toyota_unlock_doors", {"confirm": True})
    assert not result.is_error
    content = result.structured_content
    assert content["status"] == "verified"
    assert content["doors"]["all_locked"] == "unlocked"
    assert fake_controller_class.commands == [(COMMAND_PATH, {"command": "door-unlock"})]
    assert "/v1/remote/status" in fake_controller_class.calls
    locked = await _call(command_server, "toyota_lock_doors", {"confirm": True})
    assert locked.is_error
    assert "wait" in locked.content[0].text


async def test_lock_already_locked_needs_a_newer_report(
    command_server: MCPServer, fake_controller_class: type[FakeControllerBase]
) -> None:
    fake_controller_class.commands_take_effect = False
    result = await _call(command_server, "toyota_lock_doors", {"confirm": True})
    assert not result.is_error
    content = result.structured_content
    assert content["status"] == "accepted"
    assert "already reported locked" in content["detail"]
    assert "toyota_get_status" in content["detail"]


async def test_lock_already_locked_verified_when_car_reports_again(
    command_server: MCPServer,
) -> None:
    result = await _call(command_server, "toyota_lock_doors", {"confirm": True})
    assert result.structured_content["status"] == "verified"


async def test_verification_keeps_polling_until_the_car_changes(
    command_server: MCPServer, fake_controller_class: type[FakeControllerBase]
) -> None:
    fake_controller_class.effect_delay = 2
    result = await _call(command_server, "toyota_unlock_doors", {"confirm": True})
    assert result.structured_content["status"] == "verified"
    assert fake_controller_class.calls.count(STATUS_PATH) >= 4


async def test_verification_survives_a_transient_read_error(
    command_server: MCPServer, fake_controller_class: type[FakeControllerBase]
) -> None:
    fake_controller_class.fail_reads_after_command = 1
    result = await _call(command_server, "toyota_unlock_doors", {"confirm": True})
    assert not result.is_error
    assert result.structured_content["status"] == "verified"


async def test_unconfirmed_command_leaves_no_stale_snapshot(
    command_server: MCPServer, fake_controller_class: type[FakeControllerBase]
) -> None:
    fake_controller_class.commands_take_effect = False
    result = await _call(command_server, "toyota_unlock_doors", {"confirm": True})
    assert result.structured_content["status"] == "accepted"
    reads_before = fake_controller_class.calls.count(STATUS_PATH)
    status = await _call(command_server, "toyota_get_status")
    assert status.structured_content["freshness"]["source"] == "live"
    assert fake_controller_class.calls.count(STATUS_PATH) == reads_before + 1


async def test_start_climate_sends_the_full_preset(
    command_server: MCPServer, fake_controller_class: type[FakeControllerBase]
) -> None:
    result = await _call(command_server, "toyota_start_climate", {"confirm": True})
    assert not result.is_error
    content = result.structured_content
    assert content["status"] == "verified"
    assert content["climate"]["is_on"] is True
    path, body = fake_controller_class.commands[0]
    assert path == CLIMATE_CONTROL_PATH
    assert body["command"] == "start"
    assert body["temperature"] == {"value": 22.0, "unit": "C"}
    assert body["duration"] == 20
    assert body["heatingOptions"]["frontDefroster"] == "off"
    assert body["seatOptions"]["driverSeat"] == "off"
    assert body["saveSettings"] is False


async def test_start_climate_with_explicit_temperature(
    command_server: MCPServer, fake_controller_class: type[FakeControllerBase]
) -> None:
    await _call(
        command_server, "toyota_start_climate", {"confirm": True, "temperature_celsius": 24.5}
    )
    assert fake_controller_class.commands[0][1]["temperature"] == {"value": 24.5, "unit": "C"}


async def test_start_climate_rejects_invalid_temperature(command_server: MCPServer) -> None:
    result = await _call(
        command_server, "toyota_start_climate", {"confirm": True, "temperature_celsius": 24.3}
    )
    assert result.is_error


async def test_climate_rejected_by_toyota_is_failed_without_polling(
    command_server: MCPServer, fake_controller_class: type[FakeControllerBase]
) -> None:
    fake_controller_class.climate_return_code = "118003"
    result = await _call(command_server, "toyota_start_climate", {"confirm": True})
    assert not result.is_error
    content = result.structured_content
    assert content["status"] == "failed"
    assert "118003" in content["detail"]
    assert fake_controller_class.calls.count(CLIMATE_STATUS_PATH) == 1


async def test_climate_not_enabled_sends_nothing(
    command_server: MCPServer, fake_controller_class: type[FakeControllerBase]
) -> None:
    fake_controller_class.responses["/v2/vehicle/guid"]["payload"][0]["features"][
        "climateStartEngine"
    ] = 0
    result = await _call(
        command_server, "toyota_start_climate", {"confirm": True, "temperature_celsius": 22}
    )
    assert result.is_error
    assert "not be supported" in result.content[0].text
    assert fake_controller_class.commands == []


async def test_stop_climate_preview_then_already_stopped(
    command_server: MCPServer, fake_controller_class: type[FakeControllerBase]
) -> None:
    preview = await _call(command_server, "toyota_stop_climate")
    assert preview.structured_content["status"] == "needs_confirmation"
    assert fake_controller_class.commands == []
    result = await _call(command_server, "toyota_stop_climate", {"confirm": True})
    content = result.structured_content
    assert content["status"] == "accepted"
    assert "already reported stopped" in content["detail"]
    assert fake_controller_class.commands == [(CLIMATE_CONTROL_PATH, {"command": "stop"})]


async def test_get_status(server: MCPServer) -> None:
    result = await _call(server, "toyota_get_status")
    assert not result.is_error
    content = result.structured_content
    assert content["all_locked"] == "locked"
    assert content["doors"]["driver"]["state"] == "closed"
    assert content["windows"]["driver"] == "closed"
    assert content["hood"] == "closed"
    assert content["lights"] == {
        "hazard": {"state": "off", "reported_at": "2026-06-30T20:29:07Z"},
        "tail": {"state": "off", "reported_at": "2026-06-30T20:29:07Z"},
        "head": {"state": "off", "reported_at": "2026-06-30T20:29:07Z"},
    }
    assert content["rear_seat_reminder"]["warning"] is False
    assert content["rear_seat_reminder"]["reason"] == "notDetected"
    assert content["overall_status"] == "ok"
    assert content["warning_count"] == 0
    assert content["freshness"]["source"] == "live"
    assert content["freshness"]["vehicle_reported_at"] is not None


async def test_get_status_without_extras_still_answers(
    server: MCPServer, fake_controller_class: type[FakeControllerBase]
) -> None:
    payload = fake_controller_class.responses["/v1/vehicle/status"]["payload"]
    for key in ("lights", "rearSeatReminder", "overallStatus", "overallWarningCounts"):
        payload.pop(key)
    result = await _call(server, "toyota_get_status")
    assert not result.is_error
    content = result.structured_content
    assert content["all_locked"] == "locked"
    assert content["lights"] is None
    assert content["rear_seat_reminder"] is None
    assert content["overall_status"] is None


async def test_get_vehicle_info(server: MCPServer) -> None:
    result = await _call(server, "toyota_get_vehicle_info")
    assert not result.is_error
    content = result.structured_content
    assert content["alias"] == "Lola"
    assert content["vin_suffix"] == "0042"
    assert content["model"] == "Corolla"
    assert content["model_year"] == "2020"
    assert content["colour"] == "Dark Blue Metallic"
    assert content["powertrain"] == "full_hybrid"
    assert content["date_of_first_use"] == "2022-11-30"
    assert content["connected_services_status"] == "SUBSCRIBED"
    assert content["subscriptions"][0]["product"] == "REMOTE SERVICES"
    capabilities = content["remote_capabilities"]
    assert capabilities["last_parked_position"] is True
    assert capabilities["telemetry"] is True
    assert capabilities["lock_unlock"] is False
    assert "unreliable" in capabilities["note"]


async def test_get_climate(server: MCPServer) -> None:
    result = await _call(server, "toyota_get_climate")
    assert not result.is_error
    content = result.structured_content
    assert content["status"] == "stopped"
    assert content["is_on"] is False
    assert content["preset"]["temperature"] == {"value": 22.0, "unit": "C"}
    assert content["preset"]["duration_minutes"] == 20.0
    assert content["preset"]["options"]["front_defroster"] is False
    assert content["preset"]["options"]["driver_seat"] == "off"
    assert "cannot" in content["note"] or "not available" in content["note"]


async def test_get_climate_not_supported(
    server: MCPServer, fake_controller_class: type[FakeControllerBase]
) -> None:
    fake_controller_class.responses["/v2/vehicle/guid"]["payload"][0]["features"][
        "climateStartEngine"
    ] = 0
    result = await _call(server, "toyota_get_climate")
    assert result.is_error
    assert "not be supported" in result.content[0].text


async def test_get_energy_full_hybrid(server: MCPServer) -> None:
    result = await _call(server, "toyota_get_energy")
    assert not result.is_error
    content = result.structured_content
    assert content["powertrain"] == "full_hybrid"
    assert content["fuel_level_percent"] == 99
    assert content["total_range"] == {"value": 634.0, "unit": "km"}
    assert content["battery"] is None
    assert content["battery_note"] == FULL_HYBRID_BATTERY_NOTE
    assert content["freshness"]["vehicle_reported_at"] is not None


async def test_get_odometer(server: MCPServer) -> None:
    result = await _call(server, "toyota_get_odometer")
    assert not result.is_error
    assert result.structured_content["odometer"] == {"value": 30420.0, "unit": "km"}
    assert result.structured_content["freshness"]["vehicle_reported_at"] is not None


async def test_get_location(server: MCPServer) -> None:
    result = await _call(server, "toyota_get_location")
    assert not result.is_error
    content = result.structured_content
    assert content["latitude"] == 52.169516
    assert content["longitude"] == 0.135654
    assert content["google_maps_url"] == "https://www.google.com/maps?q=52.169516,0.135654"
    assert content["address"] == BAN_LABEL
    assert "parks" in content["freshness"]["note"]


async def test_get_location_without_open_data(offline_server: MCPServer) -> None:
    result = await _call(offline_server, "toyota_get_location")
    assert not result.is_error
    assert result.structured_content["address"] is None


async def test_find_fuel_stations(server: MCPServer) -> None:
    result = await _call(server, "toyota_find_fuel_stations", {"fuel": "e10", "radius_km": 10})
    assert not result.is_error
    content = result.structured_content
    assert content["fuel"] == "e10"
    assert content["around"]["address"] == BAN_LABEL
    assert len(content["stations"]) == 2
    first = content["stations"][0]
    assert first["price_eur_per_litre"] == 1.985
    assert first["city"] == "Blagnac"
    assert 0 < first["distance_km"] < 1
    assert first["open_24h"] is False
    assert content["stations"][1]["open_24h"] is True
    assert "stations" in content["note"]


async def test_find_fuel_stations_disabled(offline_server: MCPServer) -> None:
    result = await _call(offline_server, "toyota_find_fuel_stations")
    assert result.is_error
    assert "TOYOTA_OPEN_DATA" in result.content[0].text


async def test_get_location_never_reported(
    server: MCPServer, fake_controller_class: type[FakeControllerBase]
) -> None:
    fake_controller_class.responses["/v1/location"]["payload"].pop("vehicleLocation")
    result = await _call(server, "toyota_get_location")
    assert result.is_error
    assert errors.LOCATION_NEVER_REPORTED in result.content[0].text


async def test_get_last_trip(server: MCPServer) -> None:
    result = await _call(server, "toyota_get_last_trip")
    assert not result.is_error
    content = result.structured_content
    assert content["trip"]["distance"]["unit"] == "km"
    assert content["trip"]["fuel_consumed"]["unit"] == "L"
    assert content["trip"]["ev_ratio_percent"] is not None
    assert content["trip"]["end"]["address"] == BAN_LABEL
    assert content["trip"]["hybrid_breakdown"]["ev"]["distance"]["unit"] == "km"
    assert content["freshness"]["source"] == "live"


async def test_get_trips(server: MCPServer) -> None:
    result = await _call(server, "toyota_get_trips", {"days": 30, "limit": 2})
    assert not result.is_error
    content = result.structured_content
    assert content["returned_count"] == 2
    assert content["total_in_window"] == 3
    assert "2 most recent of 3 trips" in content["note"]
    assert len(content["trips"]) == 2
    first, second = content["trips"]
    assert first["started_at"] >= second["started_at"]
    assert first["end"]["address"] == BAN_LABEL
    assert first["hybrid_breakdown"]["ev"]["duration_minutes"] is not None
    assert "12 months" in content["retention_note"]


async def test_get_trips_window_counts_calendar_days_inclusive(server: MCPServer) -> None:
    result = await _call(server, "toyota_get_trips", {"days": 1})
    assert not result.is_error
    content = result.structured_content
    assert content["window_from"] == content["window_to"]


async def test_get_trips_rejects_out_of_range_days(server: MCPServer) -> None:
    result = await _call(server, "toyota_get_trips", {"days": 0})
    assert result.is_error
    result = await _call(server, "toyota_get_trips", {"days": 400})
    assert result.is_error


async def test_get_trip_summary(server: MCPServer) -> None:
    result = await _call(server, "toyota_get_trip_summary", {"days": 365})
    assert not result.is_error
    content = result.structured_content
    assert content["total_distance"]["unit"] == "km"
    assert content["average_consumption"]["unit"] == "L/100km"
    assert content["ev_ratio_percent"] is not None
    assert content["days_with_driving"] > 0


async def test_get_health(server: MCPServer) -> None:
    result = await _call(server, "toyota_get_health")
    assert not result.is_error
    content = result.structured_content
    assert content["warning_lights"] == []
    assert len(content["notifications"]) == 10
    assert content["last_service"] is not None
    assert "HISTORY" in content["service_note"]


async def test_get_health_without_service_records(
    server: MCPServer, fake_controller_class: type[FakeControllerBase]
) -> None:
    fake_controller_class.responses["/v1/servicehistory/vehicle/summary"]["payload"][
        "serviceHistories"
    ] = []
    result = await _call(server, "toyota_get_health")
    assert not result.is_error
    content = result.structured_content
    assert content["last_service"] is None
    assert "HISTORY" in content["service_note"]


async def test_refresh_data_floor(server: MCPServer) -> None:
    async with Client(server) as client:
        first = await client.call_tool("toyota_refresh_data", {})
        second = await client.call_tool("toyota_refresh_data", {})
    assert not first.is_error
    assert first.structured_content["refreshed"] is True
    assert not second.is_error
    assert second.structured_content["refreshed"] is False
    assert "skipped" in second.structured_content["note"]


async def test_error_message_reaches_the_model(
    server: MCPServer, fake_controller_class: type[FakeControllerBase]
) -> None:
    fake_controller_class.failure = ToyotaApiError("Request Failed. 429, slow down.")
    result = await _call(server, "toyota_get_energy")
    assert result.is_error
    assert errors.API_UNAVAILABLE in result.content[0].text


async def test_structured_and_text_content_both_present(server: MCPServer) -> None:
    result = await _call(server, "toyota_get_energy")
    assert result.structured_content is not None
    assert result.content
    assert result.content[0].type == "text"
