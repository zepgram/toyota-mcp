from mcp.server import MCPServer

from toyota_mcp.tools import (
    climate,
    energy,
    fuel,
    health,
    location,
    refresh,
    status,
    trips,
    vehicle,
)
from toyota_mcp.tools.base import READ_ONLY

__all__ = ["READ_ONLY", "register_all"]


def register_all(mcp: MCPServer) -> None:
    vehicle.register(mcp)
    status.register(mcp)
    energy.register(mcp)
    location.register(mcp)
    trips.register(mcp)
    health.register(mcp)
    climate.register(mcp)
    fuel.register(mcp)
    refresh.register(mcp)
