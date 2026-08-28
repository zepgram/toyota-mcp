from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.server import MCPServer
from pydantic import ValidationError

from toyota_mcp.config import Settings
from toyota_mcp.gateway import AppContext, VehicleGateway
from toyota_mcp.tools import register_all

INSTRUCTIONS = (
    "Read-only access to a MyToyota Europe vehicle. Every response carries a "
    "freshness block: the car uploads data when it parks, so cite "
    "vehicle_reported_at or age_seconds when the user asks about current state. "
    "Nothing here actuates the vehicle."
)


def create_server(gateway: VehicleGateway) -> MCPServer:
    @asynccontextmanager
    async def lifespan(server: MCPServer) -> AsyncIterator[AppContext]:
        try:
            yield AppContext(gateway=gateway)
        finally:
            await gateway.aclose()

    mcp = MCPServer("toyota", instructions=INSTRUCTIONS, lifespan=lifespan)
    register_all(mcp)
    return mcp


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
    if len(sys.argv) > 1 and sys.argv[1] == "doctor":
        from toyota_mcp import doctor

        raise SystemExit(doctor.run(dump="--dump" in sys.argv[2:]))
    settings = load_settings()
    create_server(VehicleGateway(settings)).run()


if __name__ == "__main__":
    main()
