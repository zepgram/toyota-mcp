from __future__ import annotations

import contextlib
import re
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pytoyoda.models.endpoints.vehicle_guid import VehicleGuidModel

from toyota_mcp.opendata import FuelKind, FuelStation

if TYPE_CHECKING:
    from pytoyoda.models.climate import ClimateSettings, ClimateStatus
    from pytoyoda.models.dashboard import Dashboard
    from pytoyoda.models.electric_status import ElectricStatus
    from pytoyoda.models.endpoints.electric import ChargingSchedule
    from pytoyoda.models.location import Location
    from pytoyoda.models.lock_status import Door, LockStatus, Window
    from pytoyoda.models.nofication import Notification
    from pytoyoda.models.service_history import ServiceHistory
    from pytoyoda.models.summary import Summary
    from pytoyoda.models.trips import Trip
    from pytoyoda.models.vehicle import Vehicle

OpenState = Literal["closed", "open", "unknown"]
LockState = Literal["locked", "unlocked", "unknown"]
Powertrain = Literal["full_hybrid", "plug_in_hybrid", "electric", "fuel_only", "unknown"]
KNOWN_POWERTRAINS = ("full_hybrid", "plug_in_hybrid", "electric", "fuel_only")

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
_VIN_PREFIX = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b\s*:?\s*")
CAPABILITIES_NOTE = (
    "Capability flags come from Toyota's vehicle record and are known to be unreliable "
    "(Toyota's two flag sets disagree on the same car) — only a live command proves support."
)
CLIMATE_NOTE = (
    "Remote pre-conditioning state plus the preset a remote start would apply; "
    "toyota_start_climate / toyota_stop_climate act on it."
)
HYBRID_BREAKDOWN_NOTE = (
    "Toyota hybrid coaching split of the trip: electric-only driving, battery charging, "
    "eco and power driving."
)
OPEN_DATA_DISABLED = (
    "Address enrichment is off. Start the server with --addresses osm (OpenStreetMap, "
    "worldwide) or --addresses fr (French address base plus fuel prices) — it sends the "
    "car's position to that service."
)
LightState = Literal["on", "off", "unknown"]
CommandStatus = Literal["needs_confirmation", "verified", "accepted", "failed"]
PLUG_IN_POWERTRAINS: tuple[Powertrain, ...] = ("plug_in_hybrid", "electric")
ELECTRIC_NOT_APPLICABLE = (
    "Not applicable: this powertrain has no plug-in battery — charging data only exists "
    "for plug-in hybrids and electric vehicles."
)
ELECTRIC_NOT_REPORTED = (
    "The vehicle has not reported its charging state yet — try toyota_wake_vehicle, or ask "
    "again later."
)
CHARGING_UNTESTED_NOTE = (
    "Charging data follows pytoyoda's model of Toyota's electric endpoint; field semantics "
    "(e.g. charging_status values) are Toyota's own."
)
CONFIRMATION_NOTE = "Nothing was sent. Ask the user to confirm, then call again with confirm=true."


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


