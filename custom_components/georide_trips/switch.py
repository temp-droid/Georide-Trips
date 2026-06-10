"""GeoRide Trips switch entities.

Switches attached to each motorcycle's device:
- Eco mode  : reflects and controls isInEco via the GeoRide API.
- Lock      : reflects and controls isLocked via the GeoRide API.

Note: The maintenance indicators (refuel required, drivetrain, oil change, service)
are now binary_sensors computed in real time (read-only).
See binary_sensor.py: GeoRidePleinRequisBinarySensor, etc.
"""

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .helpers import GeoRideEntityMixin

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up GeoRide Trips switch entities from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    trackers = data["trackers"]
    tracker_status_coordinators = data["tracker_status_coordinators"]
    api = data["api"]

    entities = []
    for tracker in trackers:
        tracker_id = str(tracker.get("trackerId"))
        status_coordinator = tracker_status_coordinators[tracker_id]
        entities.extend(
            [
                GeoRideEcoModeSwitch(status_coordinator, entry, tracker, api),
                GeoRideLockSwitch(status_coordinator, entry, tracker, api),
            ]
        )

    async_add_entities(entities)
    _LOGGER.info("Added %d switches for %d trackers", len(entities), len(trackers))


class GeoRideEcoModeSwitch(GeoRideEntityMixin, CoordinatorEntity, SwitchEntity):
    """Switch to enable/disable the GeoRide tracker's eco mode.

    The state is read from the GeoRideTrackerStatusCoordinator (polling /user/trackers).
    The change is sent via PUT /tracker/{id}/eco.
    """

    def __init__(self, coordinator, entry: ConfigEntry, tracker: dict, api) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._tracker = tracker
        self._api = api
        self._tracker_id = str(tracker.get("trackerId"))
        self._tracker_name = tracker.get("trackerName", f"Tracker {self._tracker_id}")
        # Mixin-required public attributes
        self.tracker_id = self._tracker_id
        self.tracker_name = self._tracker_name
        self._attr_unique_id = f"{self._tracker_id}_eco_mode"
        self._attr_name = "Eco mode"
        self._attr_icon = "mdi:leaf"
        self._attr_entity_category = None

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data
        if not data:
            return None
        return bool(data.get("isInEco", False))

    @property
    def icon(self) -> str:
        return "mdi:leaf" if self.is_on else "mdi:leaf-off"

    async def async_turn_on(self, **kwargs) -> None:
        """Enable eco mode."""
        success = await self._api.set_eco_mode(self._tracker_id, True)
        if success:
            await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        """Disable eco mode."""
        success = await self._api.set_eco_mode(self._tracker_id, False)
        if success:
            await self.coordinator.async_request_refresh()


class GeoRideLockSwitch(GeoRideEntityMixin, CoordinatorEntity, SwitchEntity):
    """Switch to lock/unlock the GeoRide tracker.

    The state is read from the GeoRideTrackerStatusCoordinator (`isLocked` field).
    The toggle is sent via POST /tracker/{id}/toggleLock.
    On = locked, Off = unlocked.
    """

    def __init__(self, coordinator, entry: ConfigEntry, tracker: dict, api) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._tracker = tracker
        self._api = api
        self._tracker_id = str(tracker.get("trackerId"))
        self._tracker_name = tracker.get("trackerName", f"Tracker {self._tracker_id}")
        # Mixin-required public attributes
        self.tracker_id = self._tracker_id
        self.tracker_name = self._tracker_name
        self._attr_unique_id = f"{self._tracker_id}_lock"
        self._attr_name = "Lock"
        self._attr_icon = "mdi:lock"
        self._attr_entity_category = None

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data
        if not data:
            return None
        return bool(data.get("isLocked", False))

    @property
    def icon(self) -> str:
        return "mdi:lock" if self.is_on else "mdi:lock-open-variant"

    async def _toggle_if_needed(self, target_locked: bool) -> None:
        """Call toggleLock only if the current state differs from the target."""
        current = self.is_on
        if current is None or current != target_locked:
            new_state = await self._api.toggle_lock(self._tracker_id)
            if new_state is not None:
                await self.coordinator.async_request_refresh()
        else:
            _LOGGER.debug(
                "Tracker %s already %s, skipping toggle",
                self._tracker_id,
                "locked" if target_locked else "unlocked",
            )

    async def async_turn_on(self, **kwargs) -> None:
        """Lock the tracker."""
        await self._toggle_if_needed(True)

    async def async_turn_off(self, **kwargs) -> None:
        """Unlock the tracker."""
        await self._toggle_if_needed(False)
