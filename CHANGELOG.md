# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). The package version is the git tag:
tagging `vX.Y.Z` publishes `X.Y.Z` to PyPI and creates the GitHub release.

## [0.3.0] — 2026-08-29

### Added
- Connecting a remote server **is** the Toyota sign-in: the OAuth consent page
  sends the owner to Toyota's own page and then lets them pick a vehicle, so a
  fresh deployment needs no configuration, no pre-existing session and no
  shared secret. Signing in to the account is the proof of ownership.
- The same setup from a conversation, for stdio clients: `toyota_sign_in`,
  `toyota_complete_sign_in`, `toyota_list_vehicles`, `toyota_select_vehicle`.
  The chosen vehicle is remembered across restarts.

### Removed
- `--access-code`: the Toyota sign-in replaces it.

## [0.2.1] — 2026-08-29

### Fixed
- The saved session falls back to a file (mode 600) when the machine has no
  credential store, so the server can run in a container or on a headless
  host; `TOYOTA_SESSION_FILE` chooses where. A `Dockerfile` ships with the
  project.

## [0.2.0] — 2026-08-29

### Added
- `toyota-mcp login` / `logout`: sign in through the browser on Toyota's own
  page and keep only a refresh token in the operating system's credential
  store, so the account password is never held by this program nor written to
  an MCP host's configuration file. `TOYOTA_USERNAME` / `TOYOTA_PASSWORD`
  become an optional headless fallback.
- Remote access: `--http URL` serves streamable HTTP behind an OAuth 2.1
  authorization server (dynamic client registration, PKCE, owner consent by
  access code, grants persisted), so MCP clients connect with their usual
  "Connect" button instead of a local process.

## [0.1.0] — 2026-08-29

### Added
- Read tools: vehicle info, status (doors, windows, locks, lights, rear-seat
  reminder), energy, odometer, location (address and named place), last trip,
  trips, trip summary (rolling window or calendar period), health (warning
  lights, oil indicators, notifications, service history), climate, charging
  (plug-in hybrids and EVs), fuel stations (France), refresh.
- Remote commands (`--read-only` leaves them out): lock/unlock doors,
  lock/unlock trunk, find car (hazard lights), sound horn, close windows,
  start/stop remote climate, charge now, wake the vehicle — every command
  previews with `confirm=false`, checks Toyota's acknowledgement and verifies the
  car-reported outcome.
- `--addresses osm|fr` (worldwide or French addresses, French fuel prices),
  `--places` named places, the `vehicle_briefing` prompt.
- `toyota-mcp doctor` (credentials, vehicles, tool availability) and
  `toyota-mcp probe` (raw command strings against the backend).
