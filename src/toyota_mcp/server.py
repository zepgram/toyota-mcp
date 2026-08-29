from __future__ import annotations

import argparse
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from loguru import logger as upstream_logger
from mcp.server import MCPServer
from pydantic import ValidationError

from toyota_mcp import __version__, probe, prompts
from toyota_mcp.config import ServerOptions, Settings
from toyota_mcp.gateway import AppContext, VehicleGateway
from toyota_mcp.opendata import OpenData
from toyota_mcp.places import Places
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
    )
    register_all(mcp, remote_commands=remote_commands)
    prompts.register(mcp)
    return mcp


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="toyota-mcp",
        description=(
            "MCP server for MyToyota Europe. Credentials come from the TOYOTA_USERNAME / "
            "TOYOTA_PASSWORD environment variables (or a .env file); features are options."
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
    commands = parser.add_subparsers(dest="subcommand", metavar="{doctor,probe}")
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
        return Settings()
    except ValidationError as exc:
        for error in exc.errors():
            variable = "TOYOTA_" + "_".join(str(part) for part in error["loc"]).upper()
            print(f"{variable}: {error['msg']}", file=sys.stderr)
        print(
            "Set the variables in your MCP host's env block or in a local .env file "
            "(see .env.example).",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc


def main() -> None:
    silence_upstream_debug_logs()
    args = build_parser().parse_args()
    options = ServerOptions(read_only=args.read_only, addresses=args.addresses, places=args.places)
    if args.subcommand == "doctor":
        from toyota_mcp import doctor

        raise SystemExit(doctor.run(dump=args.dump, options=options))
    if args.subcommand == "probe":
        raise SystemExit(probe.execute(args))
    settings = load_settings()
    opendata = OpenData(options.addresses) if options.addresses != "off" else None
    create_server(VehicleGateway(settings), opendata, not options.read_only, options.places).run()


if __name__ == "__main__":
    main()
