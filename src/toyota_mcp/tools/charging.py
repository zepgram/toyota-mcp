from mcp.server import MCPServer
from mcp.server.mcpserver import Context

from toyota_mcp.gateway import AppContext
from toyota_mcp.models import ChargingReport
from toyota_mcp.tools.base import READ_ONLY


def register(mcp: MCPServer) -> None:
    @mcp.tool(title="Charging state and schedules", annotations=READ_ONLY)
    async def toyota_get_charging(ctx: Context[AppContext]) -> ChargingReport:
        """Plug-in battery: charge level, charging status, EV range, time to full, schedules.

        Use for: is it charging? how much EV range? when is the next scheduled
        charge? Plug-in hybrids and electric vehicles only — other powertrains
        get an explicit "not applicable" error (use toyota_get_energy).
        """
        gateway = ctx.request_context.lifespan_context.gateway
        status, freshness = await gateway.charging()
        return ChargingReport.from_electric_status(status, await gateway.powertrain(), freshness)
