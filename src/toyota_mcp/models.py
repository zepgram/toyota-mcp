from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, model_validator

if TYPE_CHECKING:
    from pytoyoda.models.dashboard import Dashboard
    from pytoyoda.models.location import Location
    from pytoyoda.models.lock_status import Door, LockStatus, Window
    from pytoyoda.models.nofication import Notification
    from pytoyoda.models.service_history import ServiceHistory
    from pytoyoda.models.summary import Summary
    from pytoyoda.models.trips import Trip

OpenState = Literal["closed", "open", "unknown"]
LockState = Literal["locked", "unlocked", "unknown"]
Powertrain = Literal["full_hybrid", "plug_in_hybrid", "electric", "fuel_only", "unknown"]

RETENTION_NOTE = "Toyota keeps roughly 12 months of trip history server-side; older trips are gone."
LOCATION_NOTE = (
    "Position updates only when the car parks — while driving this shows the last parking spot."
)
SERVICE_NOTE = (
    "Toyota exposes service HISTORY, not upcoming maintenance deadlines — "
    "never infer a next-service date from this data."
)
WARNING_LIGHTS_CAVEAT = (
    "Raw manufacturer payload — semantics are not fully documented upstream; "
    "an empty list means no active warning reported."
)
FULL_HYBRID_BATTERY_NOTE = (
    "Not applicable: self-charging full hybrid — Toyota's API exposes no plug-in battery "
    "or charging data, and traction-battery charge is only visible on the in-car display. "
    "EV usage share is available via the trip tools."
)
FUEL_ONLY_BATTERY_NOTE = "Not applicable: combustion-only vehicle — there is no traction battery."
BATTERY_UNAVAILABLE_NOTE = (
    "Battery data unavailable: the vehicle has not reported electric status yet."
)


class Quantity(BaseModel):
    value: float = Field(description="Numeric value in the stated unit.")
    unit: str = Field(description="Explicit unit, e.g. 'km', 'mi', 'L', 'L/100km', 'km/h'.")


class Freshness(BaseModel):
    fetched_at: datetime = Field(description="When this server last read the data from Toyota.")
    age_seconds: float = Field(ge=0, description="Age of the server-side read, in seconds.")
    source: Literal["live", "cache", "stale_cache"] = Field(
        description=(
            "'live' = fetched for this call; 'cache' = recent snapshot; "
            "'stale_cache' = Toyota is unavailable, serving the last known data."
        )
    )
    vehicle_reported_at: datetime | None = Field(
        default=None,
        description="Car-side timestamp of the data itself, when Toyota provides one.",
    )
    note: str | None = Field(default=None, description="Human-readable freshness caveat.")

    def with_vehicle_time(self, reported_at: datetime | None, note: str | None = None) -> Freshness:
        return self.model_copy(
            update={"vehicle_reported_at": reported_at, "note": note or self.note}
        )


class DoorReport(BaseModel):
    state: OpenState = Field(description="'unknown' means the sensor did not report.")
    lock: LockState = Field(description="'unknown' means the sensor did not report.")


class DoorsReport(BaseModel):
    driver: DoorReport
    passenger: DoorReport
    driver_rear: DoorReport
    passenger_rear: DoorReport
    trunk: DoorReport


class WindowsReport(BaseModel):
    driver: OpenState
    passenger: OpenState
    driver_rear: OpenState
    passenger_rear: OpenState


class StatusReport(BaseModel):
    all_locked: LockState = Field(
        description=(
            "'locked' if every reporting door is locked, 'unlocked' if any reporting door "
            "is unlocked, 'unknown' if no door reported a lock state."
        )
    )
    doors: DoorsReport
    windows: WindowsReport
    hood: OpenState
    freshness: Freshness

    @classmethod
    def from_lock_status(cls, lock_status: LockStatus, freshness: Freshness) -> StatusReport:
        doors = lock_status.doors
        windows = lock_status.windows
        door_reports = DoorsReport(
            driver=_door(doors.driver_seat if doors else None),
            passenger=_door(doors.passenger_seat if doors else None),
            driver_rear=_door(doors.driver_rear_seat if doors else None),
            passenger_rear=_door(doors.passenger_rear_seat if doors else None),
            trunk=_door(doors.trunk if doors else None),
        )
        window_reports = WindowsReport(
            driver=_window(windows.driver_seat if windows else None),
            passenger=_window(windows.passenger_seat if windows else None),
            driver_rear=_window(windows.driver_rear_seat if windows else None),
            passenger_rear=_window(windows.passenger_rear_seat if windows else None),
        )
        return cls(
            all_locked=_overall_lock(door_reports),
            doors=door_reports,
            windows=window_reports,
            hood=_open_state(lock_status.hood.closed if lock_status.hood else None),
            freshness=freshness.with_vehicle_time(lock_status.last_updated),
        )


