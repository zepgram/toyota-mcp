from __future__ import annotations

import argparse
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from html import escape
from typing import Any

from loguru import logger as upstream_logger
from mcp.server import MCPServer
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from pydantic import AnyHttpUrl, ValidationError
from pytoyoda.exceptions import ToyotaLoginError
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from toyota_mcp import __version__, login, probe, prompts
from toyota_mcp.config import ServerOptions, Settings
from toyota_mcp.gateway import AppContext, VehicleGateway
from toyota_mcp.oauth import (
    CONSENT_PATH,
    ERROR,
    PAGE,
    SCOPE,
    SIGN_IN_BODY,
    VEHICLE_BODY,
    VEHICLE_CHOICE,
    Grants,
    OwnerAuthorizationServer,
    state_file,
)
from toyota_mcp.opendata import OpenData
from toyota_mcp.places import Places
from toyota_mcp.session import (
    Session,
    SessionStore,
    authorization_code,
    authorize_url,
    exchange,
)
from toyota_mcp.tools import register_all

_BASE_INSTRUCTIONS = (
    "Access to a MyToyota or MyLexus Europe vehicle. Setup happens here, in the "
    "conversation: with no account connected, call toyota_sign_in, give the user the link, "
    "then toyota_complete_sign_in with the address Toyota redirected them to; when the "
    "account holds several vehicles, toyota_select_vehicle picks one and it is remembered. "
    "Every response carries a freshness block: the car uploads data when it parks, so cite "
    "vehicle_reported_at or age_seconds when the user asks about current state. "
)
INSTRUCTIONS = _BASE_INSTRUCTIONS + "Nothing here actuates the vehicle."
COMMANDS_INSTRUCTIONS = _BASE_INSTRUCTIONS + (
    "Remote commands (locks, trunk, lights, horn, windows, climate, charging) are "
    "available: send one only when the user explicitly asked for it in this conversation, "
    "never on your own initiative; call with confirm=false to preview, then confirm=true "
    "once the user agreed."
)


def create_server(
    gateway: VehicleGateway,
    opendata: OpenData | None = None,
    remote_commands: bool = True,
    places: Places | None = None,
    authorization: OwnerAuthorizationServer | None = None,
    auth_settings: AuthSettings | None = None,
) -> MCPServer:
    @asynccontextmanager
    async def lifespan(server: MCPServer) -> AsyncIterator[AppContext]:
        try:
            yield AppContext(gateway=gateway, opendata=opendata, places=places or Places())
        finally:
            await gateway.aclose()
            if opendata is not None:
                await opendata.aclose()

    mcp = MCPServer(
        "toyota",
        version=__version__,
        instructions=COMMANDS_INSTRUCTIONS if remote_commands else INSTRUCTIONS,
        lifespan=lifespan,
        log_level="WARNING",
        auth_server_provider=authorization,
        auth=auth_settings,
    )
    register_all(mcp, remote_commands=remote_commands)
    prompts.register(mcp)
    if authorization is not None:
        register_consent(mcp, authorization, gateway)
    return mcp


def register_consent(
    mcp: MCPServer, authorization: OwnerAuthorizationServer, gateway: VehicleGateway
) -> None:
    """Serve the page a client sends the owner to: sign in to Toyota, then pick a vehicle."""

    @mcp.custom_route(CONSENT_PATH, methods=["GET", "POST"])  # type: ignore[untyped-decorator]
    async def consent(request: Request) -> Response:
        request_id = request.query_params.get("request", "")
        client = authorization.pending_client(request_id)
        if client is None:
            return _page("This link has expired", "Start again from your MCP client.", "", 400)
        error = ""
        if request.method == "POST" and not authorization.is_signed_in(request_id):
            form = await request.form()
            error = await _sign_in(authorization, request_id, str(form.get("redirect", "")))
        elif request.method == "POST":
            form = await request.form()
            chosen = str(form.get("vin", ""))
            store = SessionStore()
            saved = store.load()
            if saved is not None and chosen:
                store.save(Session(saved.username, saved.refresh_token, chosen))
                gateway.reset()
            redirect = authorization.approve(request_id)
            if redirect is not None:
                return RedirectResponse(redirect, status_code=302)
            error = ERROR.format(message="That approval could not be completed — start again.")

        if not authorization.is_signed_in(request_id):
            return _page(
                "Connect your Toyota",
                f"{escape(client)} is asking to read your vehicle and send remote commands.",
                SIGN_IN_BODY.format(sign_in_url=escape(authorize_url())),
                error=error,
            )
        vehicles = await gateway.vehicles()
        if len(vehicles) <= 1:
            redirect = authorization.approve(request_id)
            if redirect is not None:
                return RedirectResponse(redirect, status_code=302)
        return _page(
            "Choose the vehicle",
            f"{escape(client)} will act on the vehicle you pick.",
            VEHICLE_BODY.format(choices=_choices(vehicles)),
            error=error,
        )


async def _sign_in(authorization: OwnerAuthorizationServer, request_id: str, redirect: str) -> str:
    try:
        code = authorization_code(redirect)
    except ValueError as exc:
        return ERROR.format(message=f"That is not the address Toyota redirected to ({exc}).")
    try:
        session = await exchange(code)
    except ToyotaLoginError as exc:
        return ERROR.format(message=str(exc))
    store = SessionStore()
    known = store.load()
    if known is not None and known.username and session.username != known.username:
        return ERROR.format(message="This server is already connected to another Toyota account.")
    store.save(Session(session.username, session.refresh_token, known.vin if known else None))
    authorization.mark_signed_in(request_id)
    return ""


