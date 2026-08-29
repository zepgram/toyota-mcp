from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

from toyota_mcp.models import (
    FUEL_ONLY_BATTERY_NOTE,
    FULL_HYBRID_BATTERY_NOTE,
    DoorReport,
    DoorsReport,
    EnergyReport,
    Freshness,
    NotificationItem,
    Quantity,
    StatusExtras,
    TripReport,
    TripsReport,
    TripSummaryReport,
    _overall_lock,
)


def _freshness() -> Freshness:
    return Freshness(fetched_at=datetime.now(UTC), age_seconds=0, source="live")


def _doors(*locks: str) -> DoorsReport:
    reports = [DoorReport(state="closed", lock=cast(Any, lock)) for lock in locks]
    return DoorsReport(
        driver=reports[0],
        passenger=reports[1],
        driver_rear=reports[2],
        passenger_rear=reports[3],
        trunk=reports[4],
    )


def test_overall_lock_all_locked() -> None:
    assert _overall_lock(_doors("locked", "locked", "locked", "locked", "locked")) == "locked"


def test_overall_lock_one_unlocked() -> None:
    assert _overall_lock(_doors("locked", "unlocked", "locked", "locked", "locked")) == "unlocked"


def test_overall_lock_partial_sensors_still_locked() -> None:
    assert _overall_lock(_doors("locked", "unknown", "locked", "unknown", "locked")) == "locked"


def test_overall_lock_no_sensor() -> None:
    assert _overall_lock(_doors("unknown", "unknown", "unknown", "unknown", "unknown")) == "unknown"


