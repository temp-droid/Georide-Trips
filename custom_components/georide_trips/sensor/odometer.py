"""GeoRide odometer sensors — lifetime odometer and real odometer."""

import logging

from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import UnitOfLength
from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..const import METERS_TO_KM
from ..helpers import GeoRideEntityMixin
from . import MILLISECONDS_TO_HOURS

_LOGGER = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════
# SENSORS — LIFETIME ODOMETER
# ════════════════════════════════════════════════════════════════════════════


class GeoRideLifetimeOdometerSensor(
    GeoRideEntityMixin, CoordinatorEntity, SensorEntity
):
    """Sensor for lifetime odometer."""

    def __init__(self, coordinator, entry, tracker):
        super().__init__(coordinator)
        self.tracker_id = str(tracker.get("trackerId"))
        self.tracker_name = tracker.get("trackerName", f"Tracker {self.tracker_id}")
        self._entry = entry
        self._tracker = tracker
        self._attr_name = "Lifetime odometer"
        self._attr_unique_id = f"{self.tracker_id}_lifetime_odometer"
        self._attr_icon = "mdi:counter"
        self._attr_native_unit_of_measurement = UnitOfLength.KILOMETERS
        self._attr_device_class = SensorDeviceClass.DISTANCE
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING

    @property
    def native_value(self):
        data = self.coordinator.data
        if not data or "trips" not in data:
            # No lifetime data yet (first fetch deferred): unknown,
            # never 0 — a 0 would be recorded as a TOTAL_INCREASING reset.
            return None
        trips = data["trips"]
        total_m = sum(trip.get("distance", 0) for trip in trips)
        return round(total_m / METERS_TO_KM, 2)

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data
        if not data or "trips" not in data:
            return {}
        trips = data["trips"]

        total_distance_m = sum(trip.get("distance", 0) for trip in trips)
        total_duration_ms = sum(trip.get("duration", 0) for trip in trips)
        total_duration_hours = round(total_duration_ms / MILLISECONDS_TO_HOURS, 2)

        if trips:
            sorted_trips = sorted(trips, key=lambda x: x.get("startTime", ""))
            first_trip_date = sorted_trips[0].get("startTime", "")
            last_trip_date = sorted_trips[-1].get("startTime", "")
        else:
            first_trip_date = ""
            last_trip_date = ""

        from_date = data.get("from_date")
        to_date = data.get("to_date")
        if from_date and to_date:
            try:
                if from_date.tzinfo is None:
                    from_date = from_date.replace(tzinfo=to_date.tzinfo)
                elif to_date.tzinfo is None:
                    to_date = to_date.replace(tzinfo=from_date.tzinfo)
                days_tracked = (to_date - from_date).days
            except Exception:
                days_tracked = 0
        else:
            days_tracked = 0

        return {
            "total_trips": len(trips),
            "total_distance_m": total_distance_m,
            "total_duration_hours": total_duration_hours,
            "total_duration_days": round(total_duration_hours / 24, 2),
            "average_distance_per_trip_km": round(
                total_distance_m / METERS_TO_KM / len(trips), 2
            )
            if trips
            else 0,
            "average_distance_per_day_km": round(
                total_distance_m / METERS_TO_KM / days_tracked, 2
            )
            if days_tracked > 0
            else 0,
            "first_trip_date": first_trip_date,
            "last_trip_date": last_trip_date,
            "days_tracked": days_tracked,
            "from_date": from_date.isoformat() if from_date else None,
            "to_date": to_date.isoformat() if to_date else None,
        }


