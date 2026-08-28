from mcp.server import MCPServer
from mcp.server.mcpserver import Context

from toyota_mcp.gateway import AppContext
from toyota_mcp.models import (
    SERVICE_NOTE,
    WARNING_LIGHTS_CAVEAT,
    HealthReport,
    NotificationItem,
    ServiceRecord,
)
from toyota_mcp.tools.base import READ_ONLY


def register(mcp: MCPServer) -> None:
    @mcp.tool(title="Vehicle health and maintenance", annotations=READ_ONLY)
    async def toyota_get_health(ctx: Context[AppContext]) -> HealthReport:
        """Active warning lights, recent notifications and last recorded service.

        Use for: any alerts on the car? when was it last serviced? Toyota
        exposes service history, not upcoming maintenance deadlines.
        """
        gateway = ctx.request_context.lifespan_context.gateway
        bundle, freshness = await gateway.health()
        notifications = sorted(
            bundle.notifications,
            key=lambda item: item.date.timestamp() if item.date else float("-inf"),
            reverse=True,
        )[:10]
        service_note = SERVICE_NOTE
        if not bundle.service_history_enabled:
            service_note = f"Service history is not enabled for this account. {SERVICE_NOTE}"
        return HealthReport(
            warning_lights=bundle.warning_lights,
            warning_lights_caveat=WARNING_LIGHTS_CAVEAT,
            notifications=[
                NotificationItem.from_notification(notification) for notification in notifications
            ],
            last_service=(
                ServiceRecord.from_service_history(bundle.latest_service, gateway.use_metric)
                if bundle.latest_service
                else None
            ),
            service_note=service_note,
            freshness=freshness,
        )
