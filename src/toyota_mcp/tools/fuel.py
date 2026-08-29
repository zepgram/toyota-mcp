from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from toyota_mcp import errors
from toyota_mcp.gateway import AppContext
from toyota_mcp.models import LOCATION_NOTE, OPEN_DATA_DISABLED, FuelStationsReport, TripPlace
from toyota_mcp.opendata import FUEL_PRICES_SOURCE, FuelKind, OpenDataUnavailable
from toyota_mcp.tools.base import READ_ONLY


def register(mcp: MCPServer) -> None:
    @mcp.tool(title="Cheapest fuel stations near the car", annotations=READ_ONLY)
    async def toyota_find_fuel_stations(
        ctx: Context[AppContext],
        fuel: Annotated[
            FuelKind,
            Field(description="Fuel to price: e10, sp95, sp98, e85, gazole (diesel) or gplc."),
        ] = "e10",
        radius_km: Annotated[int, Field(ge=1, le=50, description="Search radius in km.")] = 5,
        limit: Annotated[int, Field(ge=1, le=10, description="Number of stations.")] = 5,
    ) -> FuelStationsReport:
        """Cheapest stations selling a given fuel around the car's last parked position.

        Use for: where can I fill up cheaply near the car? France only —
        prices are self-reported by stations through the French government
        open-data feed. Requires TOYOTA_OPEN_DATA=fr.
        """
        context = ctx.request_context.lifespan_context
        if context.opendata is None:
            raise ToolError(OPEN_DATA_DISABLED)
        location, freshness = await context.gateway.location()
        if location.latitude is None or location.longitude is None:
            raise ToolError(errors.LOCATION_NEVER_REPORTED)
        try:
            stations = await context.opendata.fuel_stations(
                location.latitude, location.longitude, radius_km, fuel, limit
            )
        except OpenDataUnavailable as exc:
            raise ToolError(str(exc)) from exc
        note = FUEL_PRICES_SOURCE
        if not stations:
            note = f"No station selling {fuel} within {radius_km} km of the car. {note}"
        return FuelStationsReport(
            fuel=fuel,
            radius_km=radius_km,
            around=TripPlace(
                latitude=location.latitude,
                longitude=location.longitude,
                address=await context.opendata.reverse_geocode(
                    location.latitude, location.longitude
                ),
            ),
            stations=stations,
            note=note,
            freshness=freshness.with_vehicle_time(location.timestamp, LOCATION_NOTE),
        )
