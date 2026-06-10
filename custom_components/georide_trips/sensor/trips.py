"""GeoRide Trips sensors — last trip, total distance, trip count."""

import logging
from datetime import datetime

from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
)
from homeassistant.const import UnitOfLength
from homeassistant.util import dt as dt_util
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..const import (
    METERS_TO_KM,
    KNOTS_TO_KMH,
)
from ..helpers import GeoRideEntityMixin
from . import MILLISECONDS_TO_MINUTES, MILLISECONDS_TO_HOURS

_LOGGER = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════
# SENSORS — TRIPS
# ════════════════════════════════════════════════════════════════════════════


class GeoRideLastTripSensor(GeoRideEntityMixin, CoordinatorEntity, SensorEntity):
    """Sensor for last trip (simple)."""

    def __init__(self, coordinator, entry, tracker):
        super().__init__(coordinator)
        self.tracker_id = str(tracker.get("trackerId"))
        self.tracker_name = tracker.get("trackerName", f"Tracker {self.tracker_id}")
        self._entry = entry
        self._tracker = tracker
        self._attr_name = "Last trip"
        self._attr_unique_id = f"{self.tracker_id}_last_trip"
        self._attr_icon = "mdi:map-marker-path"

    @property
    def native_value(self):
        trips = self.coordinator.data
        if not trips:
            return None
        return trips[0].get("startTime")


class GeoRideLastTripDetailsSensor(GeoRideEntityMixin, CoordinatorEntity, SensorEntity):
    """Sensor for last trip with detailed info."""

    def __init__(self, coordinator, entry, tracker):
        super().__init__(coordinator)
        self.tracker_id = str(tracker.get("trackerId"))
        self.tracker_name = tracker.get("trackerName", f"Tracker {self.tracker_id}")
        self._entry = entry
        self._tracker = tracker
        self._attr_name = "Last trip details"
        self._attr_unique_id = f"{self.tracker_id}_last_trip_details"
        self._attr_icon = "mdi:map-marker-star"

    @property
    def native_value(self):
        trips = self.coordinator.data
        if not trips:
            return "No trip"
        trip = trips[0]
        distance_km = trip.get("distance", 0) / METERS_TO_KM
        duration_min = trip.get("duration", 0) / MILLISECONDS_TO_MINUTES
        return f"{distance_km:.1f} km - {duration_min:.0f} min"

    @property
    def extra_state_attributes(self):
        trips = self.coordinator.data
        if not trips:
            return {}
        trip = trips[0]

        distance_m = trip.get("distance", 0)
        distance_km = distance_m / METERS_TO_KM
        duration_ms = trip.get("duration", 0)
        duration_min = duration_ms / MILLISECONDS_TO_MINUTES
        duration_hours = duration_ms / MILLISECONDS_TO_HOURS
        avg_speed_kmh = trip.get("averageSpeed", 0) * KNOTS_TO_KMH
        max_speed_kmh = trip.get("maxSpeed", 0) * KNOTS_TO_KMH

        start_time = trip.get("startTime", "")
        end_time = trip.get("endTime", "")

        try:
            start_dt = dt_util.as_local(
                datetime.fromisoformat(start_time.replace("Z", "+00:00"))
            )
            date_formatted = start_dt.strftime("%d/%m/%Y")
            start_hour = start_dt.strftime("%H:%M")
        except Exception:
            date_formatted = ""
            start_hour = ""

        try:
            end_dt = dt_util.as_local(
                datetime.fromisoformat(end_time.replace("Z", "+00:00"))
            )
            end_hour = end_dt.strftime("%H:%M")
        except Exception:
            end_hour = ""

        time_range = f"{start_hour} - {end_hour}" if start_hour and end_hour else ""
        distance_formatted = f"{distance_km:.1f} km"
        duration_formatted = (
            f"{int(duration_min)} min"
            if duration_min < 60
            else f"{duration_hours:.1f}h"
        )
        speed_formatted = f"{avg_speed_kmh:.1f} km/h"
        summary = f"{distance_formatted} in {duration_formatted} at {speed_formatted}"

        return {
            "trip_id": trip.get("id"),
            "nice_name": trip.get("niceName", ""),
            "start_time": start_time,
            "end_time": end_time,
            "date_formatted": date_formatted,
            "start_hour": start_hour,
            "end_hour": end_hour,
            "time_range": time_range,
            "distance_km": round(distance_km, 2),
            "distance_formatted": distance_formatted,
            "duration_minutes": round(duration_min, 1),
            "duration_formatted": duration_formatted,
            "average_speed_kmh": round(avg_speed_kmh, 1),
            "max_speed_kmh": round(max_speed_kmh, 1),
            "speed_formatted": speed_formatted,
            "summary": summary,
            "trip_summary": f"{date_formatted} {time_range}",
            "start_address": trip.get("startAddress", ""),
            "end_address": trip.get("endAddress", ""),
            "start_latitude": trip.get("startLatitude") or trip.get("startLat"),
            "start_longitude": trip.get("startLongitude") or trip.get("startLon"),
            "end_latitude": trip.get("endLatitude") or trip.get("endLat"),
            "end_longitude": trip.get("endLongitude") or trip.get("endLon"),
        }


class GeoRideTotalDistanceSensor(GeoRideEntityMixin, CoordinatorEntity, SensorEntity):
    """Sensor for total distance over period."""

    def __init__(self, coordinator, entry, tracker):
        super().__init__(coordinator)
        self.tracker_id = str(tracker.get("trackerId"))
        self.tracker_name = tracker.get("trackerName", f"Tracker {self.tracker_id}")
        self._entry = entry
        self._tracker = tracker
        self._attr_name = "Distance (last 30 days)"
        self._attr_unique_id = f"{self.tracker_id}_total_distance"
        self._attr_icon = "mdi:map-marker-distance"
        self._attr_native_unit_of_measurement = UnitOfLength.KILOMETERS
        self._attr_device_class = SensorDeviceClass.DISTANCE

    @property
    def native_value(self):
        trips = self.coordinator.data
        if not trips:
            return 0
        total_m = sum(trip.get("distance", 0) for trip in trips)
        return round(total_m / METERS_TO_KM, 2)


class GeoRideTripCountSensor(GeoRideEntityMixin, CoordinatorEntity, SensorEntity):
    """Sensor for trip count over period."""

    def __init__(self, coordinator, entry, tracker):
        super().__init__(coordinator)
        self.tracker_id = str(tracker.get("trackerId"))
        self.tracker_name = tracker.get("trackerName", f"Tracker {self.tracker_id}")
        self._entry = entry
        self._tracker = tracker
        self._attr_name = "Trips (last 30 days)"
        self._attr_unique_id = f"{self.tracker_id}_trip_count"
        self._attr_icon = "mdi:counter"

    @property
    def native_value(self):
        trips = self.coordinator.data
        return len(trips) if trips else 0