class _RawComponent(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    status: str | None = None
    last_update_timestamp: datetime | None = Field(default=None, alias="lastUpdateTimestamp")


class _RawLights(BaseModel):
    model_config = ConfigDict(extra="ignore")

    hazard: _RawComponent | None = None
    tail: _RawComponent | None = None
    head: _RawComponent | None = None


class _RawRearSeatReminder(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    warning: bool | None = None
    reason: str | None = None
    last_update_timestamp: datetime | None = Field(default=None, alias="lastUpdateTimestamp")


class StatusExtras(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    overall_status: str | None = Field(default=None, alias="overallStatus")
    overall_warning_counts: int | None = Field(default=None, alias="overallWarningCounts")
    lights: _RawLights | None = None
    rear_seat_reminder: _RawRearSeatReminder | None = Field(default=None, alias="rearSeatReminder")
    reported_at: datetime | None = Field(
        default=None, description="Newest lastUpdateTimestamp anywhere in the status payload."
    )

    @classmethod
    def from_payload(cls, payload: object) -> StatusExtras:
        if not isinstance(payload, dict):
            return cls()
        try:
            extras = cls.model_validate(payload)
        except ValidationError:
            extras = cls()
        extras.reported_at = _latest_timestamp(payload)
        return extras

    def is_newer_than(self, other: StatusExtras) -> bool:
        return self.reported_at is not None and (
            other.reported_at is None or self.reported_at > other.reported_at
        )


def _latest_timestamp(node: object) -> datetime | None:
    found: list[datetime] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "lastUpdateTimestamp" and isinstance(value, str):
                with contextlib.suppress(ValueError):
                    found.append(datetime.fromisoformat(value))
            else:
                nested = _latest_timestamp(value)
                if nested is not None:
                    found.append(nested)
    elif isinstance(node, list):
        found.extend(stamp for item in node if (stamp := _latest_timestamp(item)) is not None)
    return max(found, default=None)


class LightReport(BaseModel):
    state: LightState
    reported_at: datetime | None


class LightsReport(BaseModel):
    hazard: LightReport
    tail: LightReport
    head: LightReport


class RearSeatReminderReport(BaseModel):
    warning: bool | None = Field(
        description="True when the car flagged something left on the rear seats."
    )
    reason: str | None = Field(description="Raw Toyota reason, e.g. 'notDetected'.")
    reported_at: datetime | None


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
    lights: LightsReport | None = Field(
        default=None, description="Hazard, tail and head lights — 'on' means left on while parked."
    )
    rear_seat_reminder: RearSeatReminderReport | None = None
    overall_status: str | None = Field(
        default=None, description="Toyota's aggregate verdict for the car, e.g. 'ok'."
    )
    warning_count: int | None = None
    freshness: Freshness

    @classmethod
    def from_lock_status(
        cls, lock_status: LockStatus, extras: StatusExtras, freshness: Freshness
    ) -> StatusReport:
        windows = lock_status.windows
        door_reports = _doors_report(lock_status)
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
            lights=_lights(extras.lights),
            rear_seat_reminder=(
                RearSeatReminderReport(
                    warning=extras.rear_seat_reminder.warning,
                    reason=extras.rear_seat_reminder.reason,
                    reported_at=extras.rear_seat_reminder.last_update_timestamp,
                )
                if extras.rear_seat_reminder
                else None
            ),
            overall_status=extras.overall_status,
            warning_count=extras.overall_warning_counts,
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
            freshness=freshness.with_vehicle_time(telemetry_reported_at(dashboard)),
        )


SIGN_IN_STEPS = (
    "1. Open the link and sign in on Toyota's own page — the password never reaches this "
    "server. 2. Toyota then redirects to an address the browser cannot open "
    "(com.toyota.oneapp:/…) and shows an error: that is expected. 3. Copy that whole address "
    "from the address bar and pass it to toyota_complete_sign_in."
)


class SignInStart(BaseModel):
    sign_in_url: str = Field(description="Toyota's sign-in page; give this link to the user.")
    instructions: str
    next_tool: str

    @classmethod
    def create(cls, url: str) -> SignInStart:
        return cls(sign_in_url=url, instructions=SIGN_IN_STEPS, next_tool="toyota_complete_sign_in")


class VehicleSummary(BaseModel):
    name: str = Field(description="Alias set in the MyToyota app, or 'unnamed'.")
    vin_suffix: str = Field(description="Last four characters of the VIN; use it to select.")
    powertrain: Powertrain
    model: str | None
    selected: bool

    @classmethod
    def create(cls, vehicle: Vehicle[Any], selected_vin: str | None) -> VehicleSummary:
        record = getattr(vehicle, "_vehicle_info", None)
        kind = vehicle.type if vehicle.type in KNOWN_POWERTRAINS else "unknown"
        return cls(
            name=(vehicle.alias if vehicle.alias != vehicle.vin else None) or "unnamed",
            vin_suffix=(vehicle.vin or "????")[-4:],
            powertrain=cast(Powertrain, kind),
            model=getattr(record, "car_model_name", None),
            selected=bool(vehicle.vin) and vehicle.vin == selected_vin,
        )


class VehicleListReport(BaseModel):
    vehicles: list[VehicleSummary]
    selected: str | None = Field(description="vin_suffix of the vehicle the tools act on.")
    note: str | None = None

    @classmethod
    def create(cls, vehicles: list[Vehicle[Any]], selected_vin: str | None) -> VehicleListReport:
        summaries = [VehicleSummary.create(vehicle, selected_vin) for vehicle in vehicles]
        chosen = next((summary for summary in summaries if summary.selected), None)
        note = None
        if chosen is None and len(summaries) > 1:
            note = "No vehicle is selected — call toyota_select_vehicle with a vin_suffix."
        elif chosen is None and len(summaries) == 1:
            note = "Only one vehicle on this account; every tool uses it."
        return cls(vehicles=summaries, selected=chosen.vin_suffix if chosen else None, note=note)


class VehicleChoice(BaseModel):
    name: str
    vin_suffix: str
    powertrain: Powertrain
    note: str

    @classmethod
    def create(cls, vehicle: Vehicle[Any]) -> VehicleChoice:
        summary = VehicleSummary.create(vehicle, vehicle.vin)
        return cls(
            name=summary.name,
            vin_suffix=summary.vin_suffix,
            powertrain=summary.powertrain,
            note="Every tool now acts on this vehicle, and will again after a restart.",
        )


class SignInReport(BaseModel):
    account: str | None
    vehicles: list[VehicleSummary]
    session_stored_in: str
    note: str

    @classmethod
    def create(
        cls, account: str | None, vehicles: list[Vehicle[Any]], location: str
    ) -> SignInReport:
        summaries = [
            VehicleSummary.create(vehicle, vehicles[0].vin if len(vehicles) == 1 else None)
            for vehicle in vehicles
        ]
        if not summaries:
            note = "Signed in, but this account has no vehicle — pair one in the MyToyota app."
        elif len(summaries) == 1:
            note = "Signed in. Every tool acts on this vehicle."
        else:
            note = "Signed in. Several vehicles: call toyota_select_vehicle to pick one."
        return cls(account=account, vehicles=summaries, session_stored_in=location, note=note)


class SubscriptionReport(BaseModel):
    product: str | None
    status: str | None
    ends_on: date | None
    remaining_days: int | None


class RemoteCapabilities(BaseModel):
    lock_unlock: bool | None
    trunk_lock_unlock: bool | None
    hazard_lights: bool | None
    horn_or_buzzer: bool | None
    climate: bool | None
    climate_full_temperature_control: bool | None
    vehicle_finder: bool | None
    last_parked_position: bool | None
    telemetry: bool | None
    note: str


class VehicleInfoReport(BaseModel):
    alias: str | None
    vin_suffix: str = Field(description="Last four characters of the VIN.")
    model: str | None
    model_year: str | None
    registration_number: str | None
    colour: str | None
    fuel_type_code: str | None = Field(description="Raw Toyota code, e.g. 'B' for petrol.")
    powertrain: Powertrain
    date_of_first_use: date | None
    manufactured_on: date | None
    image_url: str | None
    connected_services_status: str | None
    remote_services_status: str | None
    subscriptions: list[SubscriptionReport]
    remote_capabilities: RemoteCapabilities

    @classmethod
    def from_vehicle(cls, vehicle: Vehicle[Any], powertrain: Powertrain) -> VehicleInfoReport:
        # pytoyoda keeps the vehicle record only on this private attribute.
        record = getattr(vehicle, "_vehicle_info", None)
        info = record if isinstance(record, VehicleGuidModel) else None
        capabilities = info.extended_capabilities if info else None
        return cls(
            alias=vehicle.alias if vehicle.alias != vehicle.vin else None,
            vin_suffix=(vehicle.vin or "????")[-4:],
            model=info.car_model_name if info else None,
            model_year=info.car_model_year if info else None,
            registration_number=info.registration_number if info else None,
            colour=info.color if info else None,
            fuel_type_code=info.fuel_type if info else None,
            powertrain=powertrain,
            date_of_first_use=info.date_of_first_use if info else None,
            manufactured_on=info.manufactured_date if info else None,
            image_url=info.image if info else None,
            connected_services_status=info.subscription_status if info else None,
            remote_services_status=info.remote_subscription_status if info else None,
            subscriptions=[
                SubscriptionReport(
                    product=subscription.display_procuct_name or subscription.product_name,
                    status=subscription.status,
                    ends_on=subscription.subscription_end_date,
                    remaining_days=subscription.subscription_remaining_days,
                )
                for subscription in (info.subscriptions if info else None) or []
            ],
            remote_capabilities=RemoteCapabilities(
                lock_unlock=getattr(capabilities, "door_lock_unlock_capable", None),
                trunk_lock_unlock=getattr(capabilities, "trunk_lock_unlock_capable", None),
                hazard_lights=getattr(capabilities, "hazard_capable", None),
                horn_or_buzzer=_any_true(
                    getattr(capabilities, "horn_capable", None),
                    getattr(capabilities, "buzzer_capable", None),
                ),
                climate=getattr(capabilities, "climate_capable", None),
                climate_full_temperature_control=getattr(
                    capabilities, "climate_temperature_control_full", None
                ),
                vehicle_finder=getattr(capabilities, "vehicle_finder", None),
                last_parked_position=getattr(capabilities, "last_parked_capable", None),
                telemetry=getattr(capabilities, "telemetry_capable", None),
                note=CAPABILITIES_NOTE,
            ),
        )


class ClimateOptions(BaseModel):
    front_defroster: bool | None
    rear_defogger: bool | None
    steering_heater: bool | None
    driver_seat: str | None
    passenger_seat: str | None
    rear_driver_seat: str | None
    rear_passenger_seat: str | None


class ClimatePreset(BaseModel):
    temperature: Quantity | None
    duration_minutes: float | None
    options: ClimateOptions | None


class ClimateReport(BaseModel):
    status: str | None = Field(description="Raw Toyota state, e.g. 'stopped', 'running'.")
    is_on: bool | None
    started_at: datetime | None
    duration_minutes: float | None = Field(
        default=None, description="Programmed run time of the current session."
    )
    current_temperature: Quantity | None
    target_temperature: Quantity | None
    preset: ClimatePreset | None = Field(description="What a remote start would apply.")
    note: str
    freshness: Freshness

    @classmethod
    def from_climate(
        cls, status: ClimateStatus, settings: ClimateSettings, freshness: Freshness
    ) -> ClimateReport:
        return cls(
            status=status.status,
            is_on=status.is_on,
            started_at=status.started_at,
            duration_minutes=_minutes(status.duration),
            current_temperature=_quantity(status.current_temperature),
            target_temperature=_quantity(status.target_temperature),
            preset=ClimatePreset(
                temperature=_quantity(settings.temperature),
                duration_minutes=_minutes(settings.duration),
                options=_climate_options(settings),
            ),
            note=CLIMATE_NOTE,
            freshness=freshness.with_vehicle_time(status.updated_at),
        )


class ChargingScheduleReport(BaseModel):
    id: int
    enabled: bool
    type: str = Field(description="'startOnly' or 'startEnd'.")
    start: str = Field(description="Local time HH:MM.")
    end: str | None = Field(default=None, description="Local time HH:MM, for startEnd.")
    days: list[str] = Field(description="Weekdays the schedule applies to.")


class ChargingReport(BaseModel):
    powertrain: Powertrain
    battery_level_percent: float | None
    charging_status: str | None = Field(description="Raw Toyota status, e.g. 'none', 'charging'.")
    ev_range: Quantity | None
    ev_range_with_ac: Quantity | None
    remaining_charge_minutes: float | None = Field(
        default=None, description="Minutes until fully charged, when charging."
    )
    fuel_level_percent: float | None = Field(default=None, description="Plug-in hybrids only.")
    can_set_next_charging_event: bool | None
    next_charging_event: str | None = Field(
        default=None, description="Type and time of the next scheduled charging event."
    )
    next_scheduled_window: str | None = Field(
        default=None, description="Next active schedule window (start → end), if any."
    )
    schedules: list[ChargingScheduleReport]
    note: str
    freshness: Freshness

    @classmethod
    def from_electric_status(
        cls, status: ElectricStatus[Any], powertrain: Powertrain, freshness: Freshness
    ) -> ChargingReport:
        event = status.next_charging_event
        window = status.active_scheduled_charging
        return cls(
            powertrain=powertrain,
            battery_level_percent=status.battery_level,
            charging_status=status.charging_status,
            ev_range=_quantity(status.ev_range_with_unit),
            ev_range_with_ac=_quantity(status.ev_range_with_ac_with_unit),
            remaining_charge_minutes=(
                float(status.remaining_charge_time)
                if status.remaining_charge_time is not None
                else None
            ),
            fuel_level_percent=_electric_fuel_level(status),
            can_set_next_charging_event=status.can_set_next_charging_event,
            next_charging_event=(
                f"{event.event_type} at {event.timestamp.isoformat()}" if event else None
            ),
            next_scheduled_window=(
                f"{window.start.isoformat()} → {window.end.isoformat() if window.end else '?'}"
                if window
                else None
            ),
            schedules=[_schedule(schedule) for schedule in status.charging_schedules or []],
            note=CHARGING_UNTESTED_NOTE,
            freshness=freshness.with_vehicle_time(status.last_update_timestamp),
        )


class OdometerReport(BaseModel):
    odometer: Quantity
    freshness: Freshness


class LocationReport(BaseModel):
    latitude: float
    longitude: float
    place_label: str | None = Field(default=None, description="Toyota's place name, if any.")
    place: str | None = Field(
        default=None, description="Named place from --places within 200 m, e.g. 'home'."
    )
    address: str | None = Field(
        default=None, description="Postal address; needs --addresses osm or fr."
    )
    google_maps_url: str
    freshness: Freshness

    @classmethod
    def from_location(
        cls,
        latitude: float,
        longitude: float,
        location: Location,
        freshness: Freshness,
        address: str | None = None,
        place: str | None = None,
    ) -> LocationReport:
        return cls(
            latitude=latitude,
            longitude=longitude,
            place_label=location.state,
            place=place,
            address=address,
            google_maps_url=f"https://www.google.com/maps?q={latitude},{longitude}",
            freshness=freshness.with_vehicle_time(location.timestamp, LOCATION_NOTE),
        )


class TripPlace(BaseModel):
    latitude: float | None
    longitude: float | None
    place: str | None = Field(default=None, description="Named place from --places.")
    address: str | None = Field(
        default=None, description="Postal address; needs --addresses osm or fr."
    )


class HybridModeShare(BaseModel):
    distance: Quantity | None
    duration_minutes: float | None


class HybridBreakdown(BaseModel):
    ev: HybridModeShare = Field(description="Electric-only driving.")
    charging: HybridModeShare = Field(description="Engine charging the battery.")
    eco: HybridModeShare
    power: HybridModeShare
    note: str


class TripReport(BaseModel):
    started_at: datetime | None
    ended_at: datetime | None
    start: TripPlace | None = None
    end: TripPlace | None = None
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
    hybrid_breakdown: HybridBreakdown | None = None

    @classmethod
    def from_trip(cls, trip: Trip[Any], use_metric: bool) -> TripReport:
        distance_unit = "km" if use_metric else "mi"
        locations = trip.locations
        return cls(
            started_at=trip.start_time,
            ended_at=trip.end_time,
            start=_place(locations.start if locations else None),
            end=_place(locations.end if locations else None),
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
            hybrid_breakdown=_hybrid_breakdown(trip, use_metric),
        )


class LastTripReport(BaseModel):
    trip: TripReport
    freshness: Freshness


class TripsReport(BaseModel):
    window_from: date
    window_to: date
    trips: list[TripReport] = Field(description="Newest first.")
    returned_count: int
    total_in_window: int = Field(description="Trips recorded in the window before applying limit.")
    note: str | None = Field(
        default=None, description="Set when the window is empty or the list was truncated by limit."
    )
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
        elif len(trips) > len(reports):
            note = (
                f"Showing the {len(reports)} most recent of {len(trips)} trips in the window — "
                "raise limit (max 50) to see more."
            )
        return cls(
            window_from=window_from,
            window_to=window_to,
            trips=reports,
            returned_count=len(reports),
            total_in_window=len(trips),
            note=note,
            retention_note=RETENTION_NOTE,
            freshness=freshness,
        )


CalendarPeriod = Literal["today", "this_week", "this_month", "this_year"]


class TripSummaryReport(BaseModel):
    period: CalendarPeriod | None = Field(
        default=None, description="Set when the window is a calendar period (as in the app)."
    )
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


class CommandReport(BaseModel):
    command: str
    status: CommandStatus = Field(
        description=(
            "'needs_confirmation' = nothing was sent; 'verified' = the car reported the new "
            "state; 'accepted' = Toyota accepted the command but the car has not confirmed it "
            "yet; 'failed' = the car rejected it."
        )
    )
    detail: str
    doors: StatusReport | None = None
    climate: ClimateReport | None = None
    charging: ChargingReport | None = None
    elapsed_seconds: float | None = None


class FuelStationsReport(BaseModel):
    fuel: FuelKind
    radius_km: int
    around: TripPlace = Field(description="The car's last parked position.")
    stations: list[FuelStation] = Field(description="Cheapest first.")
    note: str
    freshness: Freshness


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
            message=_VIN_PREFIX.sub("", notification.message) if notification.message else None,
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
    engine_oil_indicators: list[str] = Field(
        description="Raw oil-quantity indicators reported by the car; empty means nothing flagged."
    )
    notifications: list[NotificationItem] = Field(description="Latest 10, newest first.")
    last_service: ServiceRecord | None
    service_history: list[ServiceRecord] = Field(description="All recorded services, newest first.")
    service_note: str
    freshness: Freshness


class RefreshReport(BaseModel):
    refreshed: bool
    note: str
    freshness: Freshness


def telemetry_reported_at(dashboard: Dashboard[Any]) -> datetime | None:
    # pytoyoda only carries the telemetry timestamp on this private attribute.
    telemetry = getattr(dashboard, "_telemetry", None)
    reported_at = getattr(telemetry, "timestamp", None)
    return reported_at if isinstance(reported_at, datetime) else None


def _electric_fuel_level(status: ElectricStatus[Any]) -> float | None:
    # pytoyoda does not surface the PHEV fuel level of the electric endpoint.
    raw = getattr(getattr(status, "_electric_status", None), "fuel_level", None)
    return float(raw) if isinstance(raw, int | float) else None


def _schedule(schedule: ChargingSchedule) -> ChargingScheduleReport:
    days = schedule.days
    names = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    return ChargingScheduleReport(
        id=schedule.id,
        enabled=schedule.enabled,
        type=schedule.type,
        start=f"{schedule.start_time.hour:02d}:{schedule.start_time.minute:02d}",
        end=(
            f"{schedule.end_time.hour:02d}:{schedule.end_time.minute:02d}"
            if schedule.end_time
            else None
        ),
        days=[name for name in names if getattr(days, name, 0)],
    )


def trunk_lock_state(lock_status: LockStatus) -> LockState:
    doors = lock_status.doors
    return _lock_state(doors.trunk.locked if doors and doors.trunk else None)


def windows_state(lock_status: LockStatus) -> OpenState:
    windows = lock_status.windows
    states = [
        _open_state(window.closed if window else None)
        for window in (
            windows.driver_seat if windows else None,
            windows.passenger_seat if windows else None,
            windows.driver_rear_seat if windows else None,
            windows.passenger_rear_seat if windows else None,
        )
    ]
    known = [state for state in states if state != "unknown"]
    if not known:
        return "unknown"
    return "closed" if all(state == "closed" for state in known) else "open"


def lock_state_of(lock_status: LockStatus, doors_only: bool = False) -> LockState:
    report = _doors_report(lock_status)
    if doors_only:
        report = report.model_copy(update={"trunk": DoorReport(state="unknown", lock="unknown")})
    return _overall_lock(report)


def _doors_report(lock_status: LockStatus) -> DoorsReport:
    doors = lock_status.doors
    return DoorsReport(
        driver=_door(doors.driver_seat if doors else None),
        passenger=_door(doors.passenger_seat if doors else None),
        driver_rear=_door(doors.driver_rear_seat if doors else None),
        passenger_rear=_door(doors.passenger_rear_seat if doors else None),
        trunk=_door(doors.trunk if doors else None),
    )


def _lights(raw: _RawLights | None) -> LightsReport | None:
    if raw is None:
        return None
    return LightsReport(hazard=_light(raw.hazard), tail=_light(raw.tail), head=_light(raw.head))


def _light(component: _RawComponent | None) -> LightReport:
    status = component.status if component else None
    state: LightState = "unknown"
    if status == "on":
        state = "on"
    elif status == "off":
        state = "off"
    return LightReport(
        state=state, reported_at=component.last_update_timestamp if component else None
    )


def _any_true(*flags: bool | None) -> bool | None:
    known = [flag for flag in flags if flag is not None]
    if not known:
        return None
    return any(known)


def _climate_options(settings: ClimateSettings) -> ClimateOptions | None:
    heating = settings.heating_options
    seats = settings.seat_options
    if heating is None and seats is None:
        return None
    return ClimateOptions(
        front_defroster=heating.front_defroster if heating else None,
        rear_defogger=heating.rear_defogger if heating else None,
        steering_heater=heating.steering_heater if heating else None,
        driver_seat=seats.driver_seat if seats else None,
        passenger_seat=seats.passenger_seat if seats else None,
        rear_driver_seat=seats.rear_driver_seat if seats else None,
        rear_passenger_seat=seats.rear_passenger_seat if seats else None,
    )


def _place(position: object) -> TripPlace | None:
    latitude = getattr(position, "lat", None)
    longitude = getattr(position, "lon", None)
    if latitude is None or longitude is None:
        return None
    return TripPlace(latitude=float(latitude), longitude=float(longitude))


def _hybrid_breakdown(trip: Trip[Any], use_metric: bool) -> HybridBreakdown | None:
    # pytoyoda only exposes the EV share; the full coaching split lives on the raw trip.
    hdc = getattr(getattr(trip, "_trip", None), "hdc", None)
    if hdc is None:
        return None

    def share(distance_metres: object, seconds: object) -> HybridModeShare:
        return HybridModeShare(
            distance=_distance_from_metres(distance_metres, use_metric),
            duration_minutes=(
                round(float(seconds) / 60, 1) if isinstance(seconds, int | float) else None
            ),
        )

    return HybridBreakdown(
        ev=share(hdc.ev_distance, hdc.ev_time),
        charging=share(hdc.charge_dist, hdc.charge_time),
        eco=share(hdc.eco_dist, hdc.eco_time),
        power=share(hdc.power_dist, hdc.power_time),
        note=HYBRID_BREAKDOWN_NOTE,
    )


def _distance_from_metres(metres: object, use_metric: bool) -> Quantity | None:
    if not isinstance(metres, int | float):
        return None
    if use_metric:
        return Quantity(value=round(metres / 1000, 2), unit="km")
    return Quantity(value=round(metres / 1609.344, 2), unit="mi")


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