class BatteryStatus(BaseModel):
    level_percent: float = Field(description="Traction battery charge level, percent.")
    range: Quantity | None = Field(default=None, description="Electric range.")
    range_with_ac: Quantity | None = Field(default=None, description="Electric range with AC on.")
    charging_status: str | None = Field(default=None, description="Raw Toyota charging status.")
    remaining_charge_minutes: float | None = Field(
        default=None, description="Minutes until fully charged, when charging."
    )


class EnergyReport(BaseModel):
    powertrain: Powertrain
    fuel_level_percent: float | None = Field(default=None, description="Fuel tank level, percent.")
    fuel_range: Quantity | None = Field(default=None, description="Range on fuel alone.")
    total_range: Quantity | None = Field(
        default=None, description="Total remaining range reported by the car."
    )
    battery: BatteryStatus | None = Field(
        default=None, description="Plug-in battery state; only for PHEV/EV powertrains."
    )
    battery_note: str | None = Field(
        default=None, description="Why battery data is absent, when it is."
    )
    freshness: Freshness

    @model_validator(mode="after")
    def battery_absence_must_be_explained(self) -> EnergyReport:
        if self.battery is None and not self.battery_note:
            raise ValueError("battery_note is required when battery is absent")
        return self

    @classmethod
    def from_dashboard(
        cls, dashboard: Dashboard[Any], powertrain: Powertrain, freshness: Freshness
    ) -> EnergyReport:
        battery, battery_note = _battery(dashboard, powertrain)
        return cls(
            powertrain=powertrain,
            fuel_level_percent=dashboard.fuel_level,
            fuel_range=_quantity(dashboard.fuel_range_with_unit),
            total_range=_quantity(dashboard.range_with_unit),
            battery=battery,
            battery_note=battery_note,
            freshness=freshness,
        )


class OdometerReport(BaseModel):
    odometer: Quantity
    freshness: Freshness


class LocationReport(BaseModel):
    latitude: float
    longitude: float
    place_label: str | None = Field(default=None, description="Toyota's place name, if any.")
    google_maps_url: str
    freshness: Freshness

    @classmethod
    def from_location(
        cls, latitude: float, longitude: float, location: Location, freshness: Freshness
    ) -> LocationReport:
        return cls(
            latitude=latitude,
            longitude=longitude,
            place_label=location.state,
            google_maps_url=f"https://www.google.com/maps?q={latitude},{longitude}",
            freshness=freshness.with_vehicle_time(location.timestamp, LOCATION_NOTE),
        )


class TripReport(BaseModel):
    started_at: datetime | None
    ended_at: datetime | None
    duration_minutes: float | None
    distance: Quantity | None
    fuel_consumed: Quantity | None
    average_consumption: Quantity | None
    ev_distance: Quantity | None = Field(
        default=None, description="Distance driven in electric mode."
    )
    ev_ratio_percent: float | None = Field(
        default=None, description="Share of the distance driven in electric mode."
    )
    ev_duration_minutes: float | None = None
    hybrid_score: float | None = Field(default=None, description="Toyota eco-driving score.")
    score_acceleration: float | None = None
    score_braking: float | None = None
    score_constant_speed: float | None = None

    @classmethod
    def from_trip(cls, trip: Trip[Any], use_metric: bool) -> TripReport:
        distance_unit = "km" if use_metric else "mi"
        return cls(
            started_at=trip.start_time,
            ended_at=trip.end_time,
            duration_minutes=_minutes(trip.duration),
            distance=_quantity_of(trip.distance, distance_unit),
            fuel_consumed=_quantity_of(trip.fuel_consumed, "L" if use_metric else "gal"),
            average_consumption=_quantity_of(
                trip.average_fuel_consumed, "L/100km" if use_metric else "mpg"
            ),
            ev_distance=_quantity_of(trip.ev_distance, distance_unit),
            ev_ratio_percent=_ratio_percent(trip.ev_distance, trip.distance),
            ev_duration_minutes=_minutes(trip.ev_duration),
            hybrid_score=trip.score,
            score_acceleration=trip.score_acceleration,
            score_braking=trip.score_braking,
            score_constant_speed=trip.score_constant_speed,
        )


