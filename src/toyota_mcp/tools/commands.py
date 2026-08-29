from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations
from pydantic import Field

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
    StatusReport,
    lock_state_of,
    trunk_lock_state,
    windows_state,
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


@dataclass(frozen=True)
class CommandSpec:
    """One remote command: a spec plus its evidence row in docs/architecture.md is a tool."""

    tool: str
    title: str
    command: str
    destructive: bool
    effect: str
    outcome: str
    description: str
    state: Callable[[StatusBundle], str] | None = None
    expected: str | None = None

    def already(self, before: StatusBundle) -> bool:
        return self.state is not None and self.state(before) == self.expected

    def verified(self, before: StatusBundle, after: StatusBundle) -> bool:
        newer = after.extras.is_newer_than(before.extras)
        if self.state is None:
            return newer
        return self.state(after) == self.expected and (not self.already(before) or newer)


def _doors(bundle: StatusBundle) -> str:
    return lock_state_of(bundle.lock_status, doors_only=True)


def _trunk(bundle: StatusBundle) -> str:
    return trunk_lock_state(bundle.lock_status)


def _windows(bundle: StatusBundle) -> str:
    return windows_state(bundle.lock_status)


STATUS_COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec(
        tool="toyota_lock_doors",
        title="Lock the doors",
        command="door-lock",
        destructive=False,
        effect="lock the doors",
        outcome="doors locked",
        description=(
            "Lock the doors, then verify against the state the car reports.\n\n"
            "Only when the user explicitly asked to lock the car. Call with confirm=false "
            "first when in doubt; the report says whether the car confirmed the new state."
        ),
        state=_doors,
        expected="locked",
    ),
    CommandSpec(
        tool="toyota_unlock_doors",
        title="Unlock the doors",
        command="door-unlock",
        destructive=True,
        effect="unlock the doors",
        outcome="doors unlocked",
        description=(
            "Unlock the doors, then verify against the state the car reports.\n\n"
            "Security-sensitive: only when the user explicitly asked to unlock the car in "
            "this conversation, never on your own initiative. Preview with confirm=false "
            "unless the user already confirmed."
        ),
        state=_doors,
        expected="unlocked",
    ),
    CommandSpec(
        tool="toyota_lock_trunk",
        title="Lock the trunk",
        command="trunk-lock",
        destructive=False,
        effect="lock the trunk",
        outcome="trunk locked",
        description="Lock the trunk only, then verify against the state the car reports.",
        state=_trunk,
        expected="locked",
    ),
    CommandSpec(
        tool="toyota_unlock_trunk",
        title="Unlock the trunk",
        command="trunk-unlock",
        destructive=True,
        effect="unlock the trunk",
        outcome="trunk unlocked",
        description=(
            "Unlock the trunk only (doors stay locked), then verify against the state the "
            "car reports. Only on the user's explicit request; preview with confirm=false "
            "unless the user already confirmed."
        ),
        state=_trunk,
        expected="unlocked",
    ),
    CommandSpec(
        tool="toyota_find_car",
        title="Flash the hazard lights",
        command="hazard-on",
        destructive=False,
        effect="flash the hazard lights for a few seconds",
        outcome="hazard lights flashed",
        description=(
            "Flash the hazard lights briefly so the user can spot the car — silent. "
            "Verified by the car reporting back."
        ),
    ),
    CommandSpec(
        tool="toyota_sound_horn",
        title="Sound the horn briefly",
        command="buzzer-warning",
        destructive=False,
        effect="sound a short horn signal",
        outcome="horn sounded",
        description=(
            "Sound a short horn signal to locate the car — audible to everyone around it; "
            "prefer toyota_find_car unless the user asked for sound. Verified by the car "
            "reporting back."
        ),
    ),
    CommandSpec(
        tool="toyota_close_windows",
        title="Close the windows",
        command="power-window-close",
        destructive=False,
        effect="close all windows",
        outcome="windows closed",
        description=(
            "Close all power windows, then verify against the state the car reports. "
            "Not every model supports it; Toyota answers 'vehicle not supported' when not."
        ),
        state=_windows,
        expected="closed",
    ),
)


def register(mcp: MCPServer) -> None:
    for spec in STATUS_COMMANDS:
        _register_status_command(mcp, spec)

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

    @mcp.tool(title="Wake the car and re-read its state", annotations=REVERSIBLE)
    async def toyota_wake_vehicle(ctx: Context[AppContext]) -> CommandReport:
        """Ask the car to report its current state right now, then re-read doors and climate.

        The passive reads only see what the car pushed when it parked; this
        sends the same wake request the MyToyota app uses. It costs the car a
        little 12 V battery and cellular time — use it when the user needs the
        state as of now, not routinely.
        """
        gateway = ctx.request_context.lifespan_context.gateway
        started = time.monotonic()
        _, after, freshness, reported = await gateway.wake()
        elapsed = round(time.monotonic() - started, 1)
        doors = (
            StatusReport.from_lock_status(after.lock_status, after.extras, freshness)
            if after is not None and freshness is not None
            else None
        )
        detail = (
            "The car reported its current state."
            if reported
            else f"The car has not answered the wake request within {int(elapsed)}s; "
            "the last known state is shown."
        )
        return CommandReport(
            command="wake",
            status="verified" if reported else "accepted",
            detail=detail,
            doors=doors,
            elapsed_seconds=elapsed,
        )


def _register_status_command(mcp: MCPServer, spec: CommandSpec) -> None:
    async def tool(ctx: Context[AppContext], confirm: Confirm = False) -> CommandReport:
        return await _status_command(ctx.request_context.lifespan_context.gateway, spec, confirm)

    tool.__name__ = spec.tool
    tool.__doc__ = spec.description
    mcp.tool(
        name=spec.tool,
        title=spec.title,
        annotations=DESTRUCTIVE if spec.destructive else REVERSIBLE,
    )(tool)


async def _status_command(
    gateway: VehicleGateway, spec: CommandSpec, confirm: bool
) -> CommandReport:
    before, before_freshness = await gateway.status()
    before_report = StatusReport.from_lock_status(
        before.lock_status, before.extras, before_freshness
    )
    if not confirm:
        return CommandReport(
            command=spec.command,
            status="needs_confirmation",
            detail=f"Would {spec.effect} (doors currently {before_report.all_locked}). "
            f"{CONFIRMATION_NOTE}",
            doors=before_report,
        )
    try:
        await gateway.send_command(spec.command)
    except CommandRejected as exc:
        return CommandReport(
            command=spec.command, status="failed", detail=str(exc), doors=before_report
        )
    started = time.monotonic()
    after, freshness, verified = await gateway.wait_for_status(
        lambda now: spec.verified(before, now)
    )
    elapsed = round(time.monotonic() - started, 1)
    doors = (
        StatusReport.from_lock_status(after.lock_status, after.extras, freshness)
        if after is not None and freshness is not None
        else None
    )
    if verified:
        detail = f"The car confirms: {spec.outcome}."
    else:
        detail = UNVERIFIED_HINT.format(seconds=int(elapsed), read_tool="toyota_get_status")
        if spec.already(before):
            detail = f"The car already reported '{spec.outcome}' before the command. {detail}"
    return CommandReport(
        command=spec.command,
        status="verified" if verified else "accepted",
        detail=detail,
        doors=doors,
        elapsed_seconds=elapsed,
    )


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
