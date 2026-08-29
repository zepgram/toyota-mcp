from datetime import date, timedelta
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from toyota_mcp.gateway import AppContext
from toyota_mcp.models import LastTripReport, TripReport, TripsReport, TripSummaryReport
from toyota_mcp.tools.base import READ_ONLY


def register(mcp: MCPServer) -> None:
    @mcp.tool(title="Last trip details", annotations=READ_ONLY)
    async def toyota_get_last_trip(ctx: Context[AppContext]) -> LastTripReport:
        """Details of the most recent trip: distance, duration, consumption, EV share.

        Use for: how was the last trip? what did the last drive consume?
        """
        gateway = ctx.request_context.lifespan_context.gateway
        trip, freshness = await gateway.last_trip()
        if trip is None:
            raise ToolError("No trip has been recorded for this vehicle yet.")
        return LastTripReport(
            trip=TripReport.from_trip(trip, gateway.use_metric), freshness=freshness
        )

    @mcp.tool(title="Recent trips", annotations=READ_ONLY)
    async def toyota_get_trips(
        ctx: Context[AppContext],
        days: Annotated[
            int,
            Field(ge=1, le=92, description="Calendar days to cover, ending today (inclusive)."),
        ] = 7,
        limit: Annotated[
            int, Field(ge=1, le=50, description="Maximum number of trips to return.")
        ] = 10,
    ) -> TripsReport:
        """Individual trips over a recent window, newest first.

        Use for: list this week's trips, when did the car last drive?
        For averages over a period, prefer toyota_get_trip_summary.
        For windows beyond 92 days, use toyota_get_trip_summary.
        """
        gateway = ctx.request_context.lifespan_context.gateway
        window_from, window_to = _window(days)
        trips, freshness = await gateway.trips(window_from, window_to)
        return TripsReport.from_trips(
            trips, window_from, window_to, limit, gateway.use_metric, freshness
        )

    @mcp.tool(title="Driving statistics over a period", annotations=READ_ONLY)
    async def toyota_get_trip_summary(
        ctx: Context[AppContext],
        days: Annotated[
            int,
            Field(ge=1, le=365, description="Calendar days to cover, ending today (inclusive)."),
        ] = 7,
    ) -> TripSummaryReport:
        """Aggregated driving statistics over a rolling window ending today.

        Use for: average consumption over the last 7 days, EV-mode share this
        month, total distance this year. Consumption is recomputed over the
        whole window (total fuel vs total distance), not a mean of daily means.
        """
        gateway = ctx.request_context.lifespan_context.gateway
        window_from, window_to = _window(days)
        summaries, freshness = await gateway.daily_summaries(window_from, window_to)
        return TripSummaryReport.from_daily_summaries(
            summaries, window_from, window_to, gateway.use_metric, freshness
        )


def _window(days: int) -> tuple[date, date]:
    today = date.today()
    return today - timedelta(days=days - 1), today
