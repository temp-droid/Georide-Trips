"""GeoRide Trips sensors - SIMPLE COMPLETE VERSION."""

import logging
from datetime import datetime, timedelta

from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfLength, UnitOfElectricPotential, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.util import dt as dt_util
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import (
    DOMAIN,
    METERS_TO_KM,
    KNOTS_TO_KMH,
    CONF_DRIVE_TYPE,
    DEFAULT_DRIVE_TYPE,
    DRIVETRAIN_PROFILES,
)

_LOGGER = logging.getLogger(__name__)

# Conversion constants
MILLISECONDS_TO_MINUTES = 60000
MILLISECONDS_TO_HOURS = 3600000


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up GeoRide Trips sensors from a config entry."""
    _LOGGER.info("Setting up GeoRide Trips sensors from config entry")

    data = hass.data[DOMAIN][entry.entry_id]
    trackers = data["trackers"]
    coordinators = data["coordinators"]
    lifetime_coordinators = data["lifetime_coordinators"]
    tracker_status_coordinators = data["tracker_status_coordinators"]

    profile = DRIVETRAIN_PROFILES.get(
        entry.options.get(CONF_DRIVE_TYPE, DEFAULT_DRIVE_TYPE),
        DRIVETRAIN_PROFILES["chain"],
    )

    sensors = []
    for tracker in trackers:
        tracker_id = str(tracker.get("trackerId"))
        coordinator = coordinators[tracker_id]
        lifetime_coordinator = lifetime_coordinators[tracker_id]
        status_coordinator = tracker_status_coordinators[tracker_id]

        # Schedule the lifetime coordinator's midnight refresh
        lifetime_coordinator.schedule_midnight_refresh()

        # As soon as a new trip is detected → immediate refresh of the lifetime coordinator
        def _on_new_trip(lc=lifetime_coordinator):
            hass.async_create_task(lc.async_request_refresh())

        unregister_new_trip = coordinator.on_new_trip(_on_new_trip)
        entry.async_on_unload(unregister_new_trip)

        odometer_sensor = GeoRideRealOdometerSensor(
            lifetime_coordinator, coordinator, status_coordinator, entry, tracker, hass
        )
        autonomy_sensor = GeoRideAutonomySensor(entry, tracker, hass, odometer_sensor)

        # Midnight snapshots manager — replaces the blueprint's 'midnight' trigger
        midnight_manager = GeoRideMidnightSnapshotManager(
            hass, entry, tracker, odometer_sensor
        )
        midnight_manager.setup()
        entry.async_on_unload(midnight_manager.unschedule)

        sensors.extend(
            [
                GeoRideLastTripSensor(coordinator, entry, tracker),
                GeoRideLastTripDetailsSensor(coordinator, entry, tracker),
                GeoRideTotalDistanceSensor(coordinator, entry, tracker),
                GeoRideTripCountSensor(coordinator, entry, tracker),
                GeoRideLifetimeOdometerSensor(lifetime_coordinator, entry, tracker),
                # RealOdometer listens to both coordinators: lifetime (solid base)
                # + recent coordinator (new intraday trips)
                odometer_sensor,
                # Remaining range sensor (reactive on odometer + fuel entities)
                autonomy_sensor,
                # Periodic km sensors — computed in Python, reactive on odometer + snapshot
                GeoRideKmJournaliersSensor(entry, tracker, hass, odometer_sensor),
                GeoRideKmHebdomadairesSensor(entry, tracker, hass, odometer_sensor),
                GeoRideKmMensuelsSensor(entry, tracker, hass, odometer_sensor),
                # Maintenance sensors — remaining km and remaining days computed in Python
                GeoRideKmRestantsVidangeSensor(entry, tracker, hass, odometer_sensor),
                GeoRideKmRestantsRevisionSensor(entry, tracker, hass, odometer_sensor),
                GeoRideJoursRestantsRevisionSensor(entry, tracker, hass),
                # Sensors fed by the status coordinator (/user/trackers data)
                GeoRideTrackerStatusSensor(status_coordinator, entry, tracker),
                GeoRideExternalBatterySensor(status_coordinator, entry, tracker),
                GeoRideInternalBatterySensor(status_coordinator, entry, tracker),
                # Last alarm sensor (fed by Socket.IO)
                GeoRideLastAlarmSensor(entry, tracker),
            ]
        )

        # Drivetrain maintenance sensors — always created; label adapts to the
        # selected drive_type. Time dimension only matters when day_interval>0.
        sensors.append(
            GeoRideKmRestantsDrivetrainSensor(
                entry, tracker, hass, odometer_sensor, profile["label"]
            )
        )
        sensors.append(
            GeoRideJoursRestantsDrivetrainSensor(entry, tracker, hass, profile["label"])
        )

    async_add_entities(sensors)
    _LOGGER.info("Added %d sensors for %d trackers", len(sensors), len(trackers))


# ════════════════════════════════════════════════════════════════════════════
# COORDINATORS
# ════════════════════════════════════════════════════════════════════════════


class GeoRideTripsCoordinator(DataUpdateCoordinator):
    """Coordinator to manage fetching GeoRide trips data (30 days).

    Automatically detects new trips in two ways:
    1. StatusCoordinator (polling 5 min): as soon as isLocked turns True
       (unlocked → locked transition), a refresh is triggered.
       The lock is a reliable end-of-trip signal, insensitive to
       micro-stops (red lights, etc.).
    2. Polling (safety net): on each fetch, if the last trip
       has changed, the on_new_trip() callbacks are called.
    """

    def __init__(
        self,
        hass,
        api,
        tracker_id,
        tracker_name,
        scan_interval=3600,
        trips_days_back=30,
    ):
        self.api = api
        self.tracker_id = tracker_id
        self.tracker_name = tracker_name
        self.trips_days_back = trips_days_back
        self._last_trip_id: str | None = None
        self._new_trip_callbacks: list = []
        self._stop_confirmed_callbacks: list = []
        self._status_unsub: callable | None = None
        self._status_coordinator = None
        self._last_locked_state: bool | None = None

        # No automatic polling — refresh only on tracker lock
        # (via StatusCoordinator) or manually. The scan_interval is ignored.
        super().__init__(
            hass,
            _LOGGER,
            name=f"GeoRide Trips {tracker_name}",
            update_interval=None,
        )

    def on_new_trip(self, callback) -> callable:
        """Register a callback called when a new trip is detected.

        Returns:
            Unregister function.
        """
        self._new_trip_callbacks.append(callback)

        def unregister():
            try:
                self._new_trip_callbacks.remove(callback)
            except ValueError:
                pass

        return unregister

    def on_stop_confirmed(self, callback) -> callable:
        """Register a one-shot callback called when the tracker locks.

        The callback is automatically removed after the first call.

        Returns:
            Unregister function (for early cancellation).
        """
        self._stop_confirmed_callbacks.append(callback)

        def unregister():
            try:
                self._stop_confirmed_callbacks.remove(callback)
            except ValueError:
                pass

        return unregister

    def attach_status_coordinator(self, status_coordinator) -> None:
        """Subscribe to the StatusCoordinator to detect tracker locking.

        Triggers a refresh as soon as isLocked goes from False to True
        (unlocked → locked transition = confirmed end of trip).
        Polling every 5 min — reliable and insensitive to micro-stops.

        To be called after the StatusCoordinator's first refresh.
        """
        if status_coordinator is None:
            return
        self._status_coordinator = status_coordinator
        # Initialize the known locked state to avoid a false trigger at startup
        data = status_coordinator.data
        if data:
            self._last_locked_state = bool(data.get("isLocked", False))
        self._status_unsub = status_coordinator.async_add_listener(
            self._handle_status_update
        )
        _LOGGER.debug(
            "TripsCoordinator %s: subscribed to the StatusCoordinator (lock detection active, initial locked state=%s)",
            self.tracker_name,
            self._last_locked_state,
        )

    def detach_status_coordinator(self) -> None:
        """Unsubscribe from the StatusCoordinator (called on unload)."""
        if self._status_unsub:
            self._status_unsub()
            self._status_unsub = None
        self._status_coordinator = None

    @property
    def is_locked(self) -> bool | None:
        """Lock state via the attached StatusCoordinator.

        None if no StatusCoordinator is attached or there is no data yet.
        Public accessor — do not read _status_coordinator elsewhere.
        """
        if self._status_coordinator is None:
            return None
        return self._status_coordinator.is_locked

    @callback
    def _handle_status_update(self) -> None:
        """Called on each StatusCoordinator polling (~5 min).

        Detects the unlocked → locked transition (isLocked False → True)
        as a reliable end-of-trip signal.
        """
        if self._status_coordinator is None:
            return
        data = self._status_coordinator.data
        if not data:
            return

        is_locked = bool(data.get("isLocked", False))

        # False → True transition only (avoids triggering at startup
        # or on a stable True value)
        if is_locked and self._last_locked_state is False:
            _LOGGER.info(
                "%s: lock detected (isLocked False→True), refresh trips",
                self.tracker_name,
            )
            self._on_lock_confirmed()

        self._last_locked_state = is_locked

    def _on_lock_confirmed(self) -> None:
        """Called when a lock is detected — refresh + notify subscribers."""
        self.hass.async_create_task(self.async_request_refresh())

        # Notify the one-shot callbacks (e.g. confirm refuel button)
        callbacks = list(self._stop_confirmed_callbacks)
        self._stop_confirmed_callbacks.clear()
        for cb in callbacks:
            try:
                cb()
            except Exception as err:
                _LOGGER.error(
                    "%s: error in on_stop_confirmed callback: %s",
                    self.tracker_name,
                    err,
                )

    async def _async_update_data(self):
        try:
            from datetime import timezone as tz

            from_date = datetime.now(tz.utc) - timedelta(days=self.trips_days_back)
            to_date = datetime.now(tz.utc)

            trips = await self.api.get_trips(self.tracker_id, from_date, to_date)

            if trips:
                trips.sort(key=lambda x: x.get("startTime", ""), reverse=True)

            _LOGGER.debug(
                "Fetched %d trips for tracker %s", len(trips), self.tracker_id
            )

            # Detect a new trip (safety net if Socket.IO is down)
            if trips:
                latest = trips[0]
                latest_id = latest.get("id") or latest.get("startTime", "")
                if self._last_trip_id is not None and latest_id != self._last_trip_id:
                    _LOGGER.info(
                        "New trip detected for %s (was %s, now %s) — triggering lifetime refresh",
                        self.tracker_name,
                        self._last_trip_id,
                        latest_id,
                    )
                    for cb in list(self._new_trip_callbacks):
                        try:
                            cb()
                        except Exception as err:
                            _LOGGER.error("Error in new_trip callback: %s", err)
                self._last_trip_id = latest_id

            return trips

        except Exception as err:
            raise UpdateFailed(f"Error fetching trips: {err}")


class GeoRideLifetimeTripsCoordinator(DataUpdateCoordinator):
    """Coordinator to manage fetching ALL trips since tracker creation.

    Forced refresh at midnight to have an up-to-date lifetime base at the start of the day.
    New intraday trips are caught by the recent coordinator
    and merged in GeoRideRealOdometerSensor.
    """

    def __init__(
        self,
        hass,
        api,
        tracker_id,
        tracker_name,
        activation_date,
        lifetime_scan_interval=86400,
    ):
        self.api = api
        self.tracker_id = tracker_id
        self.tracker_name = tracker_name
        self.activation_date = activation_date
        self._midnight_unsub = None

        super().__init__(
            hass,
            _LOGGER,
            name=f"GeoRide Lifetime {tracker_name}",
            update_interval=timedelta(seconds=lifetime_scan_interval),
        )

    def schedule_midnight_refresh(self) -> None:
        """Schedule the automatic refresh at midnight (called after async_config_entry_first_refresh)."""
        if self._midnight_unsub:
            self._midnight_unsub()
        self._midnight_unsub = async_track_time_change(
            self.hass,
            self._midnight_callback,
            hour=0,
            minute=0,
            second=0,
        )
        _LOGGER.debug(
            "Midnight refresh scheduled for lifetime coordinator %s", self.tracker_name
        )

    def unschedule_midnight_refresh(self) -> None:
        """Cancel the midnight refresh."""
        if self._midnight_unsub:
            self._midnight_unsub()
            self._midnight_unsub = None

    @callback
    def _midnight_callback(self, now) -> None:
        """Trigger a refresh of the lifetime coordinator at midnight."""
        _LOGGER.info(
            "Midnight refresh triggered for lifetime coordinator %s", self.tracker_name
        )
        self.hass.async_create_task(self.async_request_refresh())

    async def _async_update_data(self):
        try:
            from datetime import timezone as tz

            if self.activation_date:
                try:
                    from_date = datetime.fromisoformat(
                        self.activation_date.replace("Z", "+00:00")
                    )
                except Exception:
                    from_date = datetime.now(tz.utc) - timedelta(days=1825)
            else:
                from_date = datetime.now(tz.utc) - timedelta(days=1825)

            to_date = datetime.now(tz.utc)

            _LOGGER.info(
                "Fetching lifetime trips for %s from %s to %s",
                self.tracker_name,
                from_date.date(),
                to_date.date(),
            )

            trips = await self.api.get_trips(self.tracker_id, from_date, to_date)

            if trips:
                trips.sort(key=lambda x: x.get("startTime", ""))

            _LOGGER.info(
                "Fetched %d lifetime trips for tracker %s", len(trips), self.tracker_id
            )

            return {
                "trips": trips,
                "from_date": from_date,
                "to_date": to_date,
            }

        except Exception as err:
            raise UpdateFailed(f"Error fetching lifetime trips: {err}")


class GeoRideTrackerStatusCoordinator(DataUpdateCoordinator):
    """Coordinator polling /user/trackers every 5 min.

    Provides: battery voltages, eco mode, moving, stolen, crashed, status (online/offline),
    isLocked, latitude/longitude — used as fallback when Socket.IO is unavailable.
    """

    def __init__(
        self, hass, api, tracker_id: str, tracker_name: str, scan_interval: int = 300
    ):
        self.api = api
        self.tracker_id = tracker_id
        self.tracker_name = tracker_name

        super().__init__(
            hass,
            _LOGGER,
            name=f"GeoRide Status {tracker_name}",
            update_interval=timedelta(seconds=scan_interval),
        )

    @property
    def is_locked(self) -> bool | None:
        """Current lock state (isLocked), None if no data."""
        if not self.data:
            return None
        return bool(self.data.get("isLocked", False))

    async def _async_update_data(self) -> dict:
        """Return the raw tracker dict for this tracker_id."""
        try:
            trackers = await self.api.get_trackers()
            for tracker in trackers:
                if str(tracker.get("trackerId")) == self.tracker_id:
                    _LOGGER.debug(
                        "Status update for tracker %s: moving=%s eco=%s status=%s",
                        self.tracker_id,
                        tracker.get("moving"),
                        tracker.get("isInEco"),
                        tracker.get("status"),
                    )
                    return tracker
            _LOGGER.warning(
                "Tracker %s not found in /user/trackers response", self.tracker_id
            )
            return {}
        except Exception as err:
            raise UpdateFailed(f"Error fetching tracker status: {err}")


# ════════════════════════════════════════════════════════════════════════════
# MANAGER — MIDNIGHT SNAPSHOTS (odometer_at_day_start / semaine / mois)
# ════════════════════════════════════════════════════════════════════════════


class GeoRideMidnightSnapshotManager:
    """Manager for the midnight odometer snapshots.

    Replaces the blueprint's 'midnight' trigger: at 00:00:00 every night,
    updates the number.odometer_at_day_start/semaine/mois directly in Python.

    The monthly reset is fixed on the 1st of the month. The monthly summary is sent
    by the blueprint on the last day of the month (before the reset).

    Usage:
        manager = GeoRideMidnightSnapshotManager(hass, entry, tracker, odometer_sensor)
        manager.setup()          # to call in async_setup_entry
        manager.unschedule()     # to call on entry unload
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        tracker: dict,
        odometer_sensor: "GeoRideRealOdometerSensor",
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._tracker = tracker
        self._odometer_sensor = odometer_sensor

        self.tracker_id = str(tracker.get("trackerId"))
        self.tracker_name = tracker.get("trackerName", f"Tracker {self.tracker_id}")

        # Entity_ids resolved on the first midnight callback (not in __init__
        # because the registry is not yet populated at this stage of setup)
        self._entity_debut_journee: str | None = None
        self._entity_debut_semaine: str | None = None
        self._entity_debut_mois: str | None = None
        self._entities_resolved = False

        self._unsub: callable | None = None

    def setup(self) -> None:
        """Schedule the midnight callback."""
        self._unsub = async_track_time_change(
            self._hass,
            self._midnight_callback,
            hour=0,
            minute=0,
            second=0,
        )
        _LOGGER.debug(
            "MidnightSnapshotManager %s: scheduled (midnight snapshots active)",
            self.tracker_name,
        )

    def unschedule(self) -> None:
        """Unschedule the midnight callback."""
        if self._unsub:
            self._unsub()
            self._unsub = None

    def _get_float(self, entity_id: str | None, default: float = 0.0) -> float:
        if entity_id is None:
            return default
        state = self._hass.states.get(entity_id)
        if state and state.state not in (None, "unknown", "unavailable"):
            try:
                return float(state.state)
            except (ValueError, TypeError):
                pass
        return default

    def _set_number(self, entity_id: str | None, value: float) -> None:
        """Update a number via hass.services.async_call."""
        if entity_id is None:
            _LOGGER.warning(
                "MidnightSnapshotManager %s: entity_id None, cannot set value %.2f",
                self.tracker_name,
                value,
            )
            return
        self._hass.async_create_task(
            self._hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": entity_id, "value": round(value, 2)},
                blocking=False,
            )
        )

    @callback
    def _midnight_callback(self, now) -> None:
        """Called at midnight: update the odometer snapshots."""
        # Lazy resolution of the entity_ids (the registry is populated after setup)
        if not self._entities_resolved:
            from .helpers import resolve_entity_id

            self._entity_debut_journee = resolve_entity_id(
                self._hass, "number", self.tracker_id, "odometer_at_day_start"
            )
            self._entity_debut_semaine = resolve_entity_id(
                self._hass, "number", self.tracker_id, "odometer_at_week_start"
            )
            self._entity_debut_mois = resolve_entity_id(
                self._hass, "number", self.tracker_id, "odometer_at_month_start"
            )
            self._entities_resolved = True

        odometer_km = self._odometer_sensor.native_value
        if odometer_km is None:
            _LOGGER.warning(
                "MidnightSnapshotManager %s: odometer unavailable at midnight, snapshots skipped",
                self.tracker_name,
            )
            return

        # Daily snapshot — every night
        self._set_number(self._entity_debut_journee, odometer_km)
        _LOGGER.info(
            "MidnightSnapshotManager %s: odometer_at_day_start = %.1f km",
            self.tracker_name,
            odometer_km,
        )

        # Weekly snapshot — only on Monday (weekday == 0)
        if now.weekday() == 0:
            self._set_number(self._entity_debut_semaine, odometer_km)
            _LOGGER.info(
                "MidnightSnapshotManager %s: odometer_at_week_start = %.1f km (Monday)",
                self.tracker_name,
                odometer_km,
            )

        # Monthly snapshot — on the 1st of the month at midnight
        if now.day == 1:
            self._set_number(self._entity_debut_mois, odometer_km)
            _LOGGER.info(
                "MidnightSnapshotManager %s: odometer_at_month_start = %.1f km (1st of the month)",
                self.tracker_name,
                odometer_km,
            )


