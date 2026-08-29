from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from toyota_mcp.gateway import AppContext, vehicle_label
from toyota_mcp.models import VehicleChoice, VehicleListReport
from toyota_mcp.tools.base import READ_ONLY

# Choosing a vehicle writes a preference; it reaches nothing outside the vehicle API.
CHOICE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)


def register(mcp: MCPServer) -> None:
    @mcp.tool(title="Vehicles on the account", annotations=READ_ONLY)
    async def toyota_list_vehicles(ctx: Context[AppContext]) -> VehicleListReport:
        """Every vehicle on the connected account, and which one the tools act on.

        Use for: which cars can I ask about? Choosing one is toyota_select_vehicle.
        """
        gateway = ctx.request_context.lifespan_context.gateway
        vehicles = await gateway.vehicles()
        return VehicleListReport.create(vehicles, gateway.selected_vin())

    @mcp.tool(title="Choose the vehicle to act on", annotations=CHOICE)
    async def toyota_select_vehicle(
        ctx: Context[AppContext],
        vin: Annotated[
            str,
            Field(
                description=(
                    "VIN of the vehicle, or the last four characters shown by toyota_list_vehicles."
                )
            ),
        ],
    ) -> VehicleChoice:
        """Point every other tool at one vehicle of the account, and remember it.

        Use when the account holds several vehicles, or to switch between them.
        """
        gateway = ctx.request_context.lifespan_context.gateway
        vehicles = await gateway.vehicles()
        wanted = vin.strip().upper().lstrip("…")
        matches = [
            vehicle
            for vehicle in vehicles
            if vehicle.vin
            and (vehicle.vin.upper() == wanted or vehicle.vin.upper().endswith(wanted))
        ]
        if len(matches) != 1 or not matches[0].vin:
            available = ", ".join(vehicle_label(vehicle) for vehicle in vehicles)
            raise ToolError(
                f"{'No vehicle matches' if not matches else 'Several vehicles match'} "
                f"{vin!r}. Available: {available}."
            )
        return VehicleChoice.create(await gateway.select(matches[0].vin))
