"""GeoRide maintenance sensors — remaining km and remaining days."""

import logging
from datetime import datetime, timedelta
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
# SENSORS — MAINTENANCE (remaining km + remaining days computed in Python)
# ════════════════════════════════════════════════════════════════════════════


class _GeoRideEntretienKmBase(GeoRideEntityMixin, SensorEntity, RestoreEntity):
    """Base class for the remaining-km maintenance sensors.

    Common computation:
      km_restants = km_dernier_entretien + intervalle_km - odometer_actuel
      (can be negative: maintenance overdue)

    Subscribes to:
      - sensor.<moto>_odometer  (via a direct reference to GeoRideRealOdometerSensor)
      - number.<moto>_<intervalle_key>  (resolved via the entity registry)
      - number.<moto>_<km_dernier_key>  (resolved via the entity registry)
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

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        if (last_state := await self.async_get_last_state()) is not None:
            if last_state.state not in (None, "unknown", "unavailable"):
                try:
                    self._attr_native_value = float(last_state.state)
                except (ValueError, TypeError):
                    pass

        # Resolve the entity_ids via the registry
        from ..helpers import resolve_entity_id

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


# Descriptions for the fixed maintenance km sensors.
# Each tuple: (unique_id_suffix, name_suffix, icon, intervalle_key, km_dernier_key)
MAINTENANCE_KM_DESCRIPTIONS = [
    (
        "oil_change_remaining_km",
        "Oil change – remaining km",
        "mdi:oil",
        "oil_change_km_interval",
        "oil_change_km_at_last_oil_change",
    ),
    (
        "service_remaining_km",
        "Service – remaining km",
        "mdi:wrench",
        "service_km_interval",
        "service_km_at_last_service",
    ),
]

# The drivetrain sensor takes a dynamic label, so it is instantiated separately
# in async_setup_entry using _GeoRideEntretienKmBase directly.
DRIVETRAIN_KM_DESCRIPTION = (
    "drivetrain_remaining_km",
    "mdi:link-variant",
    "drivetrain_km_interval",
    "drivetrain_km_at_last_service",
)


class GeoRideJoursRestantsRevisionSensor(
    GeoRideEntityMixin, SensorEntity, RestoreEntity
):
    """Sensor for days remaining before service (based on last maintenance date + interval in days).

    Computation:
      jours_restants = (date_dernier_entretien + intervalle_jours) - today
      (can be negative: service overdue)

    Subscribes to:
      - datetime.<moto>_entretien_revision_date_derniere_revision
      - number.<moto>_entretien_revision_intervalle_jours
    """

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

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        if (last_state := await self.async_get_last_state()) is not None:
            if last_state.state not in (None, "unknown", "unavailable"):
                try:
                    self._attr_native_value = float(last_state.state)
                except (ValueError, TypeError):
                    pass

        # Resolve the entity_ids via the registry
        from ..helpers import resolve_entity_id

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
