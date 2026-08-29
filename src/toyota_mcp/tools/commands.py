import time
from datetime import datetime
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations
from pydantic import Field
from pytoyoda.models.endpoints.command import CommandType

from toyota_mcp.gateway import AppContext, VehicleGateway
from toyota_mcp.models import (
    CONFIRMATION_NOTE,
    ClimateReport,
    CommandReport,
    LockState,
    NotificationItem,
    StatusReport,
    lock_state_of,
)

REVERSIBLE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
DESTRUCTIVE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
)
Confirm = Annotated[
    bool,
    Field(
        description=(
            "true sends the command to the car; false (default) only previews what would "
            "happen and sends nothing."
        )
    ),
]


def register(mcp: MCPServer) -> None:
    @mcp.tool(title="Lock the doors", annotations=REVERSIBLE)
    async def toyota_lock_doors(
        ctx: Context[AppContext], confirm: Confirm = False
    ) -> CommandReport:
        """Lock all doors and the trunk, then verify against the state the car reports.

        Only when the user explicitly asked to lock the car. Call with
        confirm=false first when in doubt; the report says whether the car
        confirmed the new state.
        """
        return await _door_command(ctx, CommandType.DOOR_LOCK, "locked", confirm)

    @mcp.tool(title="Unlock the doors", annotations=DESTRUCTIVE)
    async def toyota_unlock_doors(
        ctx: Context[AppContext], confirm: Confirm = False
    ) -> CommandReport:
        """Unlock all doors, then verify against the state the car reports.

        Security-sensitive: only when the user explicitly asked to unlock the
        car in this conversation, never on your own initiative. Preview with
        confirm=false unless the user already confirmed.
        """
        return await _door_command(ctx, CommandType.DOOR_UNLOCK, "unlocked", confirm)

    @mcp.tool(title="Start remote climate", annotations=DESTRUCTIVE)
    async def toyota_start_climate(
        ctx: Context[AppContext],
        confirm: Confirm = False,
        temperature_celsius: Annotated[
            float | None,
            Field(
                default=None,
                ge=15,
                le=30,
                multiple_of=0.5,
                description="Target cabin temperature; defaults to the saved preset.",
            ),
        ] = None,
    ) -> CommandReport:
        """Start remote pre-conditioning (heating or cooling) for the preset duration.

        On a hybrid this runs the engine — never start it in an enclosed
        space. Only when the user explicitly asked; preview with confirm=false
        unless the user already confirmed. Verified against the climate state
        the car reports.
        """
        gateway = ctx.request_context.lifespan_context.gateway
        if not confirm:
            bundle, freshness = await gateway.climate()
            current = ClimateReport.from_climate(bundle.status, bundle.settings, freshness)
            target = (
                f"{temperature_celsius} °C"
                if temperature_celsius is not None
                else "the saved preset"
            )
            return CommandReport(
                command="climate-start",
                status="needs_confirmation",
                detail=(
                    f"Would start remote climate at {target} (currently {current.status}). "
                    f"{CONFIRMATION_NOTE}"
                ),
                climate=current,
            )
        sent_at = await gateway.start_climate(temperature_celsius)
        return await _climate_outcome(gateway, "climate-start", True, sent_at)

    @mcp.tool(title="Stop remote climate", annotations=REVERSIBLE)
    async def toyota_stop_climate(
        ctx: Context[AppContext], confirm: Confirm = False
    ) -> CommandReport:
        """Stop remote pre-conditioning, then verify against the climate state the car reports.

        Only when the user explicitly asked; preview with confirm=false when in doubt.
        """
        gateway = ctx.request_context.lifespan_context.gateway
        if not confirm:
            bundle, freshness = await gateway.climate()
            current = ClimateReport.from_climate(bundle.status, bundle.settings, freshness)
            return CommandReport(
                command="climate-stop",
                status="needs_confirmation",
                detail=(
                    f"Would stop remote climate (currently {current.status}). {CONFIRMATION_NOTE}"
                ),
                climate=current,
            )
        sent_at = await gateway.stop_climate()
        return await _climate_outcome(gateway, "climate-stop", False, sent_at)


async def _door_command(
    ctx: Context[AppContext], command: CommandType, expected: LockState, confirm: bool
) -> CommandReport:
    gateway = ctx.request_context.lifespan_context.gateway
    name = str(command.value)
    before_bundle, before_freshness = await gateway.status()
    before = StatusReport.from_lock_status(
        before_bundle.lock_status, before_bundle.extras, before_freshness
    )
    if not confirm:
        return CommandReport(
            command=name,
            status="needs_confirmation",
            detail=(
                f"Would send '{name}' to the car (currently {before.all_locked}). "
                f"{CONFIRMATION_NOTE}"
            ),
            doors=before,
        )
    sent_at = await gateway.send_command(command)
    started = time.monotonic()
    bundle, freshness, ok = await gateway.wait_for_status(
        lambda current: lock_state_of(current.lock_status) == expected
    )
    after = StatusReport.from_lock_status(bundle.lock_status, bundle.extras, freshness)
    elapsed = round(time.monotonic() - started, 1)
    if ok:
        already = " (it already was)" if before.all_locked == expected else ""
        return CommandReport(
            command=name,
            status="verified",
            detail=f"The car reports its doors {expected}{already}.",
            doors=after,
            elapsed_seconds=elapsed,
        )
    return await _unverified(gateway, name, expected, sent_at, elapsed, doors=after)


async def _climate_outcome(
    gateway: VehicleGateway, name: str, expected_on: bool, sent_at: datetime
) -> CommandReport:
    started = time.monotonic()
    bundle, freshness, ok = await gateway.wait_for_climate(
        lambda current: current.status.is_on is expected_on
    )
    after = ClimateReport.from_climate(bundle.status, bundle.settings, freshness)
    elapsed = round(time.monotonic() - started, 1)
    if ok:
        return CommandReport(
            command=name,
            status="verified",
            detail=f"The car reports remote climate {after.status}.",
            climate=after,
            elapsed_seconds=elapsed,
        )
    expected = "running" if expected_on else "stopped"
    return await _unverified(gateway, name, expected, sent_at, elapsed, climate=after)


async def _unverified(
    gateway: VehicleGateway,
    name: str,
    expected: str,
    sent_at: datetime,
    elapsed: float,
    doors: StatusReport | None = None,
    climate: ClimateReport | None = None,
) -> CommandReport:
    failure = await gateway.remote_control_notification_since(sent_at)
    if failure is not None:
        reason = NotificationItem.from_notification(failure).message or "no reason given"
        return CommandReport(
            command=name,
            status="failed",
            detail=f"Toyota reported: {reason}",
            doors=doors,
            climate=climate,
            elapsed_seconds=elapsed,
        )
    return CommandReport(
        command=name,
        status="accepted",
        detail=(
            f"Toyota accepted '{name}' but the car has not reported '{expected}' within "
            f"{int(elapsed)}s — check again in a minute."
        ),
        doors=doors,
        climate=climate,
        elapsed_seconds=elapsed,
    )
