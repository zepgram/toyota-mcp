from __future__ import annotations

from typing import Any

import pytest
from mcp.client import Client
from mcp.server import MCPServer
from pytoyoda.exceptions import ToyotaApiError

from tests.conftest import FakeControllerBase
from toyota_mcp import errors
from toyota_mcp.gateway import VehicleGateway
from toyota_mcp.models import FULL_HYBRID_BATTERY_NOTE
from toyota_mcp.server import create_server

EXPECTED_TOOLS = {
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
def server(gateway: VehicleGateway) -> MCPServer:
    return create_server(gateway)


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


async def test_get_status(server: MCPServer) -> None:
    result = await _call(server, "toyota_get_status")
    assert not result.is_error
    content = result.structured_content
    assert content["all_locked"] == "locked"
    assert content["doors"]["driver"]["state"] == "closed"
    assert content["windows"]["driver"] == "closed"
    assert content["hood"] == "closed"
    assert content["freshness"]["source"] == "live"
    assert content["freshness"]["vehicle_reported_at"] is not None


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
    assert "parks" in content["freshness"]["note"]


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
