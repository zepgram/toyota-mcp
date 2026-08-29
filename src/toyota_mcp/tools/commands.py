import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations
from pydantic import Field
from pytoyoda.models.endpoints.command import CommandType

from toyota_mcp.gateway import (
    AppContext,
    ClimateBundle,
    CommandRejected,
    StatusBundle,
    VehicleGateway,
)
from toyota_mcp.models import (
    CONFIRMATION_NOTE,
    ClimateReport,
    CommandReport,
    Freshness,
    LockState,
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
UNVERIFIED_HINT = (
    "Toyota accepted the command but the car has not reported a change within {seconds}s. "
    "Check again with {read_tool} in a minute; toyota_get_health lists any message Toyota "
    "sent about it."
)


def register(mcp: MCPServer) -> None:
    @mcp.tool(title="Lock the doors", annotations=REVERSIBLE)
    async def toyota_lock_doors(
        ctx: Context[AppContext], confirm: Confirm = False
    ) -> CommandReport:
        """Lock the doors, then verify against the state the car reports.

        Only when the user explicitly asked to lock the car. Call with
        confirm=false first when in doubt; the report says whether the car
        confirmed the new state.
        """
        return await _door_command(ctx, CommandType.DOOR_LOCK, "locked", confirm)

    @mcp.tool(title="Unlock the doors", annotations=DESTRUCTIVE)
    async def toyota_unlock_doors(
        ctx: Context[AppContext], confirm: Confirm = False
    ) -> CommandReport:
        """Unlock the doors, then verify against the state the car reports.

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
        """Start remote pre-conditioning with the saved preset (duration, defrosters, seats).

        On a hybrid this runs the engine — never start it in an enclosed
        space. Only when the user explicitly asked; preview with confirm=false
        unless the user already confirmed. Verified against the climate state
        the car reports.
        """
        gateway = ctx.request_context.lifespan_context.gateway
        target = f"{temperature_celsius} °C" if temperature_celsius else "the saved preset"
        return await _climate_command(
            gateway,
            "climate-start",
            expected_on=True,
            confirm=confirm,
            preview=f"Would start remote climate at {target}",
            send=lambda: gateway.start_climate(temperature_celsius),
        )

    @mcp.tool(title="Stop remote climate", annotations=REVERSIBLE)
    async def toyota_stop_climate(
        ctx: Context[AppContext], confirm: Confirm = False
    ) -> CommandReport:
        """Stop remote pre-conditioning, then verify against the climate state the car reports.

        Only when the user explicitly asked; preview with confirm=false when in doubt.
        """
        gateway = ctx.request_context.lifespan_context.gateway
        return await _climate_command(
            gateway,
            "climate-stop",
            expected_on=False,
            confirm=confirm,
            preview="Would stop remote climate",
            send=gateway.stop_climate,
        )


async def _door_command(
    ctx: Context[AppContext], command: CommandType, expected: LockState, confirm: bool
) -> CommandReport:
    gateway = ctx.request_context.lifespan_context.gateway
    name = str(command.value)
    before, before_freshness = await gateway.status()
    before_report = StatusReport.from_lock_status(
        before.lock_status, before.extras, before_freshness
    )
    if not confirm:
        return CommandReport(
            command=name,
            status="needs_confirmation",
            detail=f"Would send '{name}' (doors currently {before_report.all_locked}). "
            f"{CONFIRMATION_NOTE}",
            doors=before_report,
        )
    try:
        await gateway.send_door_command(command)
    except CommandRejected as exc:
        return CommandReport(command=name, status="failed", detail=str(exc), doors=before_report)
    started = time.monotonic()
    after, freshness, verified = await gateway.wait_for_status(_doors_changed(before, expected))
    elapsed = round(time.monotonic() - started, 1)
    doors = (
        StatusReport.from_lock_status(after.lock_status, after.extras, freshness)
        if after is not None and freshness is not None
        else None
    )
    if verified:
        detail = f"The car reports its doors {expected}."
    else:
        detail = UNVERIFIED_HINT.format(seconds=int(elapsed), read_tool="toyota_get_status")
        if lock_state_of(before.lock_status, doors_only=True) == expected:
            detail = f"The doors were already reported {expected} before the command. {detail}"
    return CommandReport(
        command=name,
        status="verified" if verified else "accepted",
        detail=detail,
        doors=doors,
        elapsed_seconds=elapsed,
    )


def _doors_changed(before: StatusBundle, expected: LockState) -> Callable[[StatusBundle], bool]:
    before_state = lock_state_of(before.lock_status, doors_only=True)
    before_at = before.lock_status.last_updated

    def satisfied(after: StatusBundle) -> bool:
        if lock_state_of(after.lock_status, doors_only=True) != expected:
            return False
        after_at = after.lock_status.last_updated
        return before_state != expected or (
            after_at is not None and before_at is not None and after_at > before_at
        )

    return satisfied


async def _climate_command(
    gateway: VehicleGateway,
    name: str,
    expected_on: bool,
    confirm: bool,
    preview: str,
    send: Callable[[], Awaitable[datetime]],
) -> CommandReport:
    before, before_freshness = await gateway.climate()
    before_report = ClimateReport.from_climate(before.status, before.settings, before_freshness)
    if not confirm:
        return CommandReport(
            command=name,
            status="needs_confirmation",
            detail=f"{preview} (currently {before_report.status}). {CONFIRMATION_NOTE}",
            climate=before_report,
        )
    try:
        await send()
    except CommandRejected as exc:
        return CommandReport(command=name, status="failed", detail=str(exc), climate=before_report)
    started = time.monotonic()
    already = before.status.is_on is expected_on
    after: ClimateBundle | None
    freshness: Freshness | None
    if already and before.status.updated_at is None:
        # Toyota's climate status carries no timestamp: an unchanged state can never be re-verified.
        after, freshness, verified = before, before_freshness, False
    else:
        after, freshness, verified = await gateway.wait_for_climate(
            _climate_changed(before, expected_on)
        )
    elapsed = round(time.monotonic() - started, 1)
    climate = (
        ClimateReport.from_climate(after.status, before.settings, freshness)
        if after is not None and freshness is not None
        else None
    )
    expected = "running" if expected_on else "stopped"
    if verified:
        detail = f"The car reports remote climate {climate.status if climate else expected}."
    else:
        detail = UNVERIFIED_HINT.format(seconds=int(elapsed), read_tool="toyota_get_climate")
        if already:
            detail = f"Remote climate was already reported {expected} before the command. {detail}"
    return CommandReport(
        command=name,
        status="verified" if verified else "accepted",
        detail=detail,
        climate=climate,
        elapsed_seconds=elapsed,
    )


def _climate_changed(before: ClimateBundle, expected_on: bool) -> Callable[[ClimateBundle], bool]:
    before_at = before.status.updated_at

    def satisfied(after: ClimateBundle) -> bool:
        if after.status.is_on is not expected_on:
            return False
        after_at = after.status.updated_at
        return before.status.is_on is not expected_on or (
            after_at is not None and before_at is not None and after_at > before_at
        )

    return satisfied
