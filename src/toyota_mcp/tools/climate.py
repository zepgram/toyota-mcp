from mcp.server import MCPServer
from mcp.server.mcpserver import Context

from toyota_mcp.gateway import AppContext
from toyota_mcp.models import ClimateReport
from toyota_mcp.tools.base import READ_ONLY


def register(mcp: MCPServer) -> None:
    @mcp.tool(title="Remote climate state and preset", annotations=READ_ONLY)
    async def toyota_get_climate(ctx: Context[AppContext]) -> ClimateReport:
        """Remote pre-conditioning state (running or stopped, temperatures) and the saved
        preset a remote start would apply (target temperature, duration, defrosters,
        heated seats).

        Use for: is the climate running? what temperature is the preset? This
        server cannot start or stop it.
        """
        gateway = ctx.request_context.lifespan_context.gateway
        bundle, freshness = await gateway.climate()
        return ClimateReport.from_climate(bundle.status, bundle.settings, freshness)
