"""GeoRide fuel sensor — remaining range (autonomy)."""

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
# SENSOR — REMAINING RANGE (reactive)
# ════════════════════════════════════════════════════════════════════════════


class GeoRideAutonomySensor(GeoRideEntityMixin, SensorEntity, RestoreEntity):
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
        from ..helpers import resolve_entity_id

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
