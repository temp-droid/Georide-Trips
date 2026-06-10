"""GeoRide Trips number entities.

number entities attached to each motorcycle's device:

── Fuel ────────────────────────────────────────────────────────────
- fuel_total_range              : theoretical km on a full tank (config)
- fuel_range_alert_threshold        : remaining km to trigger the alert (config)
- fuel_km_at_last_refuel              : odometer snapshot at the last refuel
- km_restants_avant_plein       : computed remaining range (diagnostic)
- fuel_distance_between_refuels_1/2/3           : FIFO history of the last 3 inter-refuel distances
- fuel_calculated_average_range    : rolling average over 3 refuels (diagnostic)
- fuel_recorded_refuel_count         : counter of confirmed refuels

── Periodic mileage ───────────────────────────────────────────────
- odometer_at_day_start              : odometer snapshot at midnight (diagnostic)
- odometer_at_week_start              : odometer snapshot Monday midnight (diagnostic)
- odometer_at_month_start                 : odometer snapshot on the 1st of the month (diagnostic)
  → daily_mileage / weekly_mileage / monthly_mileage : computed in Python (sensor.py)
  → The snapshots are updated automatically at midnight by MidnightSnapshotManager (sensor.py)

── Drivetrain maintenance (adaptive: chain / shaft / belt) ───────
- drivetrain_km_interval         : km between two maintenances (config)
- drivetrain_day_interval        : max days between maintenances (config, 0 = km-only)
- drivetrain_alert_threshold     : km before due to alert (config)
- drivetrain_km_at_last_service  : odometer snapshot at the last maintenance (config)
- drivetrain_remaining_km        : km remaining before due (diagnostic)

── Oil change maintenance ────────────────────────────────────────
- oil_change_km_interval         : km between two oil changes (config)
- oil_change_alert_threshold          : km before due to alert (config)
- oil_change_km_at_last_oil_change  : odometer snapshot at the last oil change (config)
- oil_change_remaining_km           : km remaining before due (diagnostic)

── Service maintenance ───────────────────────────────────────────
- service_km_interval            : km between two services (config)
- service_day_interval         : max days between services (config)
- service_alert_threshold             : km before due to alert (config)
- service_km_at_last_service     : odometer snapshot at the last service (config)
- km_restants_avant_entretien_revision : km remaining before due (diagnostic)

── Trips ──────────────────────────────────────────────────────────
- trip_notification_threshold         : minimum distance to notify (config)

── Offset ────────────────────────────────────────────────────────
- odometer_offset               : mileage offset (km already on the tracker)
"""

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfLength
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.storage import Store

from .const import (
    DOMAIN,
    CONF_DRIVE_TYPE,
    DEFAULT_DRIVE_TYPE,
    DRIVETRAIN_PROFILES,
)
from .helpers import GeoRideEntityMixin

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY = "georide_trips_numbers"

