from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from loguru import logger as upstream_logger
from mcp.server import MCPServer
from pydantic import ValidationError

from toyota_mcp import __version__, prompts
from toyota_mcp.config import Settings
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
    "Remote commands (locks, trunk, lights, horn, windows, climate) are enabled: send one "
    "only when the user "
    "explicitly asked for it in this conversation, never on your own initiative; call "
    "with confirm=false to preview, then confirm=true once the user agreed."
)

USAGE = (
    "usage: toyota-mcp                      start the MCP server on stdio\n"
    "       toyota-mcp doctor [--dump]      check credentials and vehicle support\n"
    "       toyota-mcp probe <command> ...  send one raw remote command (see probe --help)\n"
    "       toyota-mcp --version"
)


def create_server(
    gateway: VehicleGateway,
    opendata: OpenData | None = None,
    remote_commands: bool = False,
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
    arguments = sys.argv[1:]
    if arguments and arguments[0] in ("-h", "--help"):
        print(USAGE)
        raise SystemExit(0)
    if arguments and arguments[0] == "--version":
        print(__version__)
        raise SystemExit(0)
    if arguments and arguments[0] == "doctor":
        from toyota_mcp import doctor

        raise SystemExit(doctor.run(dump="--dump" in arguments[1:]))
    if arguments and arguments[0] == "probe":
        from toyota_mcp import probe

        raise SystemExit(probe.run(arguments[1:]))
    if arguments:
        print(USAGE, file=sys.stderr)
        raise SystemExit(2)
    settings = load_settings()
    opendata = OpenData(settings.open_data) if settings.open_data != "off" else None
    create_server(
        VehicleGateway(settings),
        opendata,
        settings.remote_commands,
        Places.parse(settings.places),
    ).run()


if __name__ == "__main__":
    main()