# ════════════════════════════════════════════════════════════════════════════
# SENSORS — PERIODIC KM (daily, weekly, monthly)
# ════════════════════════════════════════════════════════════════════════════


class _GeoRideKmPeriodBase(SensorEntity, RestoreEntity):
    """Base class for the periodic km sensors.

    Computation: max(odometer - snapshot_debut, 0)

    Subscribes to:
      - sensor.<moto>_odometer  (via a direct reference to GeoRideRealOdometerSensor)
      - number.<moto>_km_debut_<periode>  (start-of-period snapshot)
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        entry,
        tracker,
        hass,
        odometer_sensor: "GeoRideRealOdometerSensor",
        unique_id_suffix: str,
        name_suffix: str,
        icon: str,
        snapshot_entity: str,
    ) -> None:
        self._entry = entry
        self._tracker = tracker
        self._hass = hass
        self._odometer_sensor = odometer_sensor
        self._snapshot_entity = snapshot_entity

        self.tracker_id = str(tracker.get("trackerId"))
        self.tracker_name = tracker.get("trackerName", f"Tracker {self.tracker_id}")

        self._attr_unique_id = f"{self.tracker_id}_{unique_id_suffix}"
        self._attr_name = name_suffix
        self._attr_icon = icon
        self._attr_native_unit_of_measurement = UnitOfLength.KILOMETERS
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_entity_category = None
        self._attr_native_value: float = 0.0

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.tracker_id)},
            name=self.tracker_name,
            manufacturer="GeoRide",
            model=self._tracker.get("model", "GeoRide Tracker"),
            sw_version=str(self._tracker.get("softwareVersion", "")),
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        # Restore the last known value — it will be corrected by the
        # first state_change_event once the numbers are restored.
        # DO NOT call _recalculate() here: the snapshots are not ready yet.
        if (last_state := await self.async_get_last_state()) is not None:
            if last_state.state not in (None, "unknown", "unavailable"):
                try:
                    self._attr_native_value = float(last_state.state)
                except (ValueError, TypeError):
                    pass

        from homeassistant.helpers.event import async_track_state_change_event

        # Subscribe to changes of the odometer AND the snapshot
        watched = [self._odometer_sensor.entity_id, self._snapshot_entity]
        self.async_on_remove(
            async_track_state_change_event(
                self._hass,
                watched,
                self._handle_state_change,
            )
        )
        # No _recalculate() here — we wait for the first state_change_event

    @callback
    def _handle_state_change(self, event) -> None:
        # Ignore the odometer's startup transitions (unknown/unavailable → value).
        # These transitions occur on every integration reload and can
        # trigger a premature recompute with a snapshot not yet stabilized.
        old_state = event.data.get("old_state")
        entity_changed = event.data.get("entity_id", "")
        if (
            entity_changed == self._odometer_sensor.entity_id
            and old_state is not None
            and old_state.state in (None, "unknown", "unavailable")
        ):
            _LOGGER.debug(
                "%s: odometer startup transition ignored (old=%s)",
                self._attr_name,
                old_state.state if old_state else "None",
            )
            return

        self._recalculate()
        self.async_write_ha_state()

    def _get_float(self, entity_id: str | None, default: float = 0.0) -> float:
        if entity_id is None:
            return default
        state = self._hass.states.get(entity_id)
        if state and state.state not in (None, "unknown", "unavailable"):
            try:
                return float(state.state)
            except (ValueError, TypeError):
                pass
        return default

    def _is_snapshot_ready(self) -> bool:
        """Return True if the snapshot entity is available and non-zero."""
        state = self._hass.states.get(self._snapshot_entity)
        if state is None or state.state in (None, "unknown", "unavailable"):
            return False
        try:
            val = float(state.state)
            return val > 0.0
        except (ValueError, TypeError):
            return False

    def _recalculate(self) -> None:
        odometer_km = self._odometer_sensor.native_value or 0.0

        # If the snapshot is 0.0 (transient value at startup before full restoration)
        # and the odometer is significant, keep the restored value without overwriting.
        if not self._is_snapshot_ready():
            _LOGGER.debug(
                "%s: snapshot not ready (unavailable or 0), recompute skipped",
                self._attr_name,
            )
            return

        snapshot_km = self._get_float(self._snapshot_entity, 0.0)
        km = max(odometer_km - snapshot_km, 0.0)
        self._attr_native_value = round(km, 1)

        _LOGGER.debug(
            "%s: odometer=%.1f km - snapshot=%.1f km = %.1f km",
            self._attr_name,
            odometer_km,
            snapshot_km,
            km,
        )

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "current_odometer": self._odometer_sensor.native_value,
            "snapshot_start": self._get_float(self._snapshot_entity),
            "snapshot_entity": self._snapshot_entity,
        }


class GeoRideKmJournaliersSensor(_GeoRideKmPeriodBase):
    """Sensor for km traveled today (odometer - midnight snapshot)."""

    def __init__(self, entry, tracker, hass, odometer_sensor) -> None:
        slug = (
            tracker.get("trackerName", f"Tracker {tracker.get('trackerId')}")
            .lower()
            .replace(" ", "_")
        )
        super().__init__(
            entry=entry,
            tracker=tracker,
            hass=hass,
            odometer_sensor=odometer_sensor,
            unique_id_suffix="daily_mileage",
            name_suffix="Daily mileage",
            icon="mdi:counter",
            snapshot_entity=f"number.{slug}_odometer_at_day_start",
        )


class GeoRideKmHebdomadairesSensor(_GeoRideKmPeriodBase):
    """Sensor for km traveled this week (odometer - Monday midnight snapshot)."""

    def __init__(self, entry, tracker, hass, odometer_sensor) -> None:
        slug = (
            tracker.get("trackerName", f"Tracker {tracker.get('trackerId')}")
            .lower()
            .replace(" ", "_")
        )
        super().__init__(
            entry=entry,
            tracker=tracker,
            hass=hass,
            odometer_sensor=odometer_sensor,
            unique_id_suffix="weekly_mileage",
            name_suffix="Weekly mileage",
            icon="mdi:calendar-week",
            snapshot_entity=f"number.{slug}_odometer_at_week_start",
        )


class GeoRideKmMensuelsSensor(_GeoRideKmPeriodBase):
    """Sensor for km traveled this month (odometer - 1st-of-month snapshot)."""

    def __init__(self, entry, tracker, hass, odometer_sensor) -> None:
        slug = (
            tracker.get("trackerName", f"Tracker {tracker.get('trackerId')}")
            .lower()
            .replace(" ", "_")
        )
        super().__init__(
            entry=entry,
            tracker=tracker,
            hass=hass,
            odometer_sensor=odometer_sensor,
            unique_id_suffix="monthly_mileage",
            name_suffix="Monthly mileage",
            icon="mdi:calendar-month",
            snapshot_entity=f"number.{slug}_odometer_at_month_start",
        )


# ════════════════════════════════════════════════════════════════════════════
# SENSORS — TRIPS
# ════════════════════════════════════════════════════════════════════════════


class GeoRideLastTripSensor(CoordinatorEntity, SensorEntity):
    """Sensor for last trip (simple)."""

    _attr_has_entity_name = True

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
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.tracker_id)},
            name=self.tracker_name,
            manufacturer="GeoRide",
            model=self._tracker.get("model", "GeoRide Tracker"),
            sw_version=str(self._tracker.get("softwareVersion", "")),
        )

    @property
    def native_value(self):
        trips = self.coordinator.data
        if not trips:
            return None
        return trips[0].get("startTime")


class GeoRideLastTripDetailsSensor(CoordinatorEntity, SensorEntity):
    """Sensor for last trip with detailed info."""

    _attr_has_entity_name = True

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
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.tracker_id)},
            name=self.tracker_name,
            manufacturer="GeoRide",
            model=self._tracker.get("model", "GeoRide Tracker"),
            sw_version=str(self._tracker.get("softwareVersion", "")),
        )

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


class GeoRideTotalDistanceSensor(CoordinatorEntity, SensorEntity):
    """Sensor for total distance over period."""

    _attr_has_entity_name = True

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
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.tracker_id)},
            name=self.tracker_name,
            manufacturer="GeoRide",
            model=self._tracker.get("model", "GeoRide Tracker"),
            sw_version=str(self._tracker.get("softwareVersion", "")),
        )

    @property
    def native_value(self):
        trips = self.coordinator.data
        if not trips:
            return 0
        total_m = sum(trip.get("distance", 0) for trip in trips)
        return round(total_m / METERS_TO_KM, 2)


class GeoRideTripCountSensor(CoordinatorEntity, SensorEntity):
    """Sensor for trip count over period."""

    _attr_has_entity_name = True

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
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.tracker_id)},
            name=self.tracker_name,
            manufacturer="GeoRide",
            model=self._tracker.get("model", "GeoRide Tracker"),
            sw_version=str(self._tracker.get("softwareVersion", "")),
        )

    @property
    def native_value(self):
        trips = self.coordinator.data
        return len(trips) if trips else 0


# ════════════════════════════════════════════════════════════════════════════
# SENSORS — LIFETIME ODOMETER
# ════════════════════════════════════════════════════════════════════════════


class GeoRideLifetimeOdometerSensor(CoordinatorEntity, SensorEntity):
    """Sensor for lifetime odometer."""

    _attr_has_entity_name = True

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
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.tracker_id)},
            name=self.tracker_name,
            manufacturer="GeoRide",
            model=self._tracker.get("model", "GeoRide Tracker"),
            sw_version=str(self._tracker.get("softwareVersion", "")),
        )

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


class GeoRideRealOdometerSensor(CoordinatorEntity, SensorEntity):
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

    _attr_has_entity_name = True

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
        from .helpers import resolve_entity_id

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
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.tracker_id)},
            name=self.tracker_name,
            manufacturer="GeoRide",
            model=self._tracker.get("model", "GeoRide Tracker"),
            sw_version=str(self._tracker.get("softwareVersion", "")),
        )

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


# ════════════════════════════════════════════════════════════════════════════
# SENSOR — REMAINING RANGE (reactive)
# ════════════════════════════════════════════════════════════════════════════


class GeoRideAutonomySensor(SensorEntity, RestoreEntity):
    """Remaining range sensor, updated on every odometer change.

    Computation:
      - fuel_total_range is the single reference.
      - The computed average (fuel_calculated_average_range) is offered as an option
        via the button.<moto>_appliquer_autonomie_calculee button and the
        blueprint notification — it is not applied automatically.

      km_restants = autonomie_ref - (odometer_actuel - fuel_km_at_last_refuel)
      (floored at 0)

    Subscribes to state changes of:
      - sensor.<moto>_odometer        (via a direct reference to GeoRideRealOdometerSensor)
      - number.<moto>_km_dernier_plein
      - number.<moto>_autonomie_totale
      - number.<moto>_autonomie_moyenne_calculee
      - number.<moto>_nb_pleins_enregistres
    """

    _attr_has_entity_name = True

    def __init__(
        self, entry, tracker, hass, odometer_sensor: "GeoRideRealOdometerSensor"
    ):
        self._entry = entry
        self._tracker = tracker
        self._hass = hass
        self._odometer_sensor = odometer_sensor

        self.tracker_id = str(tracker.get("trackerId"))
        self.tracker_name = tracker.get("trackerName", f"Tracker {self.tracker_id}")

        # The entity_ids will be resolved in async_added_to_hass via the registry
        self._entity_km_dernier_plein: str | None = None
        self._entity_autonomie_totale: str | None = None
        self._entity_autonomie_moyenne: str | None = None
        self._entity_nb_pleins: str | None = None

        self._attr_unique_id = f"{self.tracker_id}_remaining_range"
        self._attr_name = "Remaining range"
        self._attr_icon = "mdi:gas-station-outline"
        self._attr_native_unit_of_measurement = UnitOfLength.KILOMETERS
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_value: float = 0.0

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.tracker_id)},
            name=self.tracker_name,
            manufacturer="GeoRide",
            model=self._tracker.get("model", "GeoRide Tracker"),
            sw_version=str(self._tracker.get("softwareVersion", "")),
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        # Restoration
        if (last_state := await self.async_get_last_state()) is not None:
            if last_state.state not in (None, "unknown", "unavailable"):
                try:
                    self._attr_native_value = float(last_state.state)
                except (ValueError, TypeError):
                    pass

        # Resolve the entity_ids via the registry (reliable, independent of the slug)
        from .helpers import resolve_entity_id

        self._entity_km_dernier_plein = resolve_entity_id(
            self._hass, "number", self.tracker_id, "fuel_km_at_last_refuel"
        )
        self._entity_autonomie_totale = resolve_entity_id(
            self._hass, "number", self.tracker_id, "fuel_total_range"
        )
        self._entity_autonomie_moyenne = resolve_entity_id(
            self._hass, "number", self.tracker_id, "fuel_calculated_average_range"
        )
        self._entity_nb_pleins = resolve_entity_id(
            self._hass, "number", self.tracker_id, "fuel_recorded_refuel_count"
        )

        if not self._entity_km_dernier_plein or not self._entity_autonomie_totale:
            _LOGGER.warning(
                "Autonomie %s: entity_ids not resolved (fuel_km_at_last_refuel=%s, fuel_total_range=%s). "
                "Are the number entities created?",
                self.tracker_name,
                self._entity_km_dernier_plein,
                self._entity_autonomie_totale,
            )

        from homeassistant.helpers.event import async_track_state_change_event

        watched = [
            eid
            for eid in [
                self._odometer_sensor.entity_id,
                self._entity_km_dernier_plein,
                self._entity_autonomie_totale,
                self._entity_autonomie_moyenne,
                self._entity_nb_pleins,
            ]
            if eid is not None
        ]

        self.async_on_remove(
            async_track_state_change_event(
                self._hass,
                watched,
                self._handle_state_change,
            )
        )

        # No _recalculate() here — we wait for the first state_change_event
        # to avoid spurious values before the numbers are restored

    @callback
    def _handle_state_change(self, event) -> None:
        self._recalculate()
        self.async_write_ha_state()

    def _get_float(self, entity_id: str | None, default: float = 0.0) -> float:
        if entity_id is None:
            return default
        state = self._hass.states.get(entity_id)
        if state and state.state not in (None, "unknown", "unavailable"):
            try:
                return float(state.state)
            except (ValueError, TypeError):
                pass
        return default

    def _recalculate(self) -> None:
        odometer_km = self._odometer_sensor.native_value or 0.0
        fuel_km_at_last_refuel = self._get_float(self._entity_km_dernier_plein)
        fuel_total_range = self._get_float(self._entity_autonomie_totale, 150.0)

        # Not configured until the first refuel is recorded (odometer-at-last-refuel
        # stays 0). Report "unknown" rather than 0 km, which would trip a false
        # "refuel needed" alert on a fresh install.
        if fuel_km_at_last_refuel == 0:
            self._attr_native_value = None
            return

        km_parcourus = max(odometer_km - fuel_km_at_last_refuel, 0.0)
        km_restants = max(fuel_total_range - km_parcourus, 0.0)

        self._attr_native_value = round(km_restants, 1)

        _LOGGER.debug(
            "Autonomie %s: ref=%.1f km (manual), traveled=%.1f km (since %.1f), remaining=%.1f km",
            self.tracker_name,
            fuel_total_range,
            km_parcourus,
            fuel_km_at_last_refuel,
            km_restants,
        )

    @property
    def extra_state_attributes(self) -> dict:
        nb_pleins = self._get_float(self._entity_nb_pleins)
        autonomie_moyenne = self._get_float(self._entity_autonomie_moyenne)
        fuel_total_range = self._get_float(self._entity_autonomie_totale, 150.0)
        return {
            "range_reference": "manual",
            "fuel_total_range_km": fuel_total_range,
            "fuel_calculated_average_range_km": autonomie_moyenne
            if autonomie_moyenne > 0
            else None,
            "fuel_recorded_refuel_count": int(nb_pleins),
            "fuel_km_at_last_refuel": self._get_float(self._entity_km_dernier_plein),
        }


# ════════════════════════════════════════════════════════════════════════════
# SENSORS — MAINTENANCE (remaining km + remaining days computed in Python)
# ════════════════════════════════════════════════════════════════════════════


class _GeoRideEntretienKmBase(SensorEntity, RestoreEntity):
    """Base class for the remaining-km maintenance sensors.

    Common computation:
      km_restants = km_dernier_entretien + intervalle_km - odometer_actuel
      (can be negative: maintenance overdue)

    Subscribes to:
      - sensor.<moto>_odometer  (via a direct reference to GeoRideRealOdometerSensor)
      - number.<moto>_<intervalle_key>  (resolved via the entity registry)
      - number.<moto>_<km_dernier_key>  (resolved via the entity registry)
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        entry,
        tracker,
        hass,
        odometer_sensor: "GeoRideRealOdometerSensor",
        unique_id_suffix: str,
        name_suffix: str,
        icon: str,
        intervalle_key: str,
        km_dernier_key: str,
    ) -> None:
        self._entry = entry
        self._tracker = tracker
        self._hass = hass
        self._odometer_sensor = odometer_sensor
        self._intervalle_key = intervalle_key
        self._km_dernier_key = km_dernier_key

        # Entity_ids resolved in async_added_to_hass
        self._entity_intervalle: str | None = None
        self._entity_km_dernier: str | None = None

        self.tracker_id = str(tracker.get("trackerId"))
        self.tracker_name = tracker.get("trackerName", f"Tracker {self.tracker_id}")

        self._attr_unique_id = f"{self.tracker_id}_{unique_id_suffix}"
        self._attr_name = name_suffix
        self._attr_icon = icon
        self._attr_native_unit_of_measurement = UnitOfLength.KILOMETERS
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_entity_category = None
        self._attr_native_value: float = 0.0

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.tracker_id)},
            name=self.tracker_name,
            manufacturer="GeoRide",
            model=self._tracker.get("model", "GeoRide Tracker"),
            sw_version=str(self._tracker.get("softwareVersion", "")),
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        if (last_state := await self.async_get_last_state()) is not None:
            if last_state.state not in (None, "unknown", "unavailable"):
                try:
                    self._attr_native_value = float(last_state.state)
                except (ValueError, TypeError):
                    pass

        # Resolve the entity_ids via the registry
        from .helpers import resolve_entity_id

        self._entity_intervalle = resolve_entity_id(
            self._hass,
            "number",
            self.tracker_id,
            self._intervalle_key,
        )
        self._entity_km_dernier = resolve_entity_id(
            self._hass,
            "number",
            self.tracker_id,
            self._km_dernier_key,
        )

        from homeassistant.helpers.event import async_track_state_change_event

        watched = [
            eid
            for eid in [
                self._odometer_sensor.entity_id,
                self._entity_intervalle,
                self._entity_km_dernier,
            ]
            if eid is not None
        ]
        self.async_on_remove(
            async_track_state_change_event(
                self._hass,
                watched,
                self._handle_state_change,
            )
        )
        # No _recalculate() here — we wait for the first state_change_event
        # to avoid spurious values before the numbers are restored

    @callback
    def _handle_state_change(self, event) -> None:
        self._recalculate()
        self.async_write_ha_state()

    def _get_float(self, entity_id: str | None, default: float = 0.0) -> float:
        if entity_id is None:
            return default
        state = self._hass.states.get(entity_id)
        if state and state.state not in (None, "unknown", "unavailable"):
            try:
                return float(state.state)
            except (ValueError, TypeError):
                pass
        return default

    def _recalculate(self) -> None:
        odometer_km = self._odometer_sensor.native_value or 0.0
        intervalle_km = self._get_float(self._entity_intervalle, 0.0)
        km_dernier = self._get_float(self._entity_km_dernier, 0.0)

        # Not configured until a maintenance is recorded (km_dernier stays 0), or
        # the interval is 0 (maintenance disabled). Report "unknown" rather than a
        # bogus negative "overdue" value that would trip a false "due" alert.
        if km_dernier == 0 or intervalle_km == 0:
            self._attr_native_value = None
            return

        km_restants = km_dernier + intervalle_km - odometer_km
        self._attr_native_value = round(km_restants, 1)

        _LOGGER.debug(
            "%s: dernier=%.1f km + intervalle=%.1f km - odometer=%.1f km = restants=%.1f km",
            self._attr_name,
            km_dernier,
            intervalle_km,
            odometer_km,
            km_restants,
        )

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "km_at_last_service": self._get_float(self._entity_km_dernier),
            "km_interval": self._get_float(self._entity_intervalle),
            "current_odometer": self._odometer_sensor.native_value,
        }


