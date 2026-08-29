from mcp.server import MCPServer
from mcp.server.mcpserver import Context

from toyota_mcp.gateway import AppContext
from toyota_mcp.models import VehicleInfoReport
from toyota_mcp.tools.base import READ_ONLY


def register(mcp: MCPServer) -> None:
    @mcp.tool(title="Vehicle identity and capabilities", annotations=READ_ONLY)
    async def toyota_get_vehicle_info(ctx: Context[AppContext]) -> VehicleInfoReport:
        """Model, year, plate, colour, first-use date, connected-services subscriptions and
        which remote capabilities Toyota declares for this car.

        Use for: what car is this? when was it registered? is the connected
        subscription still active? can it be locked remotely? Capability flags
        are indicative only — see remote_capabilities.note.
        """
        gateway = ctx.request_context.lifespan_context.gateway
        vehicle = await gateway.vehicle()
        return VehicleInfoReport.from_vehicle(vehicle, await gateway.powertrain())
