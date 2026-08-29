from mcp.server import MCPServer
from mcp.server.mcpserver import Context

from toyota_mcp.gateway import AppContext
from toyota_mcp.models import StatusReport
from toyota_mcp.tools.base import READ_ONLY


def register(mcp: MCPServer) -> None:
    @mcp.tool(title="Vehicle doors, windows and locks", annotations=READ_ONLY)
    async def toyota_get_status(ctx: Context[AppContext]) -> StatusReport:
        """Doors, windows, trunk, hood, lock state, lights and rear-seat reminder.

        Use for: is the car locked? are windows or doors open? did I leave the
        lights on? Lock state is
        pushed by the car when parked and can lag — always cite
        freshness.vehicle_reported_at when answering. Warning lights are NOT
        here; use toyota_get_health for alerts and maintenance.
        """
        gateway = ctx.request_context.lifespan_context.gateway
        bundle, freshness = await gateway.status()
        return StatusReport.from_lock_status(bundle.lock_status, bundle.extras, freshness)