class GeoRideKmRestantsDrivetrainSensor(_GeoRideEntretienKmBase):
    """Sensor for km remaining before drivetrain maintenance (chain/shaft/belt)."""

    def __init__(
        self, entry, tracker, hass, odometer_sensor, label="Drivetrain"
    ) -> None:
        super().__init__(
            entry=entry,
            tracker=tracker,
            hass=hass,
            odometer_sensor=odometer_sensor,
            unique_id_suffix="drivetrain_remaining_km",
            name_suffix=f"{label} – remaining km",
            icon="mdi:link-variant",
            intervalle_key="drivetrain_km_interval",
            km_dernier_key="drivetrain_km_at_last_service",
        )


class GeoRideKmRestantsVidangeSensor(_GeoRideEntretienKmBase):
    """Sensor for km remaining before oil change."""

    def __init__(self, entry, tracker, hass, odometer_sensor) -> None:
        super().__init__(
            entry=entry,
            tracker=tracker,
            hass=hass,
            odometer_sensor=odometer_sensor,
            unique_id_suffix="oil_change_remaining_km",
            name_suffix="Oil change – remaining km",
            icon="mdi:oil",
            intervalle_key="oil_change_km_interval",
            km_dernier_key="oil_change_km_at_last_oil_change",
        )


class GeoRideKmRestantsRevisionSensor(_GeoRideEntretienKmBase):
    """Sensor for km remaining before service."""

    def __init__(self, entry, tracker, hass, odometer_sensor) -> None:
        super().__init__(
            entry=entry,
            tracker=tracker,
            hass=hass,
            odometer_sensor=odometer_sensor,
            unique_id_suffix="service_remaining_km",
            name_suffix="Service – remaining km",
            icon="mdi:wrench",
            intervalle_key="service_km_interval",
            km_dernier_key="service_km_at_last_service",
        )


