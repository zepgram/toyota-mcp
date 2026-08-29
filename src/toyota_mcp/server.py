from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from loguru import logger as upstream_logger
from mcp.server import MCPServer
from pydantic import ValidationError

from toyota_mcp import __version__
from toyota_mcp.config import Settings
from toyota_mcp.gateway import AppContext, VehicleGateway
from toyota_mcp.tools import register_all

INSTRUCTIONS = (
    "Read-only access to a MyToyota Europe vehicle. Every response carries a "
    "freshness block: the car uploads data when it parks, so cite "
    "vehicle_reported_at or age_seconds when the user asks about current state. "
    "Nothing here actuates the vehicle."
)

USAGE = (
    "usage: toyota-mcp                   start the MCP server on stdio\n"
    "       toyota-mcp doctor [--dump]   check credentials and vehicle support\n"
    "       toyota-mcp --version"
)


def create_server(gateway: VehicleGateway) -> MCPServer:
    @asynccontextmanager
    async def lifespan(server: MCPServer) -> AsyncIterator[AppContext]:
        try:
            yield AppContext(gateway=gateway)
        finally:
            await gateway.aclose()

    mcp = MCPServer(
        "toyota",
        version=__version__,
        instructions=INSTRUCTIONS,
        lifespan=lifespan,
        log_level="WARNING",
    )
    register_all(mcp)
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
    if arguments:
        print(USAGE, file=sys.stderr)
        raise SystemExit(2)
    create_server(VehicleGateway(load_settings())).run()


if __name__ == "__main__":
    main()
