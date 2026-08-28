from mcp.server import MCPServer
from mcp.server.mcpserver import Context

from toyota_mcp.gateway import AppContext
from toyota_mcp.models import StatusReport
from toyota_mcp.tools.base import READ_ONLY


def register(mcp: MCPServer) -> None:
    @mcp.tool(title="Vehicle doors, windows and locks", annotations=READ_ONLY)
    async def toyota_get_status(ctx: Context[AppContext]) -> StatusReport:
        """Doors, windows, trunk, hood and lock state of the vehicle.

        Use for: is the car locked? are windows or doors open? Lock state is
        pushed by the car when parked and can lag — always cite
        freshness.vehicle_reported_at when answering. Warning lights are NOT
        here; use toyota_get_health for alerts and maintenance.
        """
        gateway = ctx.request_context.lifespan_context.gateway
        lock_status, freshness = await gateway.lock_status()
        return StatusReport.from_lock_status(lock_status, freshness)
