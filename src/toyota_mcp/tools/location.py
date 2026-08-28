from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError

from toyota_mcp import errors
from toyota_mcp.gateway import AppContext
from toyota_mcp.models import LocationReport
from toyota_mcp.tools.base import READ_ONLY


def register(mcp: MCPServer) -> None:
    @mcp.tool(title="Last parked position", annotations=READ_ONLY)
    async def toyota_get_location(ctx: Context[AppContext]) -> LocationReport:
        """Last parked position of the vehicle with a Google Maps link.

        Use for: where is the car? The position updates only when the car
        parks — while driving it shows the last parking spot; cite
        freshness.vehicle_reported_at when answering.
        """
        gateway = ctx.request_context.lifespan_context.gateway
        location, freshness = await gateway.location()
        if location.latitude is None or location.longitude is None:
            raise ToolError(errors.LOCATION_NEVER_REPORTED)
        return LocationReport.from_location(
            location.latitude, location.longitude, location, freshness
        )
