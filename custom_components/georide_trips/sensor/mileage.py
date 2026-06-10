"""GeoRide periodic mileage sensors — daily, weekly, monthly km."""

import logging
from typing import TYPE_CHECKING

from homeassistant.components.sensor import (
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfLength
from homeassistant.core import callback
from homeassistant.helpers.restore_state import RestoreEntity

from ..helpers import GeoRideEntityMixin

if TYPE_CHECKING:
    from .odometer import GeoRideRealOdometerSensor

_LOGGER = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════
# SENSORS — PERIODIC KM (daily, weekly, monthly)
# ════════════════════════════════════════════════════════════════════════════


class _GeoRideKmPeriodBase(GeoRideEntityMixin, SensorEntity, RestoreEntity):
    """Base class for the periodic km sensors.

    Computation: max(odometer - snapshot_debut, 0)

    Subscribes to:
      - sensor.<moto>_odometer  (via a direct reference to GeoRideRealOdometerSensor)
      - number.<moto>_km_debut_<periode>  (start-of-period snapshot)
    """

    def __init__(
        self,
        entry,
        tracker,
        hass,
        odometer_sensor: "GeoRideRealOdometerSensor",
        unique_id_suffix: str,
        name_suffix: str,
        icon: str,
        snapshot_key: str,
    ) -> None:
        self._entry = entry
        self._tracker = tracker
        self._hass = hass
        self._odometer_sensor = odometer_sensor
        self._snapshot_key = snapshot_key
        self._snapshot_entity: str | None = None
        self._snapshot_subscribed = False

        self.tracker_id = str(tracker.get("trackerId"))
        self.tracker_name = tracker.get("trackerName", f"Tracker {self.tracker_id}")

        self._attr_unique_id = f"{self.tracker_id}_{unique_id_suffix}"
        self._attr_name = name_suffix
        self._attr_icon = icon
        self._attr_native_unit_of_measurement = UnitOfLength.KILOMETERS
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_entity_category = None
        self._attr_native_value: float = 0.0

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

        # Subscribe to changes of the odometer AND the snapshot. The snapshot
        # number may not be registered yet (platforms set up concurrently);
        # in that case _handle_state_change retries the subscription.
        watched = [self._odometer_sensor.entity_id]
        if (snapshot := self._resolve_snapshot_entity()) is not None:
            watched.append(snapshot)
            self._snapshot_subscribed = True
        self.async_on_remove(
            async_track_state_change_event(
                self._hass,
                watched,
                self._handle_state_change,
            )
        )
        # No _recalculate() here — we wait for the first state_change_event

    def _resolve_snapshot_entity(self) -> str | None:
        """Resolve the snapshot number's entity_id via the registry (cached)."""
        if self._snapshot_entity is None:
            from ..helpers import resolve_entity_id

            self._snapshot_entity = resolve_entity_id(
                self._hass, "number", self.tracker_id, self._snapshot_key
            )
        return self._snapshot_entity

    @callback
    def _handle_state_change(self, event) -> None:
        # Late subscription: the snapshot number was not yet registered when
        # this sensor was added — retry now that something changed.
        if not self._snapshot_subscribed:
            if (snapshot := self._resolve_snapshot_entity()) is not None:
                from homeassistant.helpers.event import (
                    async_track_state_change_event,
                )

                self.async_on_remove(
                    async_track_state_change_event(
                        self._hass, [snapshot], self._handle_state_change
                    )
                )
                self._snapshot_subscribed = True

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

    def _is_snapshot_ready(self) -> bool:
        """Return True if the snapshot entity is available and non-zero."""
        snapshot = self._resolve_snapshot_entity()
        if snapshot is None:
            return False
        state = self._hass.states.get(snapshot)
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

        snapshot_km = self._get_float(self._resolve_snapshot_entity(), 0.0)
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
        snapshot = self._resolve_snapshot_entity()
        return {
            "current_odometer": self._odometer_sensor.native_value,
            "snapshot_start": self._get_float(snapshot) if snapshot else None,
            "snapshot_entity": snapshot,
        }


class GeoRideKmJournaliersSensor(_GeoRideKmPeriodBase):
    """Sensor for km traveled today (odometer - midnight snapshot)."""

    def __init__(self, entry, tracker, hass, odometer_sensor) -> None:
        super().__init__(
            entry=entry,
            tracker=tracker,
            hass=hass,
            odometer_sensor=odometer_sensor,
            unique_id_suffix="daily_mileage",
            name_suffix="Daily mileage",
            icon="mdi:counter",
            snapshot_key="odometer_at_day_start",
        )


class GeoRideKmHebdomadairesSensor(_GeoRideKmPeriodBase):
    """Sensor for km traveled this week (odometer - Monday midnight snapshot)."""

    def __init__(self, entry, tracker, hass, odometer_sensor) -> None:
        super().__init__(
            entry=entry,
            tracker=tracker,
            hass=hass,
            odometer_sensor=odometer_sensor,
            unique_id_suffix="weekly_mileage",
            name_suffix="Weekly mileage",
            icon="mdi:calendar-week",
            snapshot_key="odometer_at_week_start",
        )


class GeoRideKmMensuelsSensor(_GeoRideKmPeriodBase):
    """Sensor for km traveled this month (odometer - 1st-of-month snapshot)."""

    def __init__(self, entry, tracker, hass, odometer_sensor) -> None:
        super().__init__(
            entry=entry,
            tracker=tracker,
            hass=hass,
            odometer_sensor=odometer_sensor,
            unique_id_suffix="monthly_mileage",
            name_suffix="Monthly mileage",
            icon="mdi:calendar-month",
            snapshot_key="odometer_at_month_start",
        )
