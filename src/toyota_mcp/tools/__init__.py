from mcp.server import MCPServer

from toyota_mcp.tools import energy, health, location, refresh, status, trips
from toyota_mcp.tools.base import READ_ONLY

__all__ = ["READ_ONLY", "register_all"]


def register_all(mcp: MCPServer) -> None:
    status.register(mcp)
    energy.register(mcp)
    location.register(mcp)
    trips.register(mcp)
    health.register(mcp)
    refresh.register(mcp)
