"""GeoRide Trips data update coordinators."""

import logging
from datetime import datetime, timedelta

from homeassistant.core import callback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

_LOGGER = logging.getLogger(__name__)


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
