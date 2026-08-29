from __future__ import annotations

import argparse
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from html import escape

import anyio
from loguru import logger as upstream_logger
from mcp.server import MCPServer
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from pydantic import AnyHttpUrl, ValidationError
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from toyota_mcp import __version__, login, probe, prompts
from toyota_mcp.config import ServerOptions, Settings
from toyota_mcp.gateway import AppContext, VehicleGateway
from toyota_mcp.oauth import (
    CONSENT_ERROR,
    CONSENT_PAGE,
    CONSENT_PATH,
    SCOPE,
    WRONG_CODE_DELAY,
    Grants,
    OwnerAuthorizationServer,
    generate_access_code,
    state_file,
)
from toyota_mcp.opendata import OpenData
from toyota_mcp.places import Places
from toyota_mcp.session import NO_CREDENTIALS, SessionStore
from toyota_mcp.tools import register_all

_BASE_INSTRUCTIONS = (
    "Access to a MyToyota Europe vehicle. Every response carries a freshness block: "
    "the car uploads data when it parks, so cite vehicle_reported_at or age_seconds "
    "when the user asks about current state. "
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
        register_consent(mcp, authorization)
    return mcp


def register_consent(mcp: MCPServer, authorization: OwnerAuthorizationServer) -> None:
    @mcp.custom_route(CONSENT_PATH, methods=["GET", "POST"])  # type: ignore[untyped-decorator]
    async def consent(request: Request) -> Response:
        request_id = request.query_params.get("request", "")
        client = authorization.pending_client(request_id)
        if client is None:
            return HTMLResponse("<h1>This approval link has expired.</h1>", status_code=400)
        error = ""
        if request.method == "POST":
            form = await request.form()
            redirect = authorization.approve(request_id, str(form.get("access_code", "")))
            if redirect is not None:
                return RedirectResponse(redirect, status_code=302)
            await anyio.sleep(WRONG_CODE_DELAY)
            error = CONSENT_ERROR.format(
                message="That access code is not the one this server printed."
            )
        return HTMLResponse(CONSENT_PAGE.format(client=escape(client), error=error))


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
            "MCP clients discover it, register themselves and ask you to approve with an "
            "access code, e.g. https://toyota.example.com"
        ),
    )
    remote.add_argument("--host", default="127.0.0.1", help="address to bind (default 127.0.0.1)")
    remote.add_argument("--port", type=int, default=8787, help="port to bind (default 8787)")
    remote.add_argument(
        "--access-code",
        help="the code that approves a client; generated and printed when omitted",
    )
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
        print(NO_CREDENTIALS, file=sys.stderr)
        raise SystemExit(2)
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
    access_code = args.access_code or generate_access_code()
    server = create_server(
        gateway,
        opendata,
        not options.read_only,
        options.places,
        authorization=OwnerAuthorizationServer(access_code, Grants.load(state_file())),
        auth_settings=AuthSettings(
            issuer_url=AnyHttpUrl(public_url),
            resource_server_url=AnyHttpUrl(f"{public_url}/mcp"),
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
            ),
        ),
    )
    print(f"Serving {public_url}/mcp — add that URL in your MCP client.", file=sys.stderr)
    print(f"Access code to approve a client: {access_code}", file=sys.stderr)
    server.run("streamable-http", host=args.host, port=args.port)


def _is_local(url: str) -> bool:
    return url.startswith(("http://localhost", "http://127.0.0.1", "http://[::1]"))


if __name__ == "__main__":
    main()
