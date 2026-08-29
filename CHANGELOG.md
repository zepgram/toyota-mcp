# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). The package version is the git tag:
tagging `vX.Y.Z` publishes `X.Y.Z` to PyPI and creates the GitHub release.

## [Unreleased]

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
