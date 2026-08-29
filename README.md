<!-- mcp-name: io.github.zepgram/toyota -->

# toyota-mcp — Toyota & Lexus MCP server for Claude and other AI assistants

**Ask your Toyota anything.** `toyota-mcp` is a [Model Context Protocol](https://modelcontextprotocol.io)
(MCP) server that connects a **MyToyota** or **MyLexus** Europe account to Claude,
Claude Code, Cursor, VS Code or any MCP client — read your car's fuel level,
range, location, trips and health, and send remote commands (lock, climate,
charging) in plain language.

[![PyPI](https://img.shields.io/pypi/v/toyota-mcp)](https://pypi.org/project/toyota-mcp/)
[![CI](https://github.com/zepgram/toyota-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/zepgram/toyota-mcp/actions/workflows/ci.yml)
[![Python](https://img.shields.io/pypi/pyversions/toyota-mcp)](https://pypi.org/project/toyota-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> *"How much range is left?"* · *"Where is the car?"* · *"Is it locked?"* ·
> *"What did the last trip consume?"* · *"What's my EV share this month?"* ·
> *"Pre-heat the car for 8 am."* · *"Cheapest petrol near the car?"*

| | |
|---|---|
| **Vehicles** | Toyota and Lexus, **Europe only** (Toyota Connected Services / `ctpa-oneapi`) |
| **Powertrains** | petrol, diesel, full hybrid, plug-in hybrid, electric |
| **Tools** | 13 read + 11 remote commands + 1 prompt ([full list](#available-tools)) |
| **Install** | `uvx toyota-mcp` — no clone, no build |
| **Requires** | a MyToyota/MyLexus account **without MFA**, Python 3.11+ |
| **Built on** | [pytoyoda](https://github.com/pytoyoda/pytoyoda), the community client for Toyota Europe |

## Requirements

- A **MyToyota Europe** account (the API covers Europe only — North America and
  Japan use entirely different systems).
- Sign in with `toyota-mcp login`, or with account credentials in the
  environment (that path cannot handle MFA/2FA).
- The vehicle must appear in the MyToyota mobile app.
- Python 3.11+ and [`uv`](https://docs.astral.sh/uv/) for the zero-install `uvx` launcher
  (`uvx` fetches a suitable Python by itself).

> **Unofficial API.** Toyota can change or break this API at any time without
> notice. All API access is isolated behind pytoyoda, which historically absorbs
> such breakage within days.

## Quickstart

### Sign in once

```bash
uvx toyota-mcp login
```

Your browser opens Toyota's own sign-in page. Toyota then redirects to an
address the browser cannot follow (`com.toyota.oneapp:/…`) and shows an error —
that is expected: copy that address from the address bar and paste it back. The
session is saved in your operating system's credential store and refreshed
automatically. **Your password is never seen by this program and never written
to a configuration file.** `uvx toyota-mcp logout` forgets it.

### Claude Desktop

Add to `claude_desktop_config.json` (macOS:
`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "toyota": {
      "command": "uvx",
      "args": ["toyota-mcp"]
    }
  }
}
```

Add options after `toyota-mcp` in `args` — for example
`["toyota-mcp", "--addresses", "osm"]` for postal addresses, or
`["toyota-mcp", "--read-only"]` to leave every remote command out.

### Claude Code

```bash
claude mcp add --transport stdio toyota -- uvx toyota-mcp
```

### Cursor, VS Code and other MCP clients

Any client that speaks stdio works with the same shape — command `uvx`,
arguments `["toyota-mcp"]`. In VS Code, add it to `.vscode/mcp.json`; in Cursor,
to `~/.cursor/mcp.json`.

### First contact: the doctor

Before wiring anything into an MCP host, check your setup from a terminal:

```bash
uvx toyota-mcp doctor
```

It validates credentials, lists your vehicles, and prints which tools your
specific car supports. Exit codes: `0` ok · `2` config · `3` auth ·
`4` no vehicle · `5` API error · `6` command rejected (`probe`).

## Remote access — the "Connect" button

By default the server runs on stdio, next to the MCP client. Point it at a URL
instead and it becomes a remote MCP server with its own OAuth 2.1 authorization
server, so a client connects the way it connects to Gmail or GitHub: paste the
URL, press **Connect**, approve in the browser.

```bash
toyota-mcp --http https://toyota.example.com
```

Nothing else to configure — **connecting is signing in to Toyota**:

1. Paste `https://toyota.example.com/mcp` into your client and press Connect.
2. The client registers itself and sends you to this server's sign-in page.
3. Enter the MyToyota email and password, then pick which vehicle to use when
   the account has several.
4. The client gets its token; the server keeps only Toyota's refresh token.

Signing in to the Toyota account *is* the proof that you own the deployment —
there is no shared code and no user database. A server already connected to one
account refuses a sign-in with a different one.

Toyota's own web login ends on a mobile deep link (`com.toyota.oneapp:/…`) that
a browser cannot follow — on a phone it opens the MyToyota app instead — so the
credentials are posted to Toyota by the server you run. They are never written
down: only the refresh token Toyota returns is kept. Accounts with two-factor
authentication cannot be used, as Toyota's vehicle API does not support them.

Run it on a machine of yours (a home server, a Raspberry Pi), behind a
TLS-terminating proxy: the server refuses a non-`https://` public URL other
than localhost, because bearer tokens must not travel in clear. Registered
clients and issued tokens are kept in `~/.local/state/toyota-mcp/oauth.json`
(mode 600) so a restart does not disconnect anyone; access tokens last an hour
and refresh silently.

### With Docker

```bash
docker build -t toyota-mcp --build-arg VERSION=0.3.0 .
docker run -d --name toyota-mcp -v toyota-mcp:/data -p 127.0.0.1:8787:8787 \
  toyota-mcp --http https://toyota.example.com --host 0.0.0.0
```

A container has no credential store, so the session and the grants live in
`/data` — keep it as a volume or signing in again is the price of a restart.
`TOYOTA_SESSION_FILE` moves the session file elsewhere.

## Authentication

Nothing has to be configured before the first use. A server with no account
connected still starts and says what to do: over HTTP the Connect flow signs you
in, otherwise `toyota-mcp login` does. `toyota_list_vehicles` and
`toyota_select_vehicle` then choose the car from the conversation.

Toyota has no third-party API programme: there is no developer portal, no
per-application token, and the mobile app signs in with your account password.
This server therefore offers two ways in, and prefers the one where it never
holds that password.

**`toyota-mcp login`.** Asks for the MyToyota email and password, signs in to
Toyota, and keeps only the **refresh token** — in your operating system's
credential store (Keychain, Windows Credential Locker, Secret Service), or in a
file when there is none. The password is never written down, and never lands in
an MCP host's configuration file.

**Password in the environment.** `TOYOTA_USERNAME` and `TOYOTA_PASSWORD` still
work for unattended setups, but most MCP hosts store their `env` block as plain
text on disk, so prefer `toyota-mcp login`.

## Configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| `TOYOTA_USERNAME` | no | — | MyToyota account email — only for the password sign-in |
| `TOYOTA_PASSWORD` | no | — | MyToyota account password — only for the password sign-in |
| `TOYOTA_VIN` | no | — | Pins one vehicle, overriding the choice made at sign-in |
| `TOYOTA_BRAND` | no | `T` | `T` Toyota, `L` Lexus |
| `TOYOTA_USE_METRIC` | no | `true` | `false` switches to miles/gallons |

Features are command-line options, visible in `toyota-mcp --help` and in your
host's `args`:

| Option | Default | Description |
|---|---|---|
| `--read-only` | off | register only the read tools — no lock, trunk, lights, climate or charging commands |
| `--addresses osm\|fr` | off | turn coordinates into addresses: `osm` worldwide (OpenStreetMap), `fr` France (also enables fuel prices). Off by default because it sends the car's position to that service |
| `--places SPEC` | — | named places, `"home=43.6045,1.4440;work=43.6290,1.3630"`: positions within 200 m are labelled |

```json
"toyota": {
  "command": "uvx",
  "args": ["toyota-mcp", "--addresses", "osm", "--places", "home=43.6045,1.4440"]
}
```

A local `.env` file works too (see `.env.example`). Credentials never touch disk
otherwise; tokens live in memory only.

<a id="available-tools"></a>

## Available tools

Read tools are `readOnlyHint: true`. The remote commands below are registered
unless the server runs with `--read-only`.

| Tool | Example question | Key fields |
|---|---|---|
| `toyota_list_vehicles` / `toyota_select_vehicle` | *Which cars? Use the Yaris.* | every vehicle on the account, and which one the tools act on |
| `toyota_get_vehicle_info` | *What car is this? Is the subscription active?* | model, year, plate, colour, first use, subscriptions, declared remote capabilities |
| `toyota_get_energy` | *How much range is left?* | fuel %, range (km/mi), battery or an explicit "not applicable" note |
| `toyota_get_charging` | *Is it charging? When is the next scheduled charge?* | plug-in battery %, charging status, EV range, time to full, schedules (PHEV / EV only) |
| `toyota_get_status` | *Is the car locked? Did I leave the lights on?* | doors/windows/trunk/hood, lock state, lights, rear-seat reminder, overall status |
| `toyota_get_location` | *Where is the car?* | lat/lon, address (with open data), Google Maps link |
| `toyota_get_odometer` | *How many km on the clock?* | odometer with unit |
| `toyota_get_last_trip` | *What did the last trip consume?* | distance, duration, consumption, EV share, hybrid mode split, start/end places |
| `toyota_get_trips` | *This week's trips?* | individual trips, newest first (≤ 92 days back) |
| `toyota_get_trip_summary` | *Average consumption over the last 7 days? This month's EV share?* | rolling window or calendar period (`today`, `this_week`, `this_month`, `this_year`), recomputed L/100km, EV distance & time share |
| `toyota_get_health` | *Any alerts on the car? When was it serviced?* | warning lights, oil indicators, notifications, full service history |
| `toyota_get_climate` | *Is the pre-heating running? What's the preset?* | remote climate state, target temperature, preset (duration, defrosters, heated seats) |
| `toyota_find_fuel_stations` | *Cheapest station near the car?* | cheapest stations for a fuel around the car (France, open data) |
| `toyota_refresh_data` | *I just parked — refresh.* | bounded cloud re-fetch (never wakes the car) |

## Remote commands

Registered by default; start with `--read-only` to leave them out:

| Tool | Effect | Annotation |
|---|---|---|
| `toyota_lock_doors` / `toyota_unlock_doors` | locks / unlocks the doors | reversible / **destructive** |
| `toyota_lock_trunk` / `toyota_unlock_trunk` | locks / unlocks the trunk only | reversible / **destructive** |
| `toyota_find_car` | flashes the hazard lights briefly (silent) | reversible |
| `toyota_sound_horn` | short horn signal | reversible |
| `toyota_close_windows` | closes the power windows (model-dependent) | reversible |
| `toyota_start_climate` | starts pre-conditioning with the saved preset (or a given temperature, 15–30 °C by 0.5) — on a hybrid this runs the engine | **destructive** |
| `toyota_charge_now` | starts charging immediately (PHEV / EV, plugged in) | reversible |
| `toyota_stop_climate` | stops pre-conditioning | reversible |
| `toyota_wake_vehicle` | asks the car to report its state now (costs a little 12 V battery) | reversible |

Not every car accepts every command: Toyota answers "vehicle not supported"
(or "unknown command") and the tool says so — nothing reaches the car. See
[docs/architecture.md](docs/architecture.md) for the vocabulary observed so far.

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
- Commands are rate-limited to one every 10 s.

## Prompt

`vehicle_briefing` (optional `language` argument) asks the model to produce a
short status briefing — range, doors and lights, position, last trip, alerts,
cheapest fuel when the tank is low — from the tools above.

## Addresses and fuel prices (optional)

`--addresses` turns coordinates into addresses on the parked position and on
trip start/end points — no account, no key:

| Option | Addresses | Fuel prices |
|---|---|---|
| (default) | — | — |
| `osm` | worldwide, OpenStreetMap Nominatim (throttled to 1 request/s per its usage policy, results cached) | — |
| `fr` | France, national address base (`api-adresse.data.gouv.fr`) | `toyota_find_fuel_stations`, prices self-reported by stations to `data.economie.gouv.fr` |

Enabling it sends the car's coordinates to that service; nothing is sent
anywhere otherwise. Address lookups fail open (the answer simply has no
address).

## For AI agents

If you are an AI assistant reading this to decide whether and how to use this
server, here is what you need:

**What it is.** A stdio MCP server exposing one MyToyota/MyLexus Europe vehicle.
Every tool answers with `structuredContent` matching its output schema.

**Choosing a tool.** `toyota_get_energy` for fuel and range on any powertrain;
`toyota_get_charging` only for plug-in hybrids and EVs (it errors with an
explicit "not applicable" otherwise). `toyota_get_status` for doors, windows,
locks, lights; `toyota_get_health` for warning lights, oil indicators,
notifications and service history. `toyota_get_trips` lists individual drives,
`toyota_get_trip_summary` aggregates a window or a calendar period — prefer the
summary for averages, and pass `period` to match the figures shown in the
MyToyota app.

**Data is never live by default.** The car uploads telemetry at ignition-off and
its position when it parks, so every response carries a `freshness` block:
`fetched_at`, `age_seconds`, `source` (`live` / `cache` / `stale_cache`) and
`vehicle_reported_at` when Toyota provides the car-side timestamp. When the user
asks about *current* state, cite that age rather than implying real time. If they
need the state as of now, `toyota_wake_vehicle` asks the car to report (it costs
a little 12 V battery, so do not call it routinely).

**Commands.** Every command tool takes `confirm`. Call it with `confirm=false`
first to preview: nothing is sent, and the report shows the current state. Send
`confirm=true` only after the user explicitly agreed, and never on your own
initiative — `toyota_unlock_doors`, `toyota_unlock_trunk` and
`toyota_start_climate` carry `destructiveHint` (the last one runs the engine on
a hybrid, which is dangerous indoors). The result tells you exactly what
happened: `verified` (the car reported the new state), `accepted` (Toyota took
the command but the car has not confirmed within the timeout — say so, do not
claim success), or `failed` with Toyota's reason. Do not resend on `accepted`;
commands are rate-limited to one per 10 seconds.

**Limits worth stating to the user.** Europe only. Self-charging full hybrids
expose no traction-battery level — the tools say so explicitly instead of
guessing. Toyota keeps roughly 12 months of trip history. Not every car accepts
every command; Toyota answers "vehicle not supported" and nothing reaches the car.

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
| Plug-in battery %, charging status, EV range, schedules, charge now | — explicit "not applicable" note | ✅ (untested by the author — see docs) | — |

Toyota exposes **no traction-battery charge for self-charging hybrids** — it
only exists on the in-car display. The tools say so explicitly instead of
returning misleading nulls.

## Limitations

- Europe only (`ctpa-oneapi` — Toyota Connected Europe).
- Accounts with MFA/2FA cannot authenticate.
- Toyota retains roughly **12 months** of trip history server-side.
- Lock/door status can lag reality; every answer self-reports its age.
- `--read-only` removes every remote command.

## Troubleshooting

| Message | What it means |
|---|---|
| `MyToyota sign-in failed…` | Wrong credentials, or MFA is enabled on the account. Login pauses 60 s between attempts. Run `uvx toyota-mcp doctor`. |
| `No saved session and no credentials…` | Run `uvx toyota-mcp login`, or set `TOYOTA_USERNAME` / `TOYOTA_PASSWORD`. |
| A saved session stops working | Toyota can invalidate it (password change, session revocation). Run `uvx toyota-mcp login` again. |
| `…rate-limiting or temporarily unavailable…` | Transient — NOT an auth problem. The gateway 429s freely; retry in a minute. |
| `Toyota appears to have changed this API endpoint…` | Toyota migrated a route. Update toyota-mcp / pytoyoda. |
| `No parked location has been reported…` | The car has never pushed a position (or lacks the capability). |
| `No vehicles are attached to this MyToyota account.` | Pair the car in the MyToyota mobile app first. |
| `…does not know that remote command (CTP-REMOTE-40006)` | The command string is not in Toyota's current vocabulary (observed for `find-vehicle`, `engine-start`, `engine-stop`, `hazard-off`, `headlight-off`). Nothing reached the car. |
| `…does not support that remote command (CTP-REMOTE-40041)` | Toyota knows the command but this car lacks the feature (observed for `headlight-on` on a 2026 Corolla). Nothing reached the car. |

## Security & privacy

- With `toyota-mcp login` the password never reaches this program: only a
  refresh token is kept, in the operating system's credential store. With the
  environment path the password is held as a `SecretStr` and never logged.
- GPS coordinates, VINs and payloads are never written to logs — pytoyoda's
  debug logging (which dumps full HTTP exchanges) is disabled; only warnings
  reach stderr.
- No tokens or snapshots are persisted to disk.
- `doctor --dump` output is recursively redacted, but review it manually before
  sharing.

## Contributing

[docs/architecture.md](docs/architecture.md) describes the layers, the contracts
(freshness, verification, privacy) and what to touch to add a tool, a command or
a provider. Changes are tracked in [CHANGELOG.md](CHANGELOG.md).

## Development

```bash
git clone https://github.com/zepgram/toyota-mcp && cd toyota-mcp
uv sync                             # version derives from the git tag (hatch-vcs)
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

### Releasing

The version is the git tag — nothing to edit. Tagging publishes the package
(PyPI trusted publishing, `pypi` environment) and creates the GitHub release
from the matching `CHANGELOG.md` section:

```bash
git tag v0.1.0 && git push origin v0.1.0
```

Debug interactively with the MCP Inspector:

```bash
npx @modelcontextprotocol/inspector uvx toyota-mcp
```

Note for contributors (and their coding agents): this project uses MCP Python
SDK **v2** — `MCPServer`, `mcp.server.mcpserver.Context`, `ToolError`. Most
tutorials still show v1's `FastMCP` imports, which no longer exist.

## Related projects

- [pytoyoda](https://github.com/pytoyoda/pytoyoda) — the Python client this
  server is built on; report API breakage there.
- [ha_toyota](https://github.com/pytoyoda/ha_toyota) — Home Assistant
  integration on the same library.
- [tyta](https://github.com/Stopa/tyta) — CLI and MCP server for the same API,
  with its own HTTP client.

## Keywords

Toyota MCP server, Lexus MCP server, MyToyota MCP, Toyota Connected Services
API, Toyota Claude integration, connected car MCP, vehicle telemetry MCP, remote
lock unlock MCP, EV charging MCP, Model Context Protocol car, Toyota Corolla
RAV4 Yaris C-HR bZ4X, Claude Desktop car integration, pytoyoda MCP.

## License

[MIT](LICENSE) © Benjamin Calef