class TripsReport(BaseModel):
    window_from: date
    window_to: date
    trips: list[TripReport] = Field(description="Newest first.")
    returned_count: int
    note: str | None = Field(default=None, description="Set when the window contains no trips.")
    retention_note: str
    freshness: Freshness

    @classmethod
    def from_trips(
        cls,
        trips: list[Trip[Any]],
        window_from: date,
        window_to: date,
        limit: int,
        use_metric: bool,
        freshness: Freshness,
    ) -> TripsReport:
        ordered = sorted(
            trips,
            key=lambda trip: trip.start_time.timestamp() if trip.start_time else float("-inf"),
            reverse=True,
        )[:limit]
        reports = [TripReport.from_trip(trip, use_metric) for trip in ordered]
        note = None
        if not reports:
            note = (
                f"No trips recorded between {window_from.isoformat()} and {window_to.isoformat()}."
            )
        return cls(
            window_from=window_from,
            window_to=window_to,
            trips=reports,
            returned_count=len(reports),
            note=note,
            retention_note=RETENTION_NOTE,
            freshness=freshness,
        )


class TripSummaryReport(BaseModel):
    window_from: date
    window_to: date
    total_distance: Quantity
    total_duration_hours: float
    fuel_consumed: Quantity
    average_consumption: Quantity | None = Field(
        default=None,
        description="Recomputed over the whole window (total fuel vs total distance).",
    )
    ev_distance: Quantity
    ev_ratio_percent: float | None = Field(
        default=None, description="Share of the distance driven in electric mode."
    )
    ev_duration_hours: float | None = None
    ev_time_ratio_percent: float | None = Field(
        default=None, description="Share of the driving time spent in electric mode."
    )
    average_speed: Quantity | None = None
    countries: list[str] = Field(description="ISO 3166-1 alpha-2 codes of countries driven in.")
    days_with_driving: int
    note: str | None = Field(default=None, description="Set when the window contains no trips.")
    freshness: Freshness

    @classmethod
    def from_daily_summaries(
        cls,
        summaries: list[Summary[Any]],
        window_from: date,
        window_to: date,
        use_metric: bool,
        freshness: Freshness,
    ) -> TripSummaryReport:
        distance_unit = "km" if use_metric else "mi"
        total_distance = sum(summary.distance or 0.0 for summary in summaries)
        total_duration = sum(
            (summary.duration or timedelta()).total_seconds() for summary in summaries
        )
        total_fuel = sum(summary.fuel_consumed or 0.0 for summary in summaries)
        total_ev_distance = sum(summary.ev_distance or 0.0 for summary in summaries)
        total_ev_duration = sum(
            (summary.ev_duration or timedelta()).total_seconds() for summary in summaries
        )
        duration_hours = total_duration / 3600
        countries = sorted({country for s in summaries for country in s.countries or []})
        days_with_driving = sum(1 for s in summaries if (s.distance or 0.0) > 0)
        return cls(
            window_from=window_from,
            window_to=window_to,
            total_distance=Quantity(value=round(total_distance, 1), unit=distance_unit),
            total_duration_hours=round(duration_hours, 2),
            fuel_consumed=Quantity(value=round(total_fuel, 2), unit="L" if use_metric else "gal"),
            average_consumption=_average_consumption(total_fuel, total_distance, use_metric),
            ev_distance=Quantity(value=round(total_ev_distance, 1), unit=distance_unit),
            ev_ratio_percent=_ratio_percent(total_ev_distance, total_distance),
            ev_duration_hours=round(total_ev_duration / 3600, 2),
            ev_time_ratio_percent=_ratio_percent(total_ev_duration, total_duration),
            average_speed=(
                Quantity(
                    value=round(total_distance / duration_hours, 1),
                    unit="km/h" if use_metric else "mph",
                )
                if duration_hours > 0
                else None
            ),
            countries=countries,
            days_with_driving=days_with_driving,
            note=(
                None
                if days_with_driving
                else f"No trips recorded between {window_from.isoformat()} "
                f"and {window_to.isoformat()}."
            ),
            freshness=freshness,
        )