NUMBER_DESCRIPTIONS = [
    # ── Odometer offset ───────────────────────────────────────────────────────
    {
        "key": "odometer_offset",
        "name": "Odometer offset",
        "icon": "mdi:plus-circle",
        "unit": UnitOfLength.KILOMETERS,
        "min": -100_000,
        "max": 100_000,
        "step": 0.1,
        "default": 0,
        "mode": NumberMode.BOX,
        "entity_category": EntityCategory.CONFIG,
    },
    # ── Fuel ──────────────────────────────────────────────────────────────────
    {
        "key": "fuel_total_range",
        "name": "Fuel – total range",
        "icon": "mdi:gas-station",
        "unit": UnitOfLength.KILOMETERS,
        "min": 50,
        "max": 800,
        "step": 1,
        "default": 150,
        "mode": NumberMode.BOX,
        "entity_category": EntityCategory.CONFIG,
    },
    {
        "key": "fuel_range_alert_threshold",
        "name": "Fuel – range alert threshold",
        "icon": "mdi:alert-circle",
        "unit": UnitOfLength.KILOMETERS,
        "min": 0,
        "max": 200,
        "step": 5,
        "default": 30,
        "mode": NumberMode.SLIDER,
        "entity_category": EntityCategory.CONFIG,
    },
    {
        "key": "fuel_km_at_last_refuel",
        "name": "Fuel – km at last refuel",
        "icon": "mdi:gas-station-outline",
        "unit": UnitOfLength.KILOMETERS,
        "min": 0,
        "max": 200_000,
        "step": 0.1,
        "default": 0,
        "mode": NumberMode.BOX,
        "entity_category": EntityCategory.CONFIG,
    },
    # ── Rolling average of refuels ────────────────────────────────────────────
    {
        "key": "fuel_distance_between_refuels_1",
        "name": "Fuel – distance between refuels (n-1)",
        "icon": "mdi:gas-station",
        "unit": UnitOfLength.KILOMETERS,
        "min": 0,
        "max": 1_500,
        "step": 0.1,
        "default": 0,
        "mode": NumberMode.BOX,
        "entity_category": EntityCategory.DIAGNOSTIC,
    },
    {
        "key": "fuel_distance_between_refuels_2",
        "name": "Fuel – distance between refuels (n-2)",
        "icon": "mdi:gas-station",
        "unit": UnitOfLength.KILOMETERS,
        "min": 0,
        "max": 1_500,
        "step": 0.1,
        "default": 0,
        "mode": NumberMode.BOX,
        "entity_category": EntityCategory.DIAGNOSTIC,
    },
    {
        "key": "fuel_distance_between_refuels_3",
        "name": "Fuel – distance between refuels (n-3)",
        "icon": "mdi:gas-station",
        "unit": UnitOfLength.KILOMETERS,
        "min": 0,
        "max": 1_500,
        "step": 0.1,
        "default": 0,
        "mode": NumberMode.BOX,
        "entity_category": EntityCategory.DIAGNOSTIC,
    },
    {
        "key": "fuel_calculated_average_range",
        "name": "Fuel – calculated average range",
        "icon": "mdi:gas-station-outline",
        "unit": UnitOfLength.KILOMETERS,
        "min": 0,
        "max": 1_500,
        "step": 1,
        "default": 0,
        "mode": NumberMode.BOX,
        "entity_category": EntityCategory.DIAGNOSTIC,
    },
    {
        "key": "fuel_recorded_refuel_count",
        "name": "Fuel – recorded refuel count",
        "icon": "mdi:counter",
        "unit": None,
        "min": 0,
        "max": 9_999,
        "step": 1,
        "default": 0,
        "mode": NumberMode.BOX,
        "entity_category": EntityCategory.DIAGNOSTIC,
    },
    # ── Periodic mileage ────────────────────────────────────────────────────────
    {
        "key": "odometer_at_day_start",
        "name": "Odometer at day start",
        "icon": "mdi:clock-start",
        "unit": UnitOfLength.KILOMETERS,
        "min": 0,
        "max": 200_000,
        "step": 0.1,
        "default": 0,
        "mode": NumberMode.BOX,
        "entity_category": EntityCategory.DIAGNOSTIC,
    },
    {
        "key": "odometer_at_week_start",
        "name": "Odometer at week start",
        "icon": "mdi:calendar-week",
        "unit": UnitOfLength.KILOMETERS,
        "min": 0,
        "max": 200_000,
        "step": 0.1,
        "default": 0,
        "mode": NumberMode.BOX,
        "entity_category": EntityCategory.DIAGNOSTIC,
    },
    {
        "key": "odometer_at_month_start",
        "name": "Odometer at month start",
        "icon": "mdi:calendar-month",
        "unit": UnitOfLength.KILOMETERS,
        "min": 0,
        "max": 200_000,
        "step": 0.1,
        "default": 0,
        "mode": NumberMode.BOX,
        "entity_category": EntityCategory.DIAGNOSTIC,
    },
    # ── Drivetrain maintenance (adaptive: chain / shaft / belt) ───────────────
    # name/default values are overridden per config entry from the active
    # DRIVETRAIN_PROFILE in async_setup_entry (see _drivetrain_overrides()).
    {
        "key": "drivetrain_km_interval",
        "name": "Drivetrain – km interval",
        "icon": "mdi:link-variant",
        "unit": UnitOfLength.KILOMETERS,
        "min": 100,
        "max": 60_000,
        "step": 100,
        "default": 800,
        "mode": NumberMode.BOX,
        "entity_category": EntityCategory.CONFIG,
    },
    {
        "key": "drivetrain_day_interval",
        "name": "Drivetrain – day interval",
        "icon": "mdi:calendar-clock",
        "unit": "d",
        "min": 0,
        "max": 2_000,
        "step": 30,
        "default": 0,
        "mode": NumberMode.BOX,
        "entity_category": EntityCategory.CONFIG,
    },
    {
        "key": "drivetrain_alert_threshold",
        "name": "Drivetrain – alert threshold",
        "icon": "mdi:link-variant-remove",
        "unit": UnitOfLength.KILOMETERS,
        "min": 0,
        "max": 5_000,
        "step": 50,
        "default": 150,
        "mode": NumberMode.SLIDER,
        "entity_category": EntityCategory.CONFIG,
    },
    {
        "key": "drivetrain_km_at_last_service",
        "name": "Drivetrain – km at last service",
        "icon": "mdi:link-variant-plus",
        "unit": UnitOfLength.KILOMETERS,
        "min": 0,
        "max": 200_000,
        "step": 0.1,
        "default": 0,
        "mode": NumberMode.BOX,
        "entity_category": EntityCategory.CONFIG,
    },
    # ── Service maintenance ───────────────────────────────────────────────────
    {
        "key": "service_km_interval",
        "name": "Service – km interval",
        "icon": "mdi:wrench-clock",
        "unit": UnitOfLength.KILOMETERS,
        "min": 1_000,
        "max": 50_000,
        "step": 500,
        "default": 6_000,
        "mode": NumberMode.BOX,
        "entity_category": EntityCategory.CONFIG,
    },
    {
        "key": "service_day_interval",
        "name": "Service – day interval",
        "icon": "mdi:calendar-clock",
        "unit": "d",
        "min": 30,
        "max": 730,
        "step": 30,
        "default": 365,
        "mode": NumberMode.BOX,
        "entity_category": EntityCategory.CONFIG,
    },
    {
        "key": "service_alert_threshold",
        "name": "Service – alert threshold",
        "icon": "mdi:wrench-outline",
        "unit": UnitOfLength.KILOMETERS,
        "min": 0,
        "max": 2_000,
        "step": 100,
        "default": 500,
        "mode": NumberMode.SLIDER,
        "entity_category": EntityCategory.CONFIG,
    },
    {
        "key": "service_km_at_last_service",
        "name": "Service – km at last service",
        "icon": "mdi:wrench-check",
        "unit": UnitOfLength.KILOMETERS,
        "min": 0,
        "max": 200_000,
        "step": 0.1,
        "default": 0,
        "mode": NumberMode.BOX,
        "entity_category": EntityCategory.CONFIG,
    },
    # ── Oil change maintenance ────────────────────────────────────────────────
    {
        "key": "oil_change_km_interval",
        "name": "Oil change – km interval",
        "icon": "mdi:oil",
        "unit": UnitOfLength.KILOMETERS,
        "min": 1_000,
        "max": 50_000,
        "step": 500,
        "default": 6_000,
        "mode": NumberMode.BOX,
        "entity_category": EntityCategory.CONFIG,
    },
    {
        "key": "oil_change_alert_threshold",
        "name": "Oil change – alert threshold",
        "icon": "mdi:oil-level",
        "unit": UnitOfLength.KILOMETERS,
        "min": 0,
        "max": 2_000,
        "step": 100,
        "default": 500,
        "mode": NumberMode.SLIDER,
        "entity_category": EntityCategory.CONFIG,
    },
    {
        "key": "oil_change_km_at_last_oil_change",
        "name": "Oil change – km at last oil change",
        "icon": "mdi:oil-check",
        "unit": UnitOfLength.KILOMETERS,
        "min": 0,
        "max": 200_000,
        "step": 0.1,
        "default": 0,
        "mode": NumberMode.BOX,
        "entity_category": EntityCategory.CONFIG,
    },
    # ── Trips ─────────────────────────────────────────────────────────────────
    {
        "key": "trip_notification_threshold",
        "name": "Trip notification threshold",
        "icon": "mdi:map-marker-path",
        "unit": UnitOfLength.KILOMETERS,
        "min": 0,
        "max": 50,
        "step": 0.5,
        "default": 2,
        "mode": NumberMode.SLIDER,
        "entity_category": EntityCategory.CONFIG,
    },
]


