from mcp.server import MCPServer

BRIEFING = """Give the user a short briefing about their car, in {language}.

Call these tools, then summarize in a few lines — no tables, no raw JSON:
1. toyota_get_energy — remaining range and fuel level.
2. toyota_get_status — locked? any door, window or light left open/on? any warning?
3. toyota_get_location — where it is (named place or address when available), and how
   old that position is (freshness.vehicle_reported_at).
4. toyota_get_last_trip — when it last drove, distance, consumption, hybrid share.
5. toyota_get_health — active alerts and unread notifications; skip if nothing.
6. toyota_find_fuel_stations — only if the fuel level is below 25 % and the tool is
   available; mention the cheapest station and its distance.

Cite the age of the data when it matters (the car reports when it parks). Mention
anything that needs attention first; otherwise say the car is fine."""


def register(mcp: MCPServer) -> None:
    @mcp.prompt(
        name="vehicle_briefing",
        title="Vehicle briefing",
        description=(
            "A short status briefing of the car: range, doors and lights, position, last "
            "trip, alerts, and the cheapest fuel nearby when relevant."
        ),
    )
    def vehicle_briefing(language: str = "the user's language") -> str:
        return BRIEFING.format(language=language)