class GeoRideJoursRestantsRevisionSensor(SensorEntity, RestoreEntity):
    """Sensor for days remaining before service (based on last maintenance date + interval in days).

    Computation:
      jours_restants = (date_dernier_entretien + intervalle_jours) - today
      (can be negative: service overdue)

    Subscribes to:
      - datetime.<moto>_entretien_revision_date_derniere_revision
      - number.<moto>_entretien_revision_intervalle_jours
    """

    _attr_has_entity_name = True

    # Keys/label of the maintenance slot this days-sensor belongs to.
    # Overridden by subclasses (e.g. drivetrain) to point at another slot.
    _unique_id_suffix = "service_remaining_days"
    _name_suffix = "Service – remaining days"
    _date_dernier_key = "service_last_service_date"
    _intervalle_j_key = "service_day_interval"

    def __init__(self, entry, tracker, hass) -> None:
        self._entry = entry
        self._tracker = tracker
        self._hass = hass

        self.tracker_id = str(tracker.get("trackerId"))
        self.tracker_name = tracker.get("trackerName", f"Tracker {self.tracker_id}")

        # Entity_ids resolved in async_added_to_hass
        self._entity_date_dernier: str | None = None
        self._entity_intervalle_j: str | None = None

        self._attr_unique_id = f"{self.tracker_id}_{self._unique_id_suffix}"
        self._attr_name = self._name_suffix
        self._attr_icon = "mdi:calendar-clock"
        self._attr_native_unit_of_measurement = "d"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_entity_category = None
        self._attr_native_value: float = 0.0

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.tracker_id)},
            name=self.tracker_name,
            manufacturer="GeoRide",
            model=self._tracker.get("model", "GeoRide Tracker"),
            sw_version=str(self._tracker.get("softwareVersion", "")),
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        if (last_state := await self.async_get_last_state()) is not None:
            if last_state.state not in (None, "unknown", "unavailable"):
                try:
                    self._attr_native_value = float(last_state.state)
                except (ValueError, TypeError):
                    pass

        # Resolve the entity_ids via the registry
        from .helpers import resolve_entity_id

        self._entity_date_dernier = resolve_entity_id(
            self._hass,
            "datetime",
            self.tracker_id,
            self._date_dernier_key,
        )
        self._entity_intervalle_j = resolve_entity_id(
            self._hass,
            "number",
            self.tracker_id,
            self._intervalle_j_key,
        )

        from homeassistant.helpers.event import (
            async_track_state_change_event,
            async_track_time_change,
        )

        watched = [
            eid
            for eid in [self._entity_date_dernier, self._entity_intervalle_j]
            if eid is not None
        ]
        self.async_on_remove(
            async_track_state_change_event(
                self._hass,
                watched,
                self._handle_state_change,
            )
        )
        # Daily recompute at midnight (the day count changes every day even without action)
        self.async_on_remove(
            async_track_time_change(
                self._hass,
                self._handle_midnight,
                hour=0,
                minute=0,
                second=0,
            )
        )
        # No _recalculate() here — we wait for the first state_change_event

    @callback
    def _handle_state_change(self, event) -> None:
        self._recalculate()
        self.async_write_ha_state()

    @callback
    def _handle_midnight(self, now) -> None:
        self._recalculate()
        self.async_write_ha_state()

    def _get_float(self, entity_id: str | None, default: float = 0.0) -> float:
        if entity_id is None:
            return default
        state = self._hass.states.get(entity_id)
        if state and state.state not in (None, "unknown", "unavailable"):
            try:
                return float(state.state)
            except (ValueError, TypeError):
                pass
        return default

    def _recalculate(self) -> None:
        intervalle_j = self._get_float(self._entity_intervalle_j, 0.0)

        # Read the last maintenance date from the datetime entity
        if self._entity_date_dernier is None:
            self._attr_native_value = 0.0
            return
        dt_state = self._hass.states.get(self._entity_date_dernier)
        if dt_state is None or dt_state.state in (None, "unknown", "unavailable"):
            self._attr_native_value = 0.0
            return

        if intervalle_j == 0:
            self._attr_native_value = 0.0
            return

        try:
            date_dernier = datetime.fromisoformat(dt_state.state)
            if date_dernier.tzinfo is None:
                from datetime import timezone as tz

                date_dernier = date_dernier.replace(tzinfo=tz.utc)
        except (ValueError, TypeError):
            self._attr_native_value = 0.0
            return

        from datetime import timezone as tz

        now = datetime.now(tz.utc)
        echeance = date_dernier + timedelta(days=intervalle_j)
        jours_restants = (echeance - now).days

        self._attr_native_value = float(jours_restants)

        _LOGGER.debug(
            "%s: last=%s + %d days → due=%s → remaining=%d d",
            self._attr_name,
            date_dernier.date(),
            int(intervalle_j),
            echeance.date(),
            jours_restants,
        )

    @property
    def extra_state_attributes(self) -> dict:
        intervalle_j = self._get_float(self._entity_intervalle_j)
        dt_state = (
            self._hass.states.get(self._entity_date_dernier)
            if self._entity_date_dernier
            else None
        )
        date_str = dt_state.state if dt_state else None

        echeance_str = None
        if date_str and intervalle_j > 0:
            try:
                date_dernier = datetime.fromisoformat(date_str)
                echeance = date_dernier + timedelta(days=int(intervalle_j))
                echeance_str = echeance.date().isoformat()
            except (ValueError, TypeError):
                pass

        return {
            "last_service_date": date_str,
            "day_interval": int(intervalle_j),
            "due_date": echeance_str,
        }


