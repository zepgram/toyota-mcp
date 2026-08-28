from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError

from toyota_mcp.gateway import AppContext
from toyota_mcp.models import EnergyReport, OdometerReport, Quantity
from toyota_mcp.tools.base import READ_ONLY


def register(mcp: MCPServer) -> None:
    @mcp.tool(title="Fuel, range and battery", annotations=READ_ONLY)
    async def toyota_get_energy(ctx: Context[AppContext]) -> EnergyReport:
        """Remaining fuel, driving range and (when applicable) battery state.

        Use for: how much range is left? how full is the tank? is it charging?
        On self-charging full hybrids Toyota exposes no battery data — the
        response says so explicitly in battery_note instead of returning nulls.
        """
        gateway = ctx.request_context.lifespan_context.gateway
        dashboard, freshness = await gateway.dashboard()
        powertrain = await gateway.powertrain()
        return EnergyReport.from_dashboard(dashboard, powertrain, freshness)

    @mcp.tool(title="Odometer reading", annotations=READ_ONLY)
    async def toyota_get_odometer(ctx: Context[AppContext]) -> OdometerReport:
        """Total distance on the odometer.

        Use for: how many kilometers/miles are on the car?
        """
        gateway = ctx.request_context.lifespan_context.gateway
        dashboard, freshness = await gateway.dashboard()
        odometer = dashboard.odometer_with_unit
        if odometer is None or odometer.value is None or odometer.unit is None:
            raise ToolError("The vehicle did not report an odometer reading.")
        return OdometerReport(
            odometer=Quantity(value=float(odometer.value), unit=str(odometer.unit)),
            freshness=freshness,
        )
