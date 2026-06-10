"""GeoRide Trips datetime entities.

Datetime entities attached to each motorcycle's device:

── Drivetrain maintenance (adaptive: chain / shaft / belt) ───────
- drivetrain_last_service_date   : date of the last drivetrain maintenance

── Oil change maintenance ────────────────────────────────────────
- oil_change_last_oil_change_date  : date of the last oil change

── Service maintenance ───────────────────────────────────────────
- service_last_service_date : date of the last service

── Pending refuel (internal use) ─────────────────────────────────
- refuel_pending_at : timestamp of the pending refuel (epoch 1970 = no pending refuel)
"""

import logging
from datetime import datetime, timezone

from homeassistant.components.datetime import DateTimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    CONF_DRIVE_TYPE,
    DEFAULT_DRIVE_TYPE,
    DRIVETRAIN_PROFILES,
)
from .data import GeoRideConfigEntry
from .helpers import GeoRideEntityMixin

_LOGGER = logging.getLogger(__name__)

# "No pending refuel" sentinel — convention shared with button.py
# (_get_datetime treats year 1970 as None, _set_datetime(None) writes 1970).
EPOCH_SENTINEL = datetime(1970, 1, 1, tzinfo=timezone.utc)

DATETIME_DESCRIPTIONS = [
    {
        "key": "drivetrain_last_service_date",
        "name": "Drivetrain – last service date",
        "icon": "mdi:calendar-check",
        "entity_category": EntityCategory.CONFIG,
    },
    {
        "key": "oil_change_last_oil_change_date",
        "name": "Oil change – last oil-change date",
        "icon": "mdi:calendar-check",
        "entity_category": EntityCategory.CONFIG,
    },
    {
        "key": "service_last_service_date",
        "name": "Service – last service date",
        "icon": "mdi:calendar-check",
        "entity_category": EntityCategory.CONFIG,
    },
    {
        "key": "refuel_pending_at",
        "name": "Refuel – pending timestamp",
        "icon": "mdi:clock-outline",
        "entity_category": EntityCategory.DIAGNOSTIC,
        "default_epoch": True,
    },
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GeoRideConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up GeoRide Trips datetime entities from a config entry."""
    data = entry.runtime_data
    trackers = data.trackers

    profile = DRIVETRAIN_PROFILES.get(
        entry.options.get(CONF_DRIVE_TYPE, DEFAULT_DRIVE_TYPE),
        DRIVETRAIN_PROFILES["chain"],
    )

    entities = []
    for tracker in trackers:
        for desc in DATETIME_DESCRIPTIONS:
            if desc["key"] == "drivetrain_last_service_date":
                desc = {**desc, "name": f"{profile['label']} – last service date"}
            entities.append(GeoRideDateTimeEntity(entry, tracker, desc))

    async_add_entities(entities)
    _LOGGER.info(
        "Added %d datetime entities for %d trackers",
        len(entities),
        len(trackers),
    )


class GeoRideDateTimeEntity(GeoRideEntityMixin, DateTimeEntity, RestoreEntity):
    """Persistent datetime entity attached to the GeoRide device."""

    def __init__(self, entry: ConfigEntry, tracker: dict, desc: dict) -> None:
        self._entry = entry
        self._tracker = tracker
        self._desc = desc

        self._tracker_id = str(tracker.get("trackerId"))
        self._tracker_name = tracker.get("trackerName", f"Tracker {self._tracker_id}")
        # Mixin-required public attributes
        self.tracker_id = self._tracker_id
        self.tracker_name = self._tracker_name

        self._attr_unique_id = f"{self._tracker_id}_{desc['key']}"
        self._attr_name = desc["name"]
        self._attr_icon = desc["icon"]
        self._attr_entity_category = desc.get("entity_category")

        # Default value: 1970 sentinel for refuel_pending_at (a default of
        # now() would simulate a pending refuel right from install → phantom
        # refuel calculation on the first lock), otherwise now (UTC).
        if desc.get("default_epoch"):
            self._attr_native_value: datetime = EPOCH_SENTINEL
        else:
            self._attr_native_value: datetime = datetime.now(timezone.utc)

    async def async_added_to_hass(self) -> None:
        """Restore the last state on restart."""
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None:
            if last_state.state not in (None, "unknown", "unavailable"):
                try:
                    restored = datetime.fromisoformat(last_state.state)
                    # Ensure the datetime is timezone-aware (UTC)
                    if restored.tzinfo is None:
                        restored = restored.replace(tzinfo=timezone.utc)
                    self._attr_native_value = restored
                    _LOGGER.debug(
                        "Restored %s for %s: %s",
                        self._desc["key"],
                        self._tracker_name,
                        restored,
                    )
                except (ValueError, TypeError) as err:
                    _LOGGER.warning(
                        "Could not restore datetime for %s: %s",
                        self._attr_unique_id,
                        err,
                    )

    async def async_set_value(self, value: datetime) -> None:
        """Update the date from the UI or an automation."""
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        self._attr_native_value = value
        self.async_write_ha_state()
        _LOGGER.debug(
            "Set %s for %s: %s",
            self._desc["key"],
            self._tracker_name,
            value,
        )
