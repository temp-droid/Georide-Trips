"""GeoRide Trips device tracker - GPS position of the motorcycle via Socket.IO.

device_tracker entity attached to each motorcycle's device:
- Real-time GPS position via Socket.IO events (event "position")
- Fallback: initial fetch via REST API at startup
- No polling: updates arrive as soon as GeoRide sends a position

Filters applied to Socket.IO events:
- GPS accuracy (radius): positions that are too imprecise are ignored entirely
- moving=False status: attributes updated in memory, HA state NOT written
  → no recorder entry when the motorcycle is stopped → no more stray map lines
- Minimum distance: GPS micro-drift ignored (configurable threshold, default 10m)
"""

import logging
import math

from homeassistant.components.device_tracker import SourceType
from homeassistant.components.device_tracker.config_entry import TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import GeoRideTripsAPI
from .const import (
    CONF_GPS_MIN_ACCURACY,
    DEFAULT_GPS_MIN_ACCURACY,
    CONF_GPS_MIN_DISTANCE,
    DEFAULT_GPS_MIN_DISTANCE,
)
from .data import GeoRideConfigEntry
from .helpers import GeoRideEntityMixin

_LOGGER = logging.getLogger(__name__)


def _haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compute the distance in meters between two GPS coordinates (Haversine formula)."""
    R = 6_371_000  # Earth radius in meters
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GeoRideConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up GeoRide Trips device tracker from a config entry."""
    data = entry.runtime_data
    trackers = data.trackers
    api: GeoRideTripsAPI = data.api

    entities = []
    for tracker in trackers:
        entities.append(GeoRidePositionTracker(hass, entry, tracker, api))

    async_add_entities(entities)
    _LOGGER.info(
        "Added %d device_tracker entities for %d trackers", len(entities), len(trackers)
    )