class GeoRideJoursRestantsDrivetrainSensor(GeoRideJoursRestantsRevisionSensor):
    """Days remaining before drivetrain maintenance (only meaningful when
    drivetrain_day_interval > 0, e.g. shaft final-drive oil). When the interval
    is 0 (chain/belt), the value stays 0 and the due binary sensor ignores it.
    """

    _unique_id_suffix = "drivetrain_remaining_days"
    _name_suffix = "Drivetrain – remaining days"
    _date_dernier_key = "drivetrain_last_service_date"
    _intervalle_j_key = "drivetrain_day_interval"

    def __init__(self, entry, tracker, hass, label="Drivetrain") -> None:
        self._name_suffix = f"{label} – remaining days"
        super().__init__(entry, tracker, hass)


# ════════════════════════════════════════════════════════════════════════════
# SENSORS — TRACKER STATUS (fed by GeoRideTrackerStatusCoordinator)
# ════════════════════════════════════════════════════════════════════════════


class GeoRideTrackerStatusSensor(CoordinatorEntity, SensorEntity):
    """Sensor exposing the tracker's network status (online / offline)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: GeoRideTrackerStatusCoordinator, entry, tracker):
        super().__init__(coordinator)
        self.tracker_id = str(tracker.get("trackerId"))
        self.tracker_name = tracker.get("trackerName", f"Tracker {self.tracker_id}")
        self._entry = entry
        self._tracker = tracker
        self._attr_name = "Status"
        self._attr_unique_id = f"{self.tracker_id}_tracker_status"
        self._attr_icon = "mdi:signal"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.tracker_id)},
            name=self.tracker_name,
            manufacturer="GeoRide",
            model=self._tracker.get("model", "GeoRide Tracker"),
            sw_version=str(self._tracker.get("softwareVersion", "")),
        )

    @property
    def native_value(self) -> str | None:
        data = self.coordinator.data
        if not data:
            return None
        return data.get("status")

    @property
    def icon(self) -> str:
        if self.coordinator.data and self.coordinator.data.get("status") == "online":
            return "mdi:signal"
        return "mdi:signal-off"


class GeoRideExternalBatterySensor(CoordinatorEntity, SensorEntity):
    """Sensor for the external battery voltage (GeoRide 3 only)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: GeoRideTrackerStatusCoordinator, entry, tracker):
        super().__init__(coordinator)
        self.tracker_id = str(tracker.get("trackerId"))
        self.tracker_name = tracker.get("trackerName", f"Tracker {self.tracker_id}")
        self._entry = entry
        self._tracker = tracker
        self._attr_name = "External battery"
        self._attr_unique_id = f"{self.tracker_id}_external_battery"
        self._attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT
        self._attr_device_class = SensorDeviceClass.VOLTAGE
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_icon = "mdi:battery-charging"
        self._attr_suggested_display_precision = 2

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.tracker_id)},
            name=self.tracker_name,
            manufacturer="GeoRide",
            model=self._tracker.get("model", "GeoRide Tracker"),
            sw_version=str(self._tracker.get("softwareVersion", "")),
        )

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data
        if not data:
            return None
        voltage = data.get("externalBatteryVoltage")
        if voltage is None:
            return None
        try:
            return round(float(voltage), 2)
        except (ValueError, TypeError):
            return None

    @property
    def available(self) -> bool:
        """Available only if the tracker returns this value (GeoRide 3)."""
        if not self.coordinator.last_update_success:
            return False
        data = self.coordinator.data
        return bool(data and data.get("externalBatteryVoltage") is not None)


