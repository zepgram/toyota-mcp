from mcp.server import MCPServer
from mcp.server.mcpserver import Context

from toyota_mcp.gateway import AppContext
from toyota_mcp.models import RefreshReport
from toyota_mcp.tools.base import READ_ONLY


def register(mcp: MCPServer) -> None:
    @mcp.tool(title="Refresh vehicle data", annotations=READ_ONLY)
    async def toyota_refresh_data(ctx: Context[AppContext]) -> RefreshReport:
        """Re-fetch status, telemetry and location from Toyota's cloud.

        Rarely needed — the car pushes new data only at ignition-off, so
        answers refresh themselves as the car is driven. Use only when the
        user just parked and wants the very latest position or status.
        This reads Toyota's cloud; it never wakes the car.
        """
        gateway = ctx.request_context.lifespan_context.gateway
        refreshed, note, freshness = await gateway.refresh()
        return RefreshReport(refreshed=refreshed, note=note, freshness=freshness)
