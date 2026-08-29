<!-- mcp-name: io.github.zepgram/toyota -->

# toyota-mcp

Ask your Toyota anything — [MCP](https://modelcontextprotocol.io) server for MyToyota Europe, read-only by default.

[![PyPI](https://img.shields.io/pypi/v/toyota-mcp)](https://pypi.org/project/toyota-mcp/)
[![CI](https://github.com/zepgram/toyota-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/zepgram/toyota-mcp/actions/workflows/ci.yml)
[![Python](https://img.shields.io/pypi/pyversions/toyota-mcp)](https://pypi.org/project/toyota-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Connect your MyToyota Europe account to Claude (or any MCP client) and ask in
plain language: *"how much range is left?"*, *"where is the car?"*, *"what did
the last trip consume?"*, *"what's my EV share this month?"*.

Built on [pytoyoda](https://github.com/pytoyoda/pytoyoda), the community-maintained
client for Toyota's European Connected Services.

## Requirements

- A **MyToyota Europe** account (the API covers Europe only — North America and
  Japan use entirely different systems).
- The account must **not have MFA/2FA enabled** (unsupported by the underlying API).
- The vehicle must appear in the MyToyota mobile app.
- Python 3.11+ and [`uv`](https://docs.astral.sh/uv/) for the zero-install `uvx` launcher
  (`uvx` fetches a suitable Python by itself).

> **Unofficial API.** Toyota can change or break this API at any time without
> notice. All API access is isolated behind pytoyoda, which historically absorbs
> such breakage within days.

## Quickstart

### Claude Desktop

Add to `claude_desktop_config.json` (macOS:
`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "toyota": {
      "command": "uvx",
      "args": ["toyota-mcp"],
      "env": {
        "TOYOTA_USERNAME": "you@example.com",
        "TOYOTA_PASSWORD": "your-password"
      }
    }
  }
}
```

### Claude Code

```bash
claude mcp add --transport stdio toyota \
  --env TOYOTA_USERNAME=you@example.com \
  --env TOYOTA_PASSWORD=your-password \
  -- uvx toyota-mcp
```

### First contact: the doctor

Before wiring anything into an MCP host, check your setup from a terminal:

```bash
TOYOTA_USERNAME=you@example.com TOYOTA_PASSWORD=... uvx toyota-mcp doctor
```

It validates credentials, lists your vehicles, and prints which tools your
specific car supports. Exit codes: `0` ok · `2` config · `3` auth ·
`4` no vehicle · `5` API error.

## Configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| `TOYOTA_USERNAME` | yes | — | MyToyota account email |
| `TOYOTA_PASSWORD` | yes | — | MyToyota account password |
| `TOYOTA_VIN` | no | — | Selects one vehicle when several share the account |
| `TOYOTA_BRAND` | no | `T` | `T` Toyota, `L` Lexus |
| `TOYOTA_USE_METRIC` | no | `true` | `false` switches to miles/gallons |
| `TOYOTA_OPEN_DATA` | no | `off` | `osm` adds addresses worldwide (OpenStreetMap); `fr` adds French addresses and fuel prices (see below) |
| `TOYOTA_REMOTE_COMMANDS` | no | `false` | `true` registers the lock / unlock / climate tools (see below) |

A local `.env` file works too (see `.env.example`). Credentials never touch disk
otherwise; tokens live in memory only.

## Available tools

By default every tool is **read-only** (`readOnlyHint: true`) and nothing can
actuate the vehicle. Remote commands exist but are opt-in — see below.

| Tool | Example question | Key fields |
|---|---|---|
| `toyota_get_vehicle_info` | *What car is this? Is the subscription active?* | model, year, plate, colour, first use, subscriptions, declared remote capabilities |
| `toyota_get_energy` | *How much range is left?* | fuel %, range (km/mi), battery or an explicit "not applicable" note |
| `toyota_get_status` | *Is the car locked? Did I leave the lights on?* | doors/windows/trunk/hood, lock state, lights, rear-seat reminder, overall status |
| `toyota_get_location` | *Where is the car?* | lat/lon, address (with open data), Google Maps link |
| `toyota_get_odometer` | *How many km on the clock?* | odometer with unit |
| `toyota_get_last_trip` | *What did the last trip consume?* | distance, duration, consumption, EV share, hybrid mode split, start/end places |
| `toyota_get_trips` | *This week's trips?* | individual trips, newest first (≤ 92 days back) |
| `toyota_get_trip_summary` | *Average consumption over the last 7 days?* | rolling-window totals, recomputed L/100km, EV distance & time share |
| `toyota_get_health` | *Any alerts on the car?* | warning lights, notifications, last recorded service |
| `toyota_get_climate` | *Is the pre-heating running? What's the preset?* | remote climate state, target temperature, preset (duration, defrosters, heated seats) |
| `toyota_find_fuel_stations` | *Cheapest station near the car?* | cheapest stations for a fuel around the car (France, open data) |
| `toyota_refresh_data` | *I just parked — refresh.* | bounded cloud re-fetch (never wakes the car) |

## Remote commands (optional)

Set `TOYOTA_REMOTE_COMMANDS=true` to register four write tools:

| Tool | Effect | Annotation |
|---|---|---|
| `toyota_lock_doors` | locks doors and trunk | reversible |
| `toyota_unlock_doors` | unlocks the doors | **destructive** |
| `toyota_start_climate` | starts pre-conditioning at the preset (or a given temperature, 15–30 °C by 0.5) — on a hybrid this runs the engine | **destructive** |
| `toyota_stop_climate` | stops pre-conditioning | reversible |

Safety model:

- Every command takes a `confirm` parameter. `confirm=false` (the default)
  returns a preview and **sends nothing**; the agent is instructed to preview,
  get the user's agreement, then call with `confirm=true`.
- Your MCP host still applies its own permission prompt for non-read-only tools.
- Toyota's acknowledgement is checked: a command it refuses (return code
  other than `000000`) comes back as `failed` and nothing is polled.
- The outcome is **verified**: after sending, the server asks the car to
  report (the same wake request the MyToyota app issues) and polls the
  reported state for up to 40 s. `verified` means the car reported the new
  state or a fresh report; `accepted` means Toyota took the command but no
  change was reported yet — check again in a minute, and `toyota_get_health`
  shows any message Toyota sent about it (e.g. the car was moving).
- Commands are rate-limited to one every 10 s. Horn, lights and window
  commands are deliberately not exposed.

## Addresses and fuel prices (optional)

`TOYOTA_OPEN_DATA` turns coordinates into addresses on the parked position and
on trip start/end points — no account, no key:

| Value | Addresses | Fuel prices |
|---|---|---|
| `off` (default) | — | — |
| `osm` | worldwide, OpenStreetMap Nominatim (throttled to 1 request/s per its usage policy, results cached) | — |
| `fr` | France, national address base (`api-adresse.data.gouv.fr`) | `toyota_find_fuel_stations`, prices self-reported by stations to `data.economie.gouv.fr` |

Enabling it sends the car's coordinates to that service; nothing is sent
anywhere otherwise. Address lookups fail open (the answer simply has no
address).

## How fresh is the data?

Toyota's cloud is **push-on-event**: the car uploads telemetry at ignition-off
and its position when it parks. Polling faster returns the same payload, so
this server caches snapshots for 5 minutes (15 for health and service data)
and serializes all upstream calls
(the gateway rate-limits bursts aggressively).

Every response carries a `freshness` block:

- `fetched_at` / `age_seconds` — when this server last read Toyota's cloud;
- `source` — `live`, `cache`, or `stale_cache` (Toyota briefly unavailable,
  serving last known data instead of failing);
- `vehicle_reported_at` — the car-side timestamp, when Toyota provides one.

`toyota_refresh_data` exists for the one real gap (you just parked and want the
newest position) and is floor-limited to once per minute. It re-reads the
cloud — it never wakes the car, so it cannot drain the 12V battery.

## Powertrain coverage

| Data | Full hybrid (self-charging) | PHEV / EV | Petrol/diesel |
|---|---|---|---|
| Fuel level & range | ✅ | ✅ | ✅ |
| Doors/windows/locks | ✅ | ✅ | ✅ |
| Location, odometer, health | ✅ | ✅ | ✅ |
| Trips incl. **EV share** | ✅ | ✅ | ✅ (no EV share) |
| Plug-in battery %, charging | — explicit "not applicable" note | ✅ | — |

Toyota exposes **no traction-battery charge for self-charging hybrids** — it
only exists on the in-car display. The tools say so explicitly instead of
returning misleading nulls.

## Limitations

- Europe only (`ctpa-oneapi` — Toyota Connected Europe).
- Accounts with MFA/2FA cannot authenticate.
- Toyota retains roughly **12 months** of trip history server-side.
- Lock/door status can lag reality; every answer self-reports its age.
- Remote commands are off unless `TOYOTA_REMOTE_COMMANDS=true`.

## Troubleshooting

| Message | What it means |
|---|---|
| `MyToyota sign-in failed…` | Wrong credentials, or MFA is enabled on the account. Login pauses 60 s between attempts. Run `uvx toyota-mcp doctor`. |
| `…rate-limiting or temporarily unavailable…` | Transient — NOT an auth problem. The gateway 429s freely; retry in a minute. |
| `Toyota appears to have changed this API endpoint…` | Toyota migrated a route. Update toyota-mcp / pytoyoda. |
| `No parked location has been reported…` | The car has never pushed a position (or lacks the capability). |
| `No vehicles are attached to this MyToyota account.` | Pair the car in the MyToyota mobile app first. |
| `…does not know that remote command (CTP-REMOTE-40006)` | The command string is not in Toyota's current vocabulary (observed for `find-vehicle`, `hazard-off`, `headlight-off`). Nothing reached the car. |
| `…does not support that remote command (CTP-REMOTE-40041)` | Toyota knows the command but this car lacks the feature (observed for `headlight-on` on a 2026 Corolla). Nothing reached the car. |

## Security & privacy

- Credentials come from environment variables only; the password is held as a
  `SecretStr` and never logged.
- GPS coordinates, VINs and payloads are never written to logs — pytoyoda's
  debug logging (which dumps full HTTP exchanges) is disabled; only warnings
  reach stderr.
- No tokens or snapshots are persisted to disk.
- `doctor --dump` output is recursively redacted, but review it manually before
  sharing.

## Development

```bash
git clone https://github.com/zepgram/toyota-mcp && cd toyota-mcp
uv sync
uv run pytest                       # 127 tests, no network
uv run ruff check && uv run ruff format --check
uv run mypy src tests
```

Tests fake Toyota at pytoyoda's own `controller_class` seam and exercise the
real parsing pipeline against anonymized payloads — CI never touches Toyota.
Pre-release, run the live smoke tests with real credentials:

```bash
uv run pytest -m live
```

Check which raw command strings your car's backend accepts — Toyota's vocabulary
differs from the documented one (pytoyoda#274) and only a live probe settles it.
Every command is physical, so run it next to the car:

```bash
uvx toyota-mcp probe headlight-on --watch lights
uvx toyota-mcp probe find-vehicle --beeps 2
```

Debug interactively with the MCP Inspector:

```bash
npx @modelcontextprotocol/inspector uvx toyota-mcp
```

Note for contributors (and their coding agents): this project uses MCP Python
SDK **v2** — `MCPServer`, `mcp.server.mcpserver.Context`, `ToolError`. Most
tutorials still show v1's `FastMCP` imports, which no longer exist.

## License

[MIT](LICENSE) © Benjamin Calef