def _dashboard(**overrides: Any) -> Any:
    defaults: dict[str, Any] = {
        "fuel_level": 55,
        "fuel_range_with_unit": SimpleNamespace(value=420.0, unit="km"),
        "range_with_unit": SimpleNamespace(value=460.0, unit="km"),
        "battery_level": None,
        "battery_range_with_unit": None,
        "battery_range_with_ac_with_unit": None,
        "charging_status": None,
        "remaining_charge_time": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_energy_full_hybrid_gets_explanatory_note() -> None:
    report = EnergyReport.from_dashboard(_dashboard(), "full_hybrid", _freshness())
    assert report.battery is None
    assert report.battery_note == FULL_HYBRID_BATTERY_NOTE
    assert report.fuel_level_percent == 55
    assert report.fuel_range == Quantity(value=420.0, unit="km")
    assert report.total_range == Quantity(value=460.0, unit="km")


def test_energy_fuel_only_note() -> None:
    report = EnergyReport.from_dashboard(_dashboard(), "fuel_only", _freshness())
    assert report.battery is None
    assert report.battery_note == FUEL_ONLY_BATTERY_NOTE


def test_energy_phev_battery_populated() -> None:
    dashboard = _dashboard(
        battery_level=80.0,
        battery_range_with_unit=SimpleNamespace(value=52.0, unit="km"),
        charging_status="chargeComplete",
        remaining_charge_time=timedelta(minutes=45),
    )
    report = EnergyReport.from_dashboard(dashboard, "plug_in_hybrid", _freshness())
    assert report.battery is not None
    assert report.battery.level_percent == 80.0
    assert report.battery.range == Quantity(value=52.0, unit="km")
    assert report.battery.charging_status == "chargeComplete"
    assert report.battery.remaining_charge_minutes == 45.0
    assert report.battery_note is None


def _trip(**overrides: Any) -> Any:
    defaults: dict[str, Any] = {
        "start_time": datetime(2026, 8, 28, 8, 0, tzinfo=UTC),
        "end_time": datetime(2026, 8, 28, 8, 30, tzinfo=UTC),
        "duration": timedelta(minutes=30),
        "distance": 22.0,
        "fuel_consumed": 0.9,
        "average_fuel_consumed": 4.1,
        "ev_distance": 11.0,
        "ev_duration": timedelta(minutes=18),
        "score": 82.0,
        "score_acceleration": 75.0,
        "score_braking": 88.0,
        "score_constant_speed": 61.0,
        "locations": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_trip_report_units_and_ev_ratio() -> None:
    report = TripReport.from_trip(_trip(), use_metric=True)
    assert report.distance == Quantity(value=22.0, unit="km")
    assert report.fuel_consumed == Quantity(value=0.9, unit="L")
    assert report.average_consumption == Quantity(value=4.1, unit="L/100km")
    assert report.ev_distance == Quantity(value=11.0, unit="km")
    assert report.ev_ratio_percent == 50.0
    assert report.duration_minutes == 30.0
    assert report.ev_duration_minutes == 18.0


def test_trip_report_imperial_units() -> None:
    report = TripReport.from_trip(_trip(), use_metric=False)
    assert report.distance is not None and report.distance.unit == "mi"
    assert report.fuel_consumed is not None and report.fuel_consumed.unit == "gal"
    assert report.average_consumption is not None
    assert report.average_consumption.unit == "mpg"


def test_trips_report_orders_newest_first_and_slices() -> None:
    older = _trip(start_time=datetime(2026, 8, 20, 8, 0, tzinfo=UTC))
    newer = _trip(start_time=datetime(2026, 8, 28, 8, 0, tzinfo=UTC))
    report = TripsReport.from_trips(
        [older, newer],
        window_from=date(2026, 8, 15),
        window_to=date(2026, 8, 29),
        limit=1,
        use_metric=True,
        freshness=_freshness(),
    )
    assert report.returned_count == 1
    assert report.total_in_window == 2
    assert report.trips[0].started_at == datetime(2026, 8, 28, 8, 0, tzinfo=UTC)
    assert report.note is not None
    assert "1 most recent of 2 trips" in report.note


def test_trips_report_complete_window_has_no_note() -> None:
    report = TripsReport.from_trips(
        [_trip()],
        window_from=date(2026, 8, 15),
        window_to=date(2026, 8, 29),
        limit=10,
        use_metric=True,
        freshness=_freshness(),
    )
    assert report.total_in_window == report.returned_count == 1
    assert report.note is None


def test_trips_report_empty_window_is_typed_not_error() -> None:
    report = TripsReport.from_trips(
        [],
        window_from=date(2026, 8, 15),
        window_to=date(2026, 8, 29),
        limit=10,
        use_metric=True,
        freshness=_freshness(),
    )
    assert report.trips == []
    assert report.note is not None
    assert "No trips recorded" in report.note


def test_status_extras_tolerate_garbage() -> None:
    assert StatusExtras.from_payload(None) == StatusExtras()
    assert StatusExtras.from_payload({"lights": "nope"}) == StatusExtras()
    extras = StatusExtras.from_payload(
        {"overallStatus": "ok", "lights": {"head": {"status": "on"}}}
    )
    assert extras.overall_status == "ok"
    assert extras.lights is not None and extras.lights.head is not None
    assert extras.lights.head.status == "on"


def test_trip_hybrid_breakdown_from_raw_metres_and_seconds() -> None:
    hdc = SimpleNamespace(
        ev_distance=1311,
        ev_time=308,
        charge_dist=934,
        charge_time=208,
        eco_dist=999,
        eco_time=165,
        power_dist=239,
        power_time=20,
    )
    report = TripReport.from_trip(_trip(_trip=SimpleNamespace(hdc=hdc), locations=None), True)
    assert report.hybrid_breakdown is not None
    assert report.hybrid_breakdown.ev.distance == Quantity(value=1.31, unit="km")
    assert report.hybrid_breakdown.ev.duration_minutes == 5.1
    assert report.hybrid_breakdown.power.distance == Quantity(value=0.24, unit="km")
    assert report.start is None


def test_notification_strips_vin_toyota_prepends_to_messages() -> None:
    notification = SimpleNamespace(
        date=None,
        category="VehicleStatusAlert",
        type="alert",
        message="JTDZARBE0RJ000042: Veuillez contrôler les sièges arrière.",
        read=None,
    )
    item = NotificationItem.from_notification(cast(Any, notification))
    assert item.message == "Veuillez contrôler les sièges arrière."
    assert item.read is False


def _summary(**overrides: Any) -> Any:
    defaults: dict[str, Any] = {
        "distance": 50.0,
        "duration": timedelta(hours=1),
        "fuel_consumed": 2.0,
        "ev_distance": 25.0,
        "ev_duration": timedelta(minutes=30),
        "countries": ["FR"],
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_summary_recomputes_consumption_not_mean_of_means() -> None:
    day_short = _summary(distance=10.0, fuel_consumed=1.0)
    day_long = _summary(distance=90.0, fuel_consumed=2.0)
    report = TripSummaryReport.from_daily_summaries(
        [day_short, day_long],
        window_from=date(2026, 8, 22),
        window_to=date(2026, 8, 29),
        use_metric=True,
        freshness=_freshness(),
    )
    assert report.average_consumption == Quantity(value=3.0, unit="L/100km")
    assert report.total_distance == Quantity(value=100.0, unit="km")


def test_summary_ev_ratios_and_speed() -> None:
    report = TripSummaryReport.from_daily_summaries(
        [_summary(), _summary()],
        window_from=date(2026, 8, 22),
        window_to=date(2026, 8, 29),
        use_metric=True,
        freshness=_freshness(),
    )
    assert report.ev_ratio_percent == 50.0
    assert report.ev_time_ratio_percent == 50.0
    assert report.average_speed == Quantity(value=50.0, unit="km/h")
    assert report.days_with_driving == 2
    assert report.countries == ["FR"]


def test_summary_empty_window_zeroed_with_note() -> None:
    report = TripSummaryReport.from_daily_summaries(
        [],
        window_from=date(2026, 8, 22),
        window_to=date(2026, 8, 29),
        use_metric=True,
        freshness=_freshness(),
    )
    assert report.total_distance.value == 0.0
    assert report.average_consumption is None
    assert report.average_speed is None
    assert report.days_with_driving == 0
    assert report.note is not None
    assert "No trips recorded" in report.note