class NotificationItem(BaseModel):
    date: datetime | None
    category: str | None
    type: str | None
    message: str | None
    read: bool

    @classmethod
    def from_notification(cls, notification: Notification) -> NotificationItem:
        return cls(
            date=notification.date,
            category=notification.category,
            type=notification.type,
            message=notification.message,
            read=notification.read is not None,
        )


class ServiceRecord(BaseModel):
    date: date | None
    odometer: Quantity | None
    category: str | None
    provider: str | None

    @classmethod
    def from_service_history(cls, service: ServiceHistory[Any], use_metric: bool) -> ServiceRecord:
        return cls(
            date=service.service_date,
            odometer=_quantity_of(service.odometer, "km" if use_metric else "mi"),
            category=service.service_category,
            provider=service.service_provider,
        )


class HealthReport(BaseModel):
    warning_lights: list[str]
    warning_lights_caveat: str
    notifications: list[NotificationItem] = Field(description="Latest 10, newest first.")
    last_service: ServiceRecord | None
    service_note: str
    freshness: Freshness


class RefreshReport(BaseModel):
    refreshed: bool
    note: str
    freshness: Freshness


def _door(door: Door | None) -> DoorReport:
    if door is None:
        return DoorReport(state="unknown", lock="unknown")
    return DoorReport(state=_open_state(door.closed), lock=_lock_state(door.locked))


def _window(window: Window | None) -> OpenState:
    return _open_state(window.closed if window else None)


def _open_state(closed: bool | None) -> OpenState:
    if closed is None:
        return "unknown"
    return "closed" if closed else "open"


def _lock_state(locked: bool | None) -> LockState:
    if locked is None:
        return "unknown"
    return "locked" if locked else "unlocked"


def _overall_lock(doors: DoorsReport) -> LockState:
    states = [
        door.lock
        for door in (
            doors.driver,
            doors.passenger,
            doors.driver_rear,
            doors.passenger_rear,
            doors.trunk,
        )
    ]
    known = [state for state in states if state != "unknown"]
    if not known:
        return "unknown"
    return "locked" if all(state == "locked" for state in known) else "unlocked"


def _battery(
    dashboard: Dashboard[Any], powertrain: Powertrain
) -> tuple[BatteryStatus | None, str | None]:
    if powertrain == "full_hybrid":
        return None, FULL_HYBRID_BATTERY_NOTE
    if powertrain == "fuel_only":
        return None, FUEL_ONLY_BATTERY_NOTE
    if dashboard.battery_level is None:
        return None, BATTERY_UNAVAILABLE_NOTE
    return (
        BatteryStatus(
            level_percent=dashboard.battery_level,
            range=_quantity(dashboard.battery_range_with_unit),
            range_with_ac=_quantity(dashboard.battery_range_with_ac_with_unit),
            charging_status=dashboard.charging_status,
            remaining_charge_minutes=_minutes(dashboard.remaining_charge_time),
        ),
        None,
    )


def _quantity(distance: object) -> Quantity | None:
    if distance is None:
        return None
    value = getattr(distance, "value", None)
    unit = getattr(distance, "unit", None)
    if value is None or unit is None:
        return None
    return Quantity(value=float(value), unit=str(unit))


def _quantity_of(value: float | None, unit: str) -> Quantity | None:
    if value is None:
        return None
    return Quantity(value=round(float(value), 2), unit=unit)


def _minutes(duration: timedelta | None) -> float | None:
    if duration is None:
        return None
    return round(duration.total_seconds() / 60, 1)


def _ratio_percent(part: float | None, whole: float | None) -> float | None:
    if part is None or not whole:
        return None
    return round(part / whole * 100, 1)


def _average_consumption(
    total_fuel: float, total_distance: float, use_metric: bool
) -> Quantity | None:
    if total_distance <= 0:
        return None
    if use_metric:
        return Quantity(value=round(total_fuel / total_distance * 100, 2), unit="L/100km")
    if total_fuel <= 0:
        return None
    return Quantity(value=round(total_distance / total_fuel, 1), unit="mpg")