class GeoRideInternalBatterySensor(CoordinatorEntity, SensorEntity):
    """Sensor for the internal battery voltage (GeoRide 3 only)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: GeoRideTrackerStatusCoordinator, entry, tracker):
        super().__init__(coordinator)
        self.tracker_id = str(tracker.get("trackerId"))
        self.tracker_name = tracker.get("trackerName", f"Tracker {self.tracker_id}")
        self._entry = entry
        self._tracker = tracker
        self._attr_name = "Internal battery"
        self._attr_unique_id = f"{self.tracker_id}_internal_battery"
        self._attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT
        self._attr_device_class = SensorDeviceClass.VOLTAGE
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_icon = "mdi:battery"
        self._attr_suggested_display_precision = 2

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.tracker_id)},
            name=self.tracker_name,
            manufacturer="GeoRide",
            model=self._tracker.get("model", "GeoRide Tracker"),
            sw_version=str(self._tracker.get("softwareVersion", "")),
        )

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data
        if not data:
            return None
        voltage = data.get("internalBatteryVoltage")
        if voltage is None:
            return None
        try:
            return round(float(voltage), 2)
        except (ValueError, TypeError):
            return None

    @property
    def available(self) -> bool:
        """Available only if the tracker returns this value (GeoRide 3)."""
        if not self.coordinator.last_update_success:
            return False
        data = self.coordinator.data
        return bool(data and data.get("internalBatteryVoltage") is not None)


# ════════════════════════════════════════════════════════════════════════════
# SENSOR — LAST ALARM (fed by Socket.IO)
# ════════════════════════════════════════════════════════════════════════════


class GeoRideLastAlarmSensor(RestoreEntity, SensorEntity):
    """Sensor exposing the type of the last alarm received via Socket.IO."""

    _attr_has_entity_name = True

    def __init__(self, entry, tracker):
        self.tracker_id = str(tracker.get("trackerId"))
        self.tracker_name = tracker.get("trackerName", f"Tracker {self.tracker_id}")
        self._entry = entry
        self._tracker = tracker
        self._attr_name = "Last alarm"
        self._attr_unique_id = f"{self.tracker_id}_last_alarm"
        self._attr_icon = "mdi:alarm-light"
        self._attr_entity_category = None
        self._state: str | None = None
        self._alarm_timestamp: str | None = None
        self._device_name: str | None = None
        self._unregister_alarm: callable | None = None

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.tracker_id)},
            name=self.tracker_name,
            manufacturer="GeoRide",
            model=self._tracker.get("model", "GeoRide Tracker"),
            sw_version=str(self._tracker.get("softwareVersion", "")),
        )

    @property
    def native_value(self) -> str | None:
        return self._state

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "timestamp": self._alarm_timestamp,
            "device_name": self._device_name,
            "tracker_id": self.tracker_id,
        }

    async def async_added_to_hass(self) -> None:
        """Restore the state and register with the socket_manager."""
        await super().async_added_to_hass()

        # Restoration from the recorder
        last_state = await self.async_get_last_state()
        if last_state and last_state.state not in ("unknown", "unavailable"):
            self._state = last_state.state
            self._alarm_timestamp = last_state.attributes.get("timestamp")
            self._device_name = last_state.attributes.get("device_name")

        # Socket.IO callback registration
        entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        socket_manager = entry_data.get("socket_manager")
        if socket_manager:
            self._unregister_alarm = socket_manager.register_callback(
                self.tracker_id, "alarm", self._handle_alarm
            )
            _LOGGER.debug(
                "LastAlarmSensor %s registered with socket_manager", self.tracker_id
            )
        else:
            _LOGGER.debug(
                "LastAlarmSensor %s: no socket_manager available at startup "
                "(normal if Socket.IO starts after the entities)",
                self.tracker_id,
            )

    async def async_will_remove_from_hass(self) -> None:
        """Unregister from the socket_manager."""
        if self._unregister_alarm:
            self._unregister_alarm()
            self._unregister_alarm = None

    def _handle_alarm(self, data: dict) -> None:
        """Callback called by socket_manager on an alarm."""
        # GeoRide sends the type in 'name' (e.g. "sonorAlarmOn"),
        # fallback to 'alarmType' or 'type' for compatibility.
        alarm_type = data.get("name") or data.get("alarmType") or data.get("type")
        if not alarm_type:
            return

        self._state = alarm_type
        self._alarm_timestamp = data.get("timestamp") or data.get("date")
        self._device_name = data.get("device_name") or self.tracker_name

        self.schedule_update_ha_state()
        _LOGGER.info(
            "LastAlarmSensor %s: new alarm %s",
            self.tracker_id,
            alarm_type,
        )