def _drivetrain_overrides(profile: dict) -> dict:
    """Build per-key NUMBER_DESCRIPTIONS overrides from the active drivetrain profile.

    Adapts the drivetrain numbers' display label and default values to the
    selected drive_type (chain / shaft / belt). km_at_last keeps default 0.
    """
    label = profile["label"]
    base = {d["key"]: d for d in NUMBER_DESCRIPTIONS}
    return {
        "drivetrain_km_interval": {
            **base["drivetrain_km_interval"],
            "name": f"{label} – km interval",
            "default": profile["km_interval"],
        },
        "drivetrain_day_interval": {
            **base["drivetrain_day_interval"],
            "name": f"{label} – day interval",
            "default": profile["day_interval"],
        },
        "drivetrain_alert_threshold": {
            **base["drivetrain_alert_threshold"],
            "name": f"{label} – alert threshold",
            "default": profile["alert_threshold"],
        },
        "drivetrain_km_at_last_service": {
            **base["drivetrain_km_at_last_service"],
            "name": f"{label} – km at last service",
        },
    }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up GeoRide Trips number entities from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    trackers = data["trackers"]

    # Store shared by all number entities of this config entry.
    # Written to disk immediately on each set_value → survives restarts.
    store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}_{entry.entry_id}")
    stored_data: dict = await store.async_load() or {}

    profile = DRIVETRAIN_PROFILES.get(
        entry.options.get(CONF_DRIVE_TYPE, DEFAULT_DRIVE_TYPE),
        DRIVETRAIN_PROFILES["chain"],
    )
    overrides = _drivetrain_overrides(profile)

    entities = []
    for tracker in trackers:
        for desc in NUMBER_DESCRIPTIONS:
            desc = overrides.get(desc["key"], desc)
            entities.append(
                GeoRideNumberEntity(entry, tracker, desc, store, stored_data)
            )

    async_add_entities(entities)
    _LOGGER.info(
        "Added %d number entities for %d trackers",
        len(entities),
        len(trackers),
    )


