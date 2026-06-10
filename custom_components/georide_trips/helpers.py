"""GeoRide Trips — shared utilities.

Centralizes the functions and mixins reused throughout the project:
- GeoRideEntityMixin : device_info + _get_float
- resolve_entity_id  : reliable entity_id resolution via the entity registry
"""

import logging
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class GeoRideEntityMixin:
    """Mixin providing device_info and _get_float for all GeoRide entities.

    The inheriting class must define:
      - self.tracker_id  (str)
      - self.tracker_name (str)
      - self._tracker    (dict)
      - self._hass       (HomeAssistant)  — only for _get_float
    """

    _attr_has_entity_name = True

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.tracker_id)},
            name=self.tracker_name,
            manufacturer="GeoRide",
            model=self._tracker.get("model", "GeoRide Tracker"),
            sw_version=str(self._tracker.get("softwareVersion", "")),
        )

    def _get_float(self, entity_id: str, default: float = 0.0) -> float:
        """Read the numeric value of an HA entity, with a fallback."""
        hass = getattr(self, "_hass", None) or getattr(self, "hass", None)
        if hass is None:
            return default
        state = hass.states.get(entity_id)
        if state and state.state not in (None, "unknown", "unavailable"):
            try:
                return float(state.state)
            except (ValueError, TypeError):
                pass
        return default


def resolve_entity_id(
    hass: HomeAssistant,
    domain: str,
    tracker_id: str,
    key: str,
) -> str | None:
    """Resolve a GeoRide entity's entity_id via the entity registry.

    Uses the unique_id = "{tracker_id}_{key}" to find the real
    entity_id, independent of the slug derived from the name.

    Args:
        hass: Home Assistant instance
        domain: the entity's domain (e.g. "number", "datetime", "sensor")
        tracker_id: GeoRide tracker ID
        key: the entity's key (e.g. "fuel_total_range", "fuel_km_at_last_refuel")

    Returns:
        entity_id (e.g. "number.my_bike_fuel_total_range") or None
    """
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    unique_id = f"{tracker_id}_{key}"
    entity_id = registry.async_get_entity_id(domain, DOMAIN, unique_id)

    if entity_id is None:
        _LOGGER.debug(
            "resolve_entity_id: entity not found — domain=%s unique_id=%s",
            domain,
            unique_id,
        )
    return entity_id