class GeoRideRealOdometerSensor(GeoRideEntityMixin, CoordinatorEntity, SensorEntity):
    """Sensor for real odometer = lifetime base + intraday delta + offset.

    Computation strategy:
    ─ Base (lifetime coordinator, refreshed at midnight): sum of ALL trips
      since the tracker's activation. Represents yesterday's stable mileage.

    ─ Intraday delta (recent coordinator): trips whose startTime is
      later than the last trip of the lifetime base. Allows capturing
      the day's new trips as soon as they appear in the API (interval ~ 1h),
      without waiting for the next day's lifetime refresh.

    ─ Offset: value entered via number.*_odometer_offset to align with the
      motorcycle's physical odometer.

    Odometer = base_km + delta_km + offset_km

    The sensor subscribes to both coordinators: any update from one
    or the other triggers a recompute.
    """

    def __init__(
        self,
        lifetime_coordinator,
        recent_coordinator,
        status_coordinator,
        entry,
        tracker,
        hass,
    ):
        # CoordinatorEntity attaches to the lifetime coordinator (the "main" coordinator)
        super().__init__(lifetime_coordinator)
        self._recent_coordinator = recent_coordinator
        self._status_coordinator = status_coordinator
        self.tracker_id = str(tracker.get("trackerId"))
        self.tracker_name = tracker.get("trackerName", f"Tracker {self.tracker_id}")
        self._entry = entry
        self._tracker = tracker
        self._hass = hass
        self._attr_name = "Odometer"
        self._attr_unique_id = f"{self.tracker_id}_real_odometer"
        self._attr_icon = "mdi:counter"
        self._attr_native_unit_of_measurement = UnitOfLength.KILOMETERS
        self._attr_device_class = SensorDeviceClass.DISTANCE
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        # Anti-regression guard: remembers the last valid tracker_km (base + delta)
        # to reject partial updates lower than the known value.
        self._last_known_tracker_km: float | None = None
        # Offset entity_id — resolved in async_added_to_hass via the registry
        self._offset_entity_id: str | None = None
        # Flag to avoid publishing a spurious value before the offset is restored
        self._offset_ready = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        # Resolve the offset's entity_id via the registry
        from ..helpers import resolve_entity_id

        self._offset_entity_id = resolve_entity_id(
            self._hass, "number", self.tracker_id, "odometer_offset"
        )

        # Subscribe to the recent coordinator's updates for intraday trips
        self.async_on_remove(
            self._recent_coordinator.async_add_listener(
                self._handle_recent_coordinator_update
            )
        )

        # Subscribe to the status coordinator (polls /user/trackers every 5 min)
        # — that's where GeoRide's authoritative odometer field is refreshed.
        self.async_on_remove(
            self._status_coordinator.async_add_listener(
                self._handle_recent_coordinator_update
            )
        )

        # Subscribe to changes of the offset
        from homeassistant.helpers.event import async_track_state_change_event

        if self._offset_entity_id:
            self.async_on_remove(
                async_track_state_change_event(
                    self._hass,
                    [self._offset_entity_id],
                    self._handle_offset_state_change,
                )
            )

    @callback
    def _handle_recent_coordinator_update(self) -> None:
        """Triggered on each update of the recent coordinator (~ every 1h)."""
        self.async_write_ha_state()

    @callback
    def _handle_offset_state_change(self, event) -> None:
        if not self._offset_ready:
            self._offset_ready = True
            _LOGGER.debug(
                "Odometer %s: offset ready (%.2f km), first reliable publication",
                self.tracker_name,
                self._get_offset_km(),
            )
        self.async_write_ha_state()

    def _compute_tracker_km(self) -> tuple[float, float, str]:
        """Compute tracker_km (lifetime base + intraday delta) and return the details.

        Returns:
            (base_km, delta_km, last_lifetime_trip_date)
        """
        # ── Lifetime base ──────────────────────────────────────────────────
        lifetime_data = self.coordinator.data  # lifetime coordinator
        lifetime_trips = lifetime_data.get("trips", []) if lifetime_data else []
        base_km = sum(t.get("distance", 0) for t in lifetime_trips) / METERS_TO_KM

        # Date of the last known trip in the lifetime base (to filter the delta)
        if lifetime_trips:
            last_lifetime_date = max(
                t.get("endTime") or t.get("startTime", "") for t in lifetime_trips
            )
        else:
            last_lifetime_date = ""

        # ── Intraday delta ─────────────────────────────────────────────────
        recent_trips = self._recent_coordinator.data or []
        if last_lifetime_date:
            new_trips = [
                t
                for t in recent_trips
                if (t.get("startTime") or "") > last_lifetime_date
            ]
        else:
            new_trips = recent_trips

        delta_km = sum(t.get("distance", 0) for t in new_trips) / METERS_TO_KM

        if new_trips:
            _LOGGER.debug(
                "Odometer %s: base=%.1f km + delta=%.1f km (%d new trips today)",
                self.tracker_name,
                base_km,
                delta_km,
                len(new_trips),
            )

        return base_km, delta_km, last_lifetime_date

    def _compute_tracker_km_guarded(self) -> tuple[float, float, str]:
        """Like _compute_tracker_km but with an anti-regression guard.

        If the computed tracker_km (base + delta) is lower than the last
        known value, the old value is kept by forcing delta_km to the
        difference. This prevents a partial refresh mid-trip from making
        the odometer regress.

        Returns:
            (base_km, delta_km, last_lifetime_trip_date)
        """
        base_km, delta_km, last_lifetime_date = self._compute_tracker_km()
        new_tracker_km = base_km + delta_km

        if (
            self._last_known_tracker_km is not None
            and new_tracker_km < self._last_known_tracker_km
        ):
            _LOGGER.warning(
                "Odometer guard %s: regression detected (%.1f km → %.1f km), value held at %.1f km",
                self.tracker_name,
                self._last_known_tracker_km,
                new_tracker_km,
                self._last_known_tracker_km,
            )
            # Force delta to maintain the known value
            delta_km = self._last_known_tracker_km - base_km
        else:
            self._last_known_tracker_km = new_tracker_km

        return base_km, delta_km, last_lifetime_date

    def _get_offset_km(self) -> float:
        if not self._offset_entity_id:
            return 0.0
        offset = self._hass.states.get(self._offset_entity_id)
        return (
            float(offset.state)
            if offset and offset.state not in (None, "unknown", "unavailable")
            else 0.0
        )

    def _get_georide_odometer_km(self) -> float | None:
        """Real odometer from GeoRide's own ``odometer`` field (meters), or None.

        The status coordinator returns the raw /user/trackers tracker dict, which
        carries GeoRide's server-side odometer — the exact value shown in the
        GeoRide app. Authoritative and self-correcting; no manual offset needed.
        Returns None when the field is absent/zero so we fall back to trip sums.
        """
        data = self._status_coordinator.data if self._status_coordinator else None
        raw = data.get("odometer") if data else None
        if not raw:
            return None
        try:
            km = float(raw) / METERS_TO_KM
        except (ValueError, TypeError):
            return None
        return km if km > 0 else None

    @property
    def native_value(self):
        # As long as the offset has not been restored, do not publish a value
        # to avoid a spike in the history.
        # Exception: if offset_entity_id is None (no offset configured), we are ready.
        if self._offset_entity_id and not self._offset_ready:
            offset = self._hass.states.get(self._offset_entity_id)
            if offset and offset.state not in (None, "unknown", "unavailable"):
                self._offset_ready = True
            else:
                return None

        offset_km = self._get_offset_km()

        # Primary source: GeoRide's own odometer field — authoritative, matches
        # the app, self-correcting, no manual offset needed.
        georide_km = self._get_georide_odometer_km()
        if georide_km is not None:
            return round(georide_km + offset_km, 2)

        # Fallback: lifetime trip sum + intraday delta (legacy) for trackers that
        # don't expose the odometer field. Needs the lifetime base loaded first.
        if not self.coordinator.data:
            return None
        base_km, delta_km, _ = self._compute_tracker_km_guarded()
        return round(base_km + delta_km + offset_km, 2)

    @property
    def extra_state_attributes(self):
        georide_km = self._get_georide_odometer_km()
        odometer_source = "georide" if georide_km is not None else "computed"
        # Expose the GeoRide-based value even before the lifetime trips load.
        if not self.coordinator.data:
            return {
                "odometer_source": odometer_source,
                "georide_odometer_km": round(georide_km, 2)
                if georide_km is not None
                else None,
                "offset_km": round(self._get_offset_km(), 2),
            }
        base_km, delta_km, last_lifetime_date = self._compute_tracker_km_guarded()
        offset_km = self._get_offset_km()
        offset_entity_id = self._offset_entity_id or "unknown"

        lifetime_data = self.coordinator.data
        lifetime_trips = lifetime_data.get("trips", []) if lifetime_data else []
        recent_trips = self._recent_coordinator.data or []
        if last_lifetime_date:
            new_trips = [
                t
                for t in recent_trips
                if (t.get("startTime") or "") > last_lifetime_date
            ]
        else:
            new_trips = recent_trips

        total_duration_ms = sum(
            t.get("duration", 0) for t in lifetime_trips + new_trips
        )
        total_duration_hours = round(total_duration_ms / MILLISECONDS_TO_HOURS, 2)

        all_trips = lifetime_trips + new_trips
        if all_trips:
            sorted_all = sorted(all_trips, key=lambda x: x.get("startTime", ""))
            first_trip_date = sorted_all[0].get("startTime", "")
            last_trip_date = sorted_all[-1].get("startTime", "")
        else:
            first_trip_date = ""
            last_trip_date = ""

        return {
            "total_trips": len(all_trips),
            "total_duration_hours": total_duration_hours,
            "first_trip_date": first_trip_date,
            "last_trip_date": last_trip_date,
            "base_km": round(base_km, 2),
            "delta_km_today": round(delta_km, 2),
            "tracker_km": round(base_km + delta_km, 2),
            "offset_km": round(offset_km, 2),
            "offset_entity": offset_entity_id,
            "last_lifetime_sync": last_lifetime_date,
            "odometer_source": odometer_source,
            "georide_odometer_km": round(georide_km, 2)
            if georide_km is not None
            else None,
        }
