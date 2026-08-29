from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field
from pytoyoda.exceptions import ToyotaLoginError

from toyota_mcp.gateway import AppContext, vehicle_label
from toyota_mcp.models import SignInReport, SignInStart, VehicleChoice, VehicleListReport
from toyota_mcp.session import Session, SessionStore, authorization_code, authorize_url, exchange
from toyota_mcp.tools.base import READ_ONLY

# Signing in reaches Toyota's identity provider; choosing a vehicle only writes a preference.
SIGN_IN = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
)
CHOICE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)


def register(mcp: MCPServer) -> None:
    @mcp.tool(title="Connect a Toyota account", annotations=SIGN_IN)
    async def toyota_sign_in(ctx: Context[AppContext]) -> SignInStart:
        """Start connecting this server to a MyToyota or MyLexus Europe account.

        Give the user the returned link, ask them to sign in on Toyota's page,
        and tell them the browser will fail to open the address Toyota
        redirects to — that address is what they must copy back. Then call
        toyota_complete_sign_in with it. The password is never sent here.
        """
        return SignInStart.create(authorize_url())

    @mcp.tool(title="Finish connecting the Toyota account", annotations=SIGN_IN)
    async def toyota_complete_sign_in(
        ctx: Context[AppContext],
        redirect: Annotated[
            str,
            Field(
                description=(
                    "What Toyota redirected the browser to, starting with "
                    "com.toyota.oneapp:/ — or just the code it carries."
                )
            ),
        ],
    ) -> SignInReport:
        """Finish the sign-in with what Toyota redirected the browser to, then list the vehicles.

        Saves the session so it survives restarts; only a refresh token is kept.
        When the account holds several vehicles, follow with toyota_select_vehicle.
        """
        gateway = ctx.request_context.lifespan_context.gateway
        try:
            code = authorization_code(redirect)
        except ValueError as exc:
            raise ToolError(
                f"That is not a usable Toyota redirect ({exc}). Ask the user to copy the whole "
                "address the browser could not open, then call this tool again."
            ) from exc
        try:
            session = await exchange(code)
        except ToyotaLoginError as exc:
            raise ToolError(str(exc)) from exc
        store = SessionStore()
        store.save(session)
        gateway.reset()
        vehicles = await gateway.vehicles()
        if len(vehicles) == 1 and vehicles[0].vin:
            store.save(Session(session.username, session.refresh_token, vehicles[0].vin))
        return SignInReport.create(session.username, vehicles, store.location)

    @mcp.tool(title="Vehicles on the account", annotations=READ_ONLY)
    async def toyota_list_vehicles(ctx: Context[AppContext]) -> VehicleListReport:
        """Every vehicle on the connected account, and which one the tools act on.

        Use for: which cars can I ask about? Selecting one is toyota_select_vehicle.
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
        chosen = await gateway.select(matches[0].vin)
        return VehicleChoice.create(chosen)