class GeoRidePositionTracker(GeoRideEntityMixin, TrackerEntity):
    """Device tracker representing the GPS position of a GeoRide motorcycle.

    Updates via the Socket.IO "position" event — real-time, no polling.
    Initial fallback via REST API to have a position right from startup.

    Active filters (in order of application):
    1. GPS accuracy (radius > threshold) → ignored entirely
    2. moving=False → attributes updated in memory, HA state NOT written
    3. Distance < min_threshold → micro-drift ignored, HA state NOT written
    4. Otherwise → async_write_ha_state() → recorder entry
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        tracker: dict,
        api: GeoRideTripsAPI,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._tracker = tracker
        self._api = api
        self._socket_manager = None

        self._tracker_id = str(tracker.get("trackerId"))
        self._tracker_name = tracker.get("trackerName", f"Tracker {self._tracker_id}")
        # Mixin-required public attributes
        self.tracker_id = self._tracker_id
        self.tracker_name = self._tracker_name

        self._attr_unique_id = f"{self._tracker_id}_position"
        self._attr_name = "Position"
        self._attr_icon = "mdi:motorbike"

        # Position
        self._latitude: float | None = None
        self._longitude: float | None = None
        self._gps_accuracy: int = 0
        self._fix_time: str | None = None
        self._speed: float | None = None
        self._heading: float | None = None
        self._altitude: float | None = None
        self._is_moving: bool = False

        # Socket.IO unregistration
        self._unsub_socket: list = []

    # ── TrackerEntity properties ─────────────────────────────────────────────

    @property
    def latitude(self) -> float | None:
        return self._latitude

    @property
    def longitude(self) -> float | None:
        return self._longitude

    @property
    def source_type(self) -> SourceType:
        return SourceType.GPS

    @property
    def gps_accuracy(self) -> int:
        return self._gps_accuracy

    @property
    def extra_state_attributes(self) -> dict:
        attrs = {
            "tracker_id": self._tracker_id,
            "is_moving": self._is_moving,
            "source": "socket.io",
        }
        if self._fix_time:
            attrs["fix_time"] = self._fix_time
        if self._speed is not None:
            attrs["speed_kmh"] = round(self._speed, 1)  # already in km/h
        if self._heading is not None:
            attrs["heading"] = self._heading
        if self._altitude is not None:
            attrs["altitude"] = self._altitude
        return attrs

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def async_added_to_hass(self) -> None:
        """Startup: initial position + Socket.IO subscription."""
        await super().async_added_to_hass()

        # Fetch the socket_manager from runtime_data (available here, after full setup)
        self._socket_manager = self._entry.runtime_data.socket_manager

        # Subscribe to position events via Socket.IO
        if self._socket_manager:
            unsub = self._socket_manager.register_callback(
                self._tracker_id, "position", self._handle_position_event
            )
            self._unsub_socket.append(unsub)
            _LOGGER.info(
                "Device tracker '%s' subscribed to Socket.IO position events",
                self._tracker_name,
            )
        else:
            _LOGGER.warning(
                "No socket_manager available for '%s' — no real-time updates",
                self._tracker_name,
            )

        # Initial position via REST API as a background task: get_last_position
        # chains several API calls and must not block adding the entity
        # (a Socket.IO event may provide it sooner, all the better).
        self._entry.async_create_background_task(
            self.hass,
            self._async_fetch_initial_position(),
            name=f"georide_trips_initial_position_{self._tracker_id}",
        )

    async def async_will_remove_from_hass(self) -> None:
        """Clean up subscriptions."""
        for unsub in self._unsub_socket:
            unsub()
        self._unsub_socket.clear()

    # ── Socket.IO handlers ───────────────────────────────────────────────────

    @callback
    def _handle_position_event(self, data: dict) -> None:
        """Receive a 'position' event from Socket.IO and update the state.

        Filtering logic (in order):
        1. Missing lat/lon → ignored
        2. Insufficient GPS accuracy → ignored entirely
        3. moving=False → attributes updated in memory, HA state NOT written
           (no recorder entry → no stray lines on the map)
        4. Distance < min_threshold → micro-drift ignored, HA state NOT written
        5. Otherwise → async_write_ha_state() → recorder entry
        """
        _LOGGER.debug("Socket.IO position for %s: %s", self._tracker_name, data)

        lat = data.get("latitude")
        lon = data.get("longitude")
        if lat is None or lon is None:
            _LOGGER.warning(
                "Position event without lat/lon for %s: %s", self._tracker_name, data
            )
            return

        # ── 1. GPS accuracy filter ───────────────────────────────────────────
        accuracy = int(data.get("radius", 0) or 0)
        min_accuracy = self._entry.options.get(
            CONF_GPS_MIN_ACCURACY, DEFAULT_GPS_MIN_ACCURACY
        )
        if min_accuracy > 0 and accuracy > min_accuracy:
            _LOGGER.debug(
                "Position ignored for %s: insufficient accuracy (radius=%dm > threshold=%dm)",
                self._tracker_name,
                accuracy,
                min_accuracy,
            )
            return

        lat = float(lat)
        lon = float(lon)
        is_moving = bool(data.get("moving", False))

        # ── 2. moving=False filter ───────────────────────────────────────────
        # The in-memory attributes are updated (speed, heading, fix_time…)
        # but async_write_ha_state() is NOT called → no recorder entry
        # → no point on the map → no stray line between sessions.
        if not is_moving:
            self._gps_accuracy = accuracy
            self._fix_time = data.get("fixtime") or data.get("fixTime")
            self._speed = data.get("speed")
            self._heading = data.get("heading")
            self._altitude = data.get("altitude")
            self._is_moving = False
            _LOGGER.debug(
                "Position not recorded for %s: tracker stopped (moving=False)",
                self._tracker_name,
            )
            return

        # ── 3. Minimum distance filter (anti micro-drift) ────────────────────
        min_distance = self._entry.options.get(
            CONF_GPS_MIN_DISTANCE, DEFAULT_GPS_MIN_DISTANCE
        )
        if (
            min_distance > 0
            and self._latitude is not None
            and self._longitude is not None
        ):
            distance_m = _haversine_distance(self._latitude, self._longitude, lat, lon)
            if distance_m < min_distance:
                _LOGGER.debug(
                    "Position ignored for %s: movement too small (%.1fm < threshold=%dm)",
                    self._tracker_name,
                    distance_m,
                    min_distance,
                )
                return

        # ── 4. Full update ───────────────────────────────────────────────────
        self._latitude = lat
        self._longitude = lon
        self._gps_accuracy = accuracy
        self._fix_time = data.get("fixtime") or data.get("fixTime")
        self._speed = data.get("speed")
        self._heading = data.get("heading")
        self._altitude = data.get("altitude")
        self._is_moving = True

        self.async_write_ha_state()
        _LOGGER.debug(
            "Position updated for %s: lat=%.5f lon=%.5f moving=True",
            self._tracker_name,
            self._latitude,
            self._longitude,
        )

    # ── REST API fallback ────────────────────────────────────────────────────

    async def _async_fetch_initial_position(self) -> None:
        """Fetch the last known position via REST API at startup."""
        try:
            position = await self._api.get_last_position(self._tracker_id)
            if position:
                accuracy = int(position.get("radius", 0) or 0)
                min_accuracy = self._entry.options.get(
                    CONF_GPS_MIN_ACCURACY, DEFAULT_GPS_MIN_ACCURACY
                )
                if min_accuracy > 0 and accuracy > min_accuracy:
                    _LOGGER.debug(
                        "Initial position ignored for %s: insufficient accuracy (radius=%dm > threshold=%dm)",
                        self._tracker_name,
                        accuracy,
                        min_accuracy,
                    )
                    return
                self._latitude = position.get("latitude")
                self._longitude = position.get("longitude")
                self._gps_accuracy = accuracy
                self._fix_time = position.get("fixtime") or position.get("fixTime")
                self._speed = position.get("speed")
                self._heading = position.get("heading")
                self._altitude = position.get("altitude")
                self.async_write_ha_state()
                _LOGGER.info(
                    "Initial position (API) for %s: lat=%s lon=%s",
                    self._tracker_name,
                    self._latitude,
                    self._longitude,
                )
            else:
                _LOGGER.debug(
                    "No initial position available for %s", self._tracker_name
                )
        except Exception as err:
            _LOGGER.error(
                "Error fetching initial position for %s: %s",
                self._tracker_name,
                err,
            )
