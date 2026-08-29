# Architecture

`toyota-mcp` turns a MyToyota Europe account into MCP tools. It is deliberately
thin: every piece of Toyota knowledge lives in one place, and adding a feature
means adding data, not plumbing.

## Layers

```
config.py      Settings: the account, from TOYOTA_* env / .env (pydantic-settings).
               ServerOptions: features, from the command line (server.build_parser).
gateway.py     The only code that talks to Toyota: login, one MyT client, snapshot
               cache, single-flight locking, stale-while-error, login cooldown,
               remote commands, verification polling. Wraps pytoyoda's controller
               to keep the raw payloads pytoyoda drops (lights, timestamps, oil).
models.py      Typed reports returned to the model (pydantic → structuredContent),
               conversion helpers, every user-facing note.
tools/         One module per topic; each tool is a thin async function that calls
               the gateway and builds a report. tools/commands.py holds the
               declarative command specs.
opendata.py    Optional enrichment providers (addresses, fuel prices) behind
               --addresses; fail-open.
places.py      Named places (--places) → "home" / "work" labels.
session.py     Browser sign-in and the saved session: the OAuth code exchange, the
               credential store, and a Controller that signs in from a refresh token.
login.py       The `login` / `logout` commands driving that flow.
oauth.py       The OAuth 2.1 authorization server for remote access: dynamic client
               registration, owner consent by access code, tokens persisted to disk.
prompts.py     MCP prompts (vehicle_briefing).
server.py      MCPServer assembly, opt-ins, CLI entry (doctor / probe).
doctor.py      Connectivity and capability diagnosis.  probe.py  Raw command probe.
```

Dependency direction is strictly downward: tools → gateway/models/opendata/places
→ config. Tests fake Toyota at pytoyoda's `controller_class` seam
(`tests/conftest.py`) and drive the real parsing pipeline.

## Contracts

- **Freshness**: every report carries `fetched_at`, `age_seconds`, `source`
  (`live` / `cache` / `stale_cache`) and, when Toyota provides it,
  `vehicle_reported_at`. Snapshots live 5 min (15 for health).
- **Commands are tools like the others** (`readOnlyHint: false`, `destructiveHint`
  where physical): `--read-only` removes them all, and then no request other
  than GET reaches Toyota.
- **Commands**: `confirm=false` previews and sends nothing. A sent command is
  acknowledged (`returnCode 000000`, else `failed`), the car is asked to report
  (wake request), and the outcome is polled for up to 40 s:
  `verified` = the car reported the expected state **and** either the state
  changed or the report is newer than before; otherwise `accepted`. One command
  per 10 s. Nothing is cached unless verified.
- **Errors**: `errors.translate` is the single mapping from pytoyoda/httpx
  exceptions to actionable messages, including Toyota's remote codes
  (40006 unknown command, 40041 vehicle not supported).
- **Storage**: the vehicle session goes to the OS credential store, falling back
  to a file (mode 600) when there is none — a container or a headless server.
  `TOYOTA_SESSION_FILE` overrides the location; OAuth grants always use a file.
- **Authentication**: Toyota exposes no third-party OAuth client, so the only
  credentials are the account's own. `toyota-mcp login` performs the mobile
  client's authorization-code flow in the user's browser and keeps only the
  refresh token, in the OS credential store; `SessionController` seeds pytoyoda
  with it, which makes pytoyoda refresh instead of asking for a password, and
  persists the rotated token. The password path remains for headless setups.
- **Remote access**: with `--http URL` the server serves streamable HTTP and is
  both the OAuth resource server and its own authorization server (Toyota
  cannot be one). Clients register dynamically; the owner approves each with an
  access code; grants live in `~/.local/state/toyota-mcp/oauth.json` (mode
  600). Non-localhost URLs must be `https://` — the process refuses otherwise.
- **Privacy**: pytoyoda's debug logging (full HTTP exchanges) and httpx INFO
  logging are silenced; coordinates, VINs and payloads are never logged;
  enrichment providers only receive coordinates when `--addresses` is set.

## Extension points

| To add… | Touch |
|---|---|
| a read tool | a snapshot key + extractor in `gateway.py`, a report in `models.py`, a tool in `tools/<topic>.py`, a fixture-backed test |
| a remote command | one `CommandSpec` in `tools/commands.py` (`state`/`expected` for stateful commands, none for momentary ones) plus its evidence row below |
| an enrichment provider | a `Provider` value and a method in `opendata.py`; gate it in the tool that uses it |
| an account setting | a field on `Settings` (env), `README`, `.env.example`, `server.json` |
| a feature option | an argument in `server.build_parser`, a field on `ServerOptions`, `README`, `server.json` |

## Evidence — EU backend command vocabulary

Probed with `toyota-mcp probe` (server-side answers). Toyota's accepted strings differ from pytoyoda's enum.

| Command | 2026 Corolla full hybrid | Elsewhere |
|---|---|---|
| `door-lock` / `door-unlock` | accepted; lock verified live (car re-reported in 11 s) | pytoyoda#274 |
| `trunk-lock` / `trunk-unlock` | `trunk-lock` accepted, trunk re-reported in 10 s | pytoyoda#274 |
| `hazard-on` | accepted, momentary flash, car re-reported in 15 s | pytoyoda#274 |
| `buzzer-warning`, `sound-horn` | not probed (audible) | verified in pytoyoda#274 |
| `power-window-close` | 40041 vehicle not supported | works on a 2024 Lexus (pytoyoda#274) |
| `/v2/remote/climate-control` start / stop | stop accepted (`000000`); start proven live by tyta | — |
| `headlight-on` | 40041 vehicle not supported | — |
| `find-vehicle`, `engine-start`, `engine-stop`, `hazard-off`, `headlight-off`, `power-window-on/off` | 40006 unknown command | `find-vehicle` also rejected in pytoyoda#274 |
| `/v1/global/remote/electric/command` `CHARGE_NOW` (`toyota_charge_now`) | not applicable (full hybrid) | **untested** — shape from pytoyoda; reports welcome |
| `/v1/global/remote/electric/status` (`toyota_get_charging`) | not applicable | **untested** — parsed by pytoyoda's model, fixture from its sample payload |

## Versioning and release

The package version comes from the git tag (`hatchling` + `hatch-vcs`); a
checkout without a tag builds `0.1.devN+g<sha>`. Pushing `vX.Y.Z` runs
`.github/workflows/release.yml`: build, assert the built version equals the
tag, publish to PyPI through trusted publishing, then create the GitHub release
with the `[X.Y.Z]` section of `CHANGELOG.md` and the distributions attached.
`server.json` (MCP registry manifest) carries the same version and is updated
by hand in the release commit.

## Backlog (deliberately not built)

- Charging schedules as commands (`RESERVE_CHARGE`, `SET_CHARGING_TIME`): the
  payload (day, start/end times) cannot be verified without a plug-in vehicle;
  reading schedules and `CHARGE_NOW` are shipped untested and say so.
- Several vehicles in one server instance: run one instance per `TOYOTA_VIN`.
- Hosting the server for other people: the access code identifies the owner,
  not individual users, so one deployment serves one household.
