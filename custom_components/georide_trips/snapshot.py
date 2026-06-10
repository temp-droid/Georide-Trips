"""GeoRide midnight odometer snapshot manager."""

import logging
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_change

if TYPE_CHECKING:
    from .sensor.odometer import GeoRideRealOdometerSensor

_LOGGER = logging.getLogger(__name__)


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
