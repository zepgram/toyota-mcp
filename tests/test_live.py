import pytest

from toyota_mcp.config import Settings
from toyota_mcp.gateway import VehicleGateway

pytestmark = pytest.mark.live


@pytest.fixture
async def live_gateway() -> VehicleGateway:
    return VehicleGateway(Settings())


async def test_live_energy(live_gateway: VehicleGateway) -> None:
    dashboard, freshness = await live_gateway.dashboard()
    assert dashboard.fuel_level is not None
    assert freshness.source == "live"


async def test_live_status(live_gateway: VehicleGateway) -> None:
    status, _ = await live_gateway.status()
    assert status.lock_status.last_updated is not None
    assert status.extras.overall_status is not None


async def test_live_location(live_gateway: VehicleGateway) -> None:
    location, _ = await live_gateway.location()
    assert location.latitude is not None
