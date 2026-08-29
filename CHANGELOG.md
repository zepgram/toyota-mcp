# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.2.0] — 2026-08-29

### Added
- Remote commands (opt-in `TOYOTA_REMOTE_COMMANDS=true`): lock/unlock doors,
  lock/unlock trunk, find car (hazard lights), sound horn, close windows, start/stop
  remote climate, charge now (plug-in), wake the vehicle — every command previews with `confirm=false`,
  checks Toyota's acknowledgement and verifies the car-reported outcome.
- `toyota_get_vehicle_info`, `toyota_get_climate`, `toyota_find_fuel_stations`,
  `toyota_get_charging` (plug-in hybrids and EVs; untested by the author).
- Lights, rear-seat reminder, overall status and warning count in `toyota_get_status`;
  oil indicators and the full service history in `toyota_get_health`; hybrid mode
  split and start/end places in trips; calendar periods in `toyota_get_trip_summary`.
- `TOYOTA_OPEN_DATA` (`osm` worldwide addresses, `fr` French addresses + fuel prices),
  `TOYOTA_PLACES` named places, the `vehicle_briefing` prompt.
- `toyota-mcp probe` to test raw command strings against the backend.

### Changed
- Python 3.11+, tested on 3.11–3.14; pytoyoda's debug logging silenced.

## [0.1.0] — 2026-08-28

- First read-only release: status, energy, odometer, location, trips, trip summary,
  health, refresh; `toyota-mcp doctor`.