class GeoRideNumberEntity(GeoRideEntityMixin, NumberEntity):
    """Persistent number entity attached to the GeoRide device.

    Uses homeassistant.helpers.storage.Store instead of RestoreEntity:
    each change is written to disk immediately (async_delay=0),
    which guarantees the values survive even on an abrupt restart.
    """

    def __init__(
        self,
        entry: ConfigEntry,
        tracker: dict,
        desc: dict,
        store: Store,
        stored_data: dict,
    ) -> None:
        self._entry = entry
        self._tracker = tracker
        self._desc = desc
        self._store = store

        self._tracker_id = str(tracker.get("trackerId"))
        self._tracker_name = tracker.get("trackerName", f"Tracker {self._tracker_id}")
        # Mixin-required public attributes
        self.tracker_id = self._tracker_id
        self.tracker_name = self._tracker_name

        self._attr_unique_id = f"{self._tracker_id}_{desc['key']}"
        self._attr_name = desc["name"]
        self._attr_icon = desc["icon"]
        self._attr_native_unit_of_measurement = desc.get(
            "unit", UnitOfLength.KILOMETERS
        )
        self._attr_mode = desc["mode"]
        self._attr_native_min_value = float(desc["min"])
        self._attr_native_max_value = float(desc["max"])
        self._attr_native_step = float(desc["step"])
        self._attr_entity_category = desc.get("entity_category")

        # Storage key unique per entity
        self._storage_key = f"{self._tracker_id}_{desc['key']}"

        # Restore from the Store (loaded before the entities are created)
        default = float(desc["default"])
        raw = stored_data.get(self._storage_key)
        try:
            self._attr_native_value = float(raw) if raw is not None else default
        except (ValueError, TypeError):
            self._attr_native_value = default

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()
        # Persist to disk immediately
        await self._persist(value)
        _LOGGER.debug("Set %s for %s: %s", self._desc["key"], self._tracker_name, value)

    async def _persist(self, value: float) -> None:
        """Write the value to the Store (disk) immediately."""
        try:
            current: dict = await self._store.async_load() or {}
            current[self._storage_key] = value
            await self._store.async_save(current)
        except Exception as err:
            _LOGGER.error(
                "Failed to persist %s for %s: %s",
                self._desc["key"],
                self._tracker_name,
                err,
            )