def _choices(vehicles: list[Any]) -> str:
    from toyota_mcp.gateway import vehicle_label

    saved = SessionStore().load()
    chosen = saved.vin if saved else None
    return "".join(
        VEHICLE_CHOICE.format(
            vin=escape(vehicle.vin or ""),
            name=escape(vehicle_label(vehicle).split(" (")[0]),
            model=escape(
                str(getattr(getattr(vehicle, "_vehicle_info", None), "car_model_name", "") or "")
            ),
            suffix=escape((vehicle.vin or "????")[-4:]),
            checked="checked" if vehicle.vin == chosen else "",
        )
        for vehicle in vehicles
    )


def _page(title: str, intro: str, body: str, status: int = 200, error: str = "") -> Response:
    return HTMLResponse(
        PAGE.format(title=escape(title), intro=intro, body=body, error=error), status_code=status
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="toyota-mcp",
        description=(
            "MCP server for MyToyota Europe. Sign in once with `toyota-mcp login` (your "
            "password stays with Toyota); TOYOTA_USERNAME / TOYOTA_PASSWORD remain as a "
            "headless fallback. Features are options."
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="register only the read tools — no lock, trunk, lights, climate or charging commands",
    )
    parser.add_argument(
        "--addresses",
        choices=("off", "osm", "fr"),
        default="off",
        help=(
            "turn coordinates into addresses: osm (OpenStreetMap, worldwide) or fr (French "
            "address base, also enables fuel prices); off by default because it sends the "
            "car's position to that service"
        ),
    )
    parser.add_argument(
        "--places",
        type=_places,
        default=Places(),
        metavar="SPEC",
        help='named places within 200 m, e.g. "home=43.6045,1.4440;work=43.6290,1.3630"',
    )
    remote = parser.add_argument_group("remote access (streamable HTTP + OAuth)")
    remote.add_argument(
        "--http",
        metavar="URL",
        help=(
            "serve over HTTP at this public URL instead of stdio, protected by OAuth: "
            "connecting a client signs you in to Toyota and lets you pick a vehicle, "
            "e.g. https://toyota.example.com"
        ),
    )
    remote.add_argument("--host", default="127.0.0.1", help="address to bind (default 127.0.0.1)")
    remote.add_argument("--port", type=int, default=8787, help="port to bind (default 8787)")
    commands = parser.add_subparsers(dest="subcommand", metavar="{login,logout,doctor,probe}")
    commands.add_parser(
        "login", help="sign in through your browser and save the session (no password stored)"
    )
    commands.add_parser("logout", help="forget the saved session")
    doctor = commands.add_parser(
        "doctor", help="check credentials, vehicles and which tools this car supports"
    )
    doctor.add_argument(
        "--dump",
        action="store_true",
        help="write redacted raw payloads to ./toyota-mcp-doctor-dump/",
    )
    probe.add_arguments(
        commands.add_parser("probe", help="send one raw remote command and print Toyota's answer")
    )
    return parser


def _places(spec: str) -> Places:
    try:
        return Places.parse(spec)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def silence_upstream_debug_logs() -> None:
    # pytoyoda logs full HTTP exchanges at DEBUG: credentials, tokens and GPS coordinates.
    upstream_logger.remove()
    upstream_logger.add(sys.stderr, level="WARNING")


def load_settings() -> Settings:
    try:
        settings = Settings()
    except ValidationError as exc:
        for error in exc.errors():
            variable = "TOYOTA_" + "_".join(str(part) for part in error["loc"]).upper()
            print(f"{variable}: {error['msg']}", file=sys.stderr)
        raise SystemExit(2) from exc
    if settings.password is None and SessionStore().load() is None:
        print(
            "No Toyota account connected yet — connect one from your MCP client with "
            "toyota_sign_in, or from a terminal with `toyota-mcp login`.",
            file=sys.stderr,
        )
    return settings


def main() -> None:
    silence_upstream_debug_logs()
    args = build_parser().parse_args()
    options = ServerOptions(read_only=args.read_only, addresses=args.addresses, places=args.places)
    if args.subcommand == "login":
        raise SystemExit(login.run())
    if args.subcommand == "logout":
        raise SystemExit(login.logout())
    if args.subcommand == "doctor":
        from toyota_mcp import doctor

        raise SystemExit(doctor.run(dump=args.dump, options=options))
    if args.subcommand == "probe":
        raise SystemExit(probe.execute(args))
    settings = load_settings()
    opendata = OpenData(options.addresses) if options.addresses != "off" else None
    gateway = VehicleGateway(settings)
    if not args.http:
        create_server(gateway, opendata, not options.read_only, options.places).run()
        return
    public_url = str(args.http).rstrip("/")
    if not public_url.startswith("https://") and not _is_local(public_url):
        print(
            "Refusing to serve a public URL over plain HTTP: bearer tokens would travel in "
            "clear. Put a TLS-terminating proxy in front and pass its https:// address.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    server = create_server(
        gateway,
        opendata,
        not options.read_only,
        options.places,
        authorization=OwnerAuthorizationServer(Grants.load(state_file())),
        auth_settings=AuthSettings(
            issuer_url=AnyHttpUrl(public_url),
            resource_server_url=AnyHttpUrl(f"{public_url}/mcp"),
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
            ),
        ),
    )
    print(f"Serving {public_url}/mcp — add that URL in your MCP client.", file=sys.stderr)
    print("Connecting a client signs in to Toyota; no other secret is needed.", file=sys.stderr)
    server.run("streamable-http", host=args.host, port=args.port)


def _is_local(url: str) -> bool:
    return url.startswith(("http://localhost", "http://127.0.0.1", "http://[::1]"))


if __name__ == "__main__":
    main()
