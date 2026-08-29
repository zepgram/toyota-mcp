import asyncio
from datetime import date, timedelta
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from toyota_mcp.gateway import AppContext
from toyota_mcp.models import (
    CalendarPeriod,
    LastTripReport,
    TripReport,
    TripsReport,
    TripSummaryReport,
)
from toyota_mcp.opendata import OpenData
from toyota_mcp.places import Places
from toyota_mcp.tools.base import READ_ONLY


def register(mcp: MCPServer) -> None:
    @mcp.tool(title="Last trip details", annotations=READ_ONLY)
    async def toyota_get_last_trip(ctx: Context[AppContext]) -> LastTripReport:
        """Most recent trip: distance, duration, consumption, hybrid mode split, places.

        Use for: how was the last trip? what did the last drive consume?
        Start/end addresses need TOYOTA_OPEN_DATA (osm or fr).
        """
        context = ctx.request_context.lifespan_context
        trip, freshness = await context.gateway.last_trip()
        if trip is None:
            raise ToolError("No trip has been recorded for this vehicle yet.")
        report = TripReport.from_trip(trip, context.gateway.use_metric)
        await _annotate_places([report], context.opendata, context.places)
        return LastTripReport(trip=report, freshness=freshness)

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
        context = ctx.request_context.lifespan_context
        window_from, window_to = _window(days)
        trips, freshness = await context.gateway.trips(window_from, window_to)
        report = TripsReport.from_trips(
            trips, window_from, window_to, limit, context.gateway.use_metric, freshness
        )
        await _annotate_places(report.trips, context.opendata, context.places)
        return report

    @mcp.tool(title="Driving statistics over a period", annotations=READ_ONLY)
    async def toyota_get_trip_summary(
        ctx: Context[AppContext],
        days: Annotated[
            int,
            Field(ge=1, le=365, description="Calendar days to cover, ending today (inclusive)."),
        ] = 7,
        period: Annotated[
            CalendarPeriod | None,
            Field(
                default=None,
                description=(
                    "Calendar period as shown in the MyToyota app (today, this_week, "
                    "this_month, this_year); overrides days."
                ),
            ),
        ] = None,
    ) -> TripSummaryReport:
        """Aggregated driving statistics over a rolling window or a calendar period.

        Use for: average consumption over the last 7 days, EV-mode share this
        month, total distance this year. Consumption is recomputed over the
        whole window (total fuel vs total distance), not a mean of daily means.
        Pass period to match the app's calendar figures.
        """
        gateway = ctx.request_context.lifespan_context.gateway
        window_from, window_to = _calendar_window(period) if period else _window(days)
        summaries, freshness = await gateway.daily_summaries(window_from, window_to)
        report = TripSummaryReport.from_daily_summaries(
            summaries, window_from, window_to, gateway.use_metric, freshness
        )
        return report.model_copy(update={"period": period})


async def _annotate_places(
    reports: list[TripReport], opendata: OpenData | None, places: Places
) -> None:
    targets = [
        (place, place.latitude, place.longitude)
        for report in reports
        for place in (report.start, report.end)
        if place is not None and place.latitude is not None and place.longitude is not None
    ]
    for place, latitude, longitude in targets:
        place.place = places.match(latitude, longitude)
    if opendata is None:
        return
    addresses = await asyncio.gather(
        *(opendata.reverse_geocode(latitude, longitude) for _, latitude, longitude in targets)
    )
    for (place, _, _), address in zip(targets, addresses, strict=True):
        place.address = address


def _calendar_window(period: CalendarPeriod) -> tuple[date, date]:
    today = date.today()
    if period == "today":
        return today, today
    if period == "this_week":
        return today - timedelta(days=today.weekday()), today
    if period == "this_month":
        return today.replace(day=1), today
    return today.replace(month=1, day=1), today


def _window(days: int) -> tuple[date, date]:
    today = date.today()
    return today - timedelta(days=days - 1), today
