"""GeoRide Trips binary_sensor entities.

Entities created per tracker:

── Fed by Socket.IO (real-time) + StatusCoordinator fallback ──
  - moving   : True if the motorcycle is moving
  - stolen   : True if the theft alarm is active
  - crashed  : True if a crash is detected
  - locked   : True if the tracker is UNLOCKED (HA LOCK convention)
               Socket.IO event "lock" + fallback polling 5 min
               ⚠ Inversion: GeoRide locked=True → is_on=False (locked)

── Fed by GeoRideTrackerStatusCoordinator (polling 5 min) ──
  - online   : True if the tracker is online (status == "online")

── Fed by real-time computation (state listeners) ──
  - refuel_needed     : range ≤ threshold
  - drivetrain_due  : remaining drivetrain km ≤ threshold OR (day_interval>0 AND days ≤ 30)
  - oil_change_due  : remaining oil-change km ≤ threshold
  - service_due : remaining service km ≤ threshold OR days ≤ 30
"""

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    CONF_DRIVE_TYPE,
    DEFAULT_DRIVE_TYPE,
    DRIVETRAIN_PROFILES,
)
from .helpers import GeoRideEntityMixin

_LOGGER = logging.getLogger(__name__)


SOCKET_BINARY_SENSOR_DESCRIPTIONS = [
    {
        "key": "moving",
        "name": "Moving",
        "device_class": BinarySensorDeviceClass.MOTION,
        "icon_on": "mdi:motorbike",
        "icon_off": "mdi:motorbike-off",
        "socket_events": ["position", "device"],
        "payload_key": "moving",
        # Key in the StatusCoordinator for the fallback polling
        "coordinator_fallback_key": "moving",
        # No inversion: moving=True → is_on=True
        "invert": False,
    },
    {
        "key": "stolen",
        "name": "Theft alarm",
        "device_class": BinarySensorDeviceClass.TAMPER,
        "icon_on": "mdi:shield-alert",
        "icon_off": "mdi:shield-check",
        "socket_events": ["device"],
        "payload_key": "stolen",
        "coordinator_fallback_key": None,
        "invert": False,
    },
    {
        "key": "crashed",
        "name": "Fall detected",
        "device_class": BinarySensorDeviceClass.PROBLEM,
        "icon_on": "mdi:alert-circle",
        "icon_off": "mdi:check-circle",
        "socket_events": ["device"],
        "payload_key": "crashed",
        "coordinator_fallback_key": None,
        "invert": False,
    },
    {
        "key": "locked",
        "name": "Locked",
        "device_class": BinarySensorDeviceClass.LOCK,
        "icon_on": "mdi:lock-open",
        "icon_off": "mdi:lock",
        "socket_events": ["lock", "device"],
        "payload_key": "locked",
        # The StatusCoordinator exposes "isLocked" (not "locked")
        "coordinator_fallback_key": "isLocked",
        # Inversion: BinarySensorDeviceClass.LOCK → is_on=True = unlocked
        # GeoRide sends locked=True when locked → is_on must be False
        "invert": True,
    },
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the binary_sensors for each tracker."""
    data = hass.data[DOMAIN][entry.entry_id]
    trackers = data["trackers"]
    tracker_status_coordinators = data["tracker_status_coordinators"]

    profile = DRIVETRAIN_PROFILES.get(
        entry.options.get(CONF_DRIVE_TYPE, DEFAULT_DRIVE_TYPE),
        DRIVETRAIN_PROFILES["chain"],
    )

    entities = []
    for tracker in trackers:
        tracker_id = str(tracker.get("trackerId"))
        status_coordinator = tracker_status_coordinators[tracker_id]

        # Socket.IO sensors (with optional coordinator fallback)
        for desc in SOCKET_BINARY_SENSOR_DESCRIPTIONS:
            coordinator_fallback = (
                status_coordinator if desc.get("coordinator_fallback_key") else None
            )
            entities.append(
                GeoRideBinarySensor(entry, tracker, desc, coordinator_fallback)
            )

        # Pure polling sensor: online (no dedicated Socket.IO event)
        entities.append(GeoRideOnlineBinarySensor(status_coordinator, entry, tracker))

        # Computed binary sensors: maintenance/fuel alert indicators
        entities.extend(
            [
                GeoRidePleinRequisBinarySensor(entry, tracker, hass),
                GeoRideVidangeRequiseBinarySensor(entry, tracker, hass),
                GeoRideRevisionRequiseBinarySensor(entry, tracker, hass),
            ]
        )

        # Drivetrain maintenance binary sensor — always created; label adapts
        # to the selected drive_type.
        entities.append(
            GeoRideDrivetrainRequiseBinarySensor(entry, tracker, hass, profile["label"])
        )

    async_add_entities(entities)
    _LOGGER.info(
        "Added %d binary_sensor entities for %d trackers",
        len(entities),
        len(trackers),
    )


# ════════════════════════════════════════════════════════════════════════════
# BINARY SENSORS SOCKET.IO (+ coordinator fallback)
# ════════════════════════════════════════════════════════════════════════════


class GeoRideBinarySensor(GeoRideEntityMixin, BinarySensorEntity, RestoreEntity):
    """GeoRide binary sensor fed by Socket.IO.

    Accepts an optional coordinator_fallback (GeoRideTrackerStatusCoordinator):
    on each coordinator polling, the state is synchronized to correct a
    stuck state (e.g. last Socket.IO event lost to a brief network outage).

    Supports value inversion via desc["invert"] for cases like LOCK
    where the HA convention is inverted relative to the GeoRide payload.
    """

    def __init__(
        self,
        entry: ConfigEntry,
        tracker: dict,
        desc: dict,
        coordinator_fallback=None,
    ) -> None:
        self._entry = entry
        self._tracker = tracker
        self._desc = desc
        self._socket_manager = None
        self._coordinator_fallback = coordinator_fallback

        self._tracker_id = str(tracker.get("trackerId"))
        self._tracker_name = tracker.get("trackerName", f"Tracker {self._tracker_id}")
        # Mixin-required public attributes
        self.tracker_id = self._tracker_id
        self.tracker_name = self._tracker_name

        self._attr_unique_id = f"{self._tracker_id}_{desc['key']}"
        self._attr_name = desc["name"]
        self._attr_device_class = desc["device_class"]
        self._attr_is_on = False

        # Inversion for LOCK: locked=True → is_on=False
        self._invert = desc.get("invert", False)
        # Key in the coordinator data (may differ from the Socket.IO payload)
        self._coordinator_fallback_key = desc.get("coordinator_fallback_key")

        self._unregister_callbacks: list = []
        self._unregister_coordinator: callable | None = None

    @property
    def icon(self) -> str:
        return self._desc["icon_on"] if self._attr_is_on else self._desc["icon_off"]

    async def async_added_to_hass(self) -> None:
        """Restore the state and subscribe to Socket.IO events."""
        await super().async_added_to_hass()

        if (last_state := await self.async_get_last_state()) is not None:
            if last_state.state not in (None, "unknown", "unavailable"):
                self._attr_is_on = last_state.state == "on"

        # Fetch the socket_manager from hass.data (available here, after full setup)
        entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        self._socket_manager = entry_data.get("socket_manager")

        if self._socket_manager:
            for event_name in self._desc["socket_events"]:
                unregister = self._socket_manager.register_callback(
                    self._tracker_id,
                    event_name,
                    self._handle_socket_event,
                )
                self._unregister_callbacks.append(unregister)

        # Subscribe to the status coordinator as a safety net
        if self._coordinator_fallback is not None:
            self._unregister_coordinator = (
                self._coordinator_fallback.async_add_listener(
                    self._handle_coordinator_update
                )
            )

    async def async_will_remove_from_hass(self) -> None:
        """Unregister from the Socket.IO callbacks and the coordinator."""
        for unregister in self._unregister_callbacks:
            unregister()
        self._unregister_callbacks.clear()

        if self._unregister_coordinator:
            self._unregister_coordinator()
            self._unregister_coordinator = None

    def _handle_coordinator_update(self) -> None:
        """Safety net: synchronize the state from the StatusCoordinator.

        Called on each coordinator polling (every 5 min).
        Synchronizes the binary sensor state with the coordinator value
        to correct a stuck state (e.g. lost Socket.IO event).

        For "moving": only acts if the coordinator says False and the sensor is ON
        (avoids overwriting a real movement confirmed by Socket.IO).

        For "locked": synchronizes both ways because the lock event
        can be lost in either direction (locking from the GeoRide app,
        side stand, etc.).
        """
        if not self._coordinator_fallback_key:
            return

        data = self._coordinator_fallback.data
        if not data:
            return

        coordinator_value = data.get(self._coordinator_fallback_key)
        if coordinator_value is None:
            return

        raw_state = bool(coordinator_value)
        # Apply the inversion if needed (LOCK: isLocked=True → is_on=False)
        new_state = (not raw_state) if self._invert else raw_state

        if self._desc["key"] == "moving":
            # Moving: only act to correct a stuck ON state
            # (the coordinator says stopped but Socket.IO never delivered moving=False)
            if not new_state and self._attr_is_on:
                self._attr_is_on = False
                self.async_write_ha_state()
                _LOGGER.debug(
                    "%s → OFF (coordinator fallback, Socket.IO had not delivered the final state)",
                    self._attr_name,
                )
        else:
            # For the others (locked, etc.): synchronize both ways
            if new_state != self._attr_is_on:
                self._attr_is_on = new_state
                self.async_write_ha_state()
                _LOGGER.debug(
                    "%s → %s (synced from coordinator fallback, key=%s)",
                    self._attr_name,
                    "ON" if new_state else "OFF",
                    self._coordinator_fallback_key,
                )

    async def _handle_socket_event(self, data: dict) -> None:
        """Process a Socket.IO event and update the state."""
        payload_key = self._desc["payload_key"]
        if payload_key not in data:
            _LOGGER.debug(
                "%s: payload_key '%s' missing from the event: %s",
                self._attr_name,
                payload_key,
                data,
            )
            return

        raw_state = bool(data[payload_key])
        # Apply the inversion if needed (LOCK: locked=True → is_on=False)
        new_state = (not raw_state) if self._invert else raw_state

        self._attr_is_on = new_state
        self.async_write_ha_state()
        _LOGGER.debug(
            "%s → %s (from Socket.IO event, raw %s=%s%s)",
            self._attr_name,
            "ON" if new_state else "OFF",
            payload_key,
            raw_state,
            " [inverted]" if self._invert else "",
        )


# ════════════════════════════════════════════════════════════════════════════
# BINARY SENSORS POLLING (GeoRideTrackerStatusCoordinator)
# ════════════════════════════════════════════════════════════════════════════


class GeoRideOnlineBinarySensor(
    GeoRideEntityMixin, CoordinatorEntity, BinarySensorEntity
):
    """Binary sensor: tracker online (status == 'online'), updated every 5 min."""

    def __init__(self, coordinator, entry: ConfigEntry, tracker: dict) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._tracker = tracker
        self._tracker_id = str(tracker.get("trackerId"))
        self._tracker_name = tracker.get("trackerName", f"Tracker {self._tracker_id}")
        # Mixin-required public attributes
        self.tracker_id = self._tracker_id
        self.tracker_name = self._tracker_name

        self._attr_unique_id = f"{self._tracker_id}_online"
        self._attr_name = "Online"
        self._attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def is_on(self) -> bool:
        data = self.coordinator.data
        if not data:
            return False
        return data.get("status") == "online"

    @property
    def icon(self) -> str:
        return "mdi:signal" if self.is_on else "mdi:signal-off"


# ════════════════════════════════════════════════════════════════════════════
# COMPUTED BINARY SENSORS — MAINTENANCE / FUEL ALERTS
# ════════════════════════════════════════════════════════════════════════════


class _GeoRideAlerteBinarySensorBase(GeoRideEntityMixin, BinarySensorEntity):
    """Base for the maintenance/fuel alert binary sensors.

    Computes its state in real time from the integration's sensors/numbers.
    Goes OFF→ON when the threshold is crossed, ON→OFF when the data becomes OK again
    (after a maintenance/refuel confirmation). No anti-duplicate logic needed:
    the blueprint uses a from='off' to='on' trigger to fire the notification.
    """

    def __init__(self, entry: ConfigEntry, tracker: dict, hass: HomeAssistant) -> None:
        self._entry = entry
        self._tracker = tracker
        self._hass = hass
        self._attr_is_on = False

        self.tracker_id = str(tracker.get("trackerId"))
        self.tracker_name = tracker.get("trackerName", f"Tracker {self.tracker_id}")

    def _watched_entities(self) -> list[str]:
        """Return the list of entities to watch for recomputation."""
        raise NotImplementedError

    def _compute_is_on(self) -> bool:
        """Compute whether the alert should be active."""
        raise NotImplementedError

    def _resolve_entities(self) -> None:
        """Resolve the entity_ids via the registry. Override in subclasses."""
        pass

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._resolve_entities()
        self.async_on_remove(
            async_track_state_change_event(
                self._hass,
                self._watched_entities(),
                self._handle_state_change,
            )
        )
        self._recalculate()

    @callback
    def _handle_state_change(self, event) -> None:
        self._recalculate()
        self.async_write_ha_state()

    def _recalculate(self) -> None:
        self._attr_is_on = self._compute_is_on()


class GeoRidePleinRequisBinarySensor(_GeoRideAlerteBinarySensorBase):
    """Binary sensor: refuel required (remaining_range ≤ fuel_range_alert_threshold).

    `binary_sensor.<moto>_plein_requis`
    Replaces switch.<moto>_faire_le_plein.
    """

    def __init__(self, entry, tracker, hass) -> None:
        super().__init__(entry, tracker, hass)
        # Entity_ids resolved in async_added_to_hass
        self._entity_autonomie: str | None = None
        self._entity_seuil: str | None = None
        self._attr_unique_id = f"{self.tracker_id}_refuel_needed"
        self._attr_name = "Refuel needed"
        self._attr_icon = "mdi:gas-station-alert"
        self._attr_device_class = BinarySensorDeviceClass.PROBLEM

    def _resolve_entities(self) -> None:
        from .helpers import resolve_entity_id

        self._entity_autonomie = resolve_entity_id(
            self._hass, "sensor", self.tracker_id, "remaining_range"
        )
        self._entity_seuil = resolve_entity_id(
            self._hass, "number", self.tracker_id, "fuel_range_alert_threshold"
        )

    def _watched_entities(self) -> list[str]:
        return [
            eid
            for eid in [self._entity_autonomie, self._entity_seuil]
            if eid is not None
        ]

    def _compute_is_on(self) -> bool:
        autonomie = self._get_float(self._entity_autonomie, -1.0)
        seuil = self._get_float(self._entity_seuil, 30.0)
        if autonomie < 0:
            return False
        return autonomie <= seuil


class GeoRideDrivetrainRequiseBinarySensor(_GeoRideAlerteBinarySensorBase):
    """Binary sensor: drivetrain maintenance required (chain / shaft / belt).

    `binary_sensor.<moto>_drivetrain_due`
    Dual criterion mirroring the service slot:
      remaining_km ≤ alert_threshold OR (day_interval > 0 AND remaining_days ≤ 30).
    When day_interval == 0 (chain/belt) only the km criterion applies.
    """

    def __init__(self, entry, tracker, hass, label="Drivetrain") -> None:
        super().__init__(entry, tracker, hass)
        self._entity_km_restants: str | None = None
        self._entity_jours_restants: str | None = None
        self._entity_seuil_km: str | None = None
        self._entity_intervalle_j: str | None = None
        self._attr_unique_id = f"{self.tracker_id}_drivetrain_due"
        self._attr_name = f"{label} – due"
        self._attr_icon = "mdi:link-variant-plus"
        self._attr_device_class = BinarySensorDeviceClass.PROBLEM

    def _resolve_entities(self) -> None:
        from .helpers import resolve_entity_id

        self._entity_km_restants = resolve_entity_id(
            self._hass, "sensor", self.tracker_id, "drivetrain_remaining_km"
        )
        self._entity_jours_restants = resolve_entity_id(
            self._hass, "sensor", self.tracker_id, "drivetrain_remaining_days"
        )
        self._entity_seuil_km = resolve_entity_id(
            self._hass, "number", self.tracker_id, "drivetrain_alert_threshold"
        )
        self._entity_intervalle_j = resolve_entity_id(
            self._hass, "number", self.tracker_id, "drivetrain_day_interval"
        )

    def _watched_entities(self) -> list[str]:
        return [
            eid
            for eid in [
                self._entity_km_restants,
                self._entity_jours_restants,
                self._entity_seuil_km,
                self._entity_intervalle_j,
            ]
            if eid is not None
        ]

    def _compute_is_on(self) -> bool:
        km_restants = self._get_float(self._entity_km_restants, 9999.0)
        jours_restants = self._get_float(self._entity_jours_restants, 9999.0)
        seuil_km = self._get_float(self._entity_seuil_km, 150.0)
        intervalle_j = self._get_float(self._entity_intervalle_j, 0.0)
        if km_restants == 9999.0 and jours_restants == 9999.0:
            return False
        alerte_km = km_restants <= seuil_km
        alerte_jours = intervalle_j > 0 and jours_restants <= 30
        return alerte_km or alerte_jours


class GeoRideVidangeRequiseBinarySensor(_GeoRideAlerteBinarySensorBase):
    """Binary sensor: oil change required (oil_change_remaining_km ≤ oil_change_alert_threshold).

    `binary_sensor.<moto>_vidange_requise`
    Replaces switch.<moto>_vidange_a_faire.
    """

    def __init__(self, entry, tracker, hass) -> None:
        super().__init__(entry, tracker, hass)
        self._entity_km_restants: str | None = None
        self._entity_seuil: str | None = None
        self._attr_unique_id = f"{self.tracker_id}_oil_change_due"
        self._attr_name = "Oil change – due"
        self._attr_icon = "mdi:oil-level"
        self._attr_device_class = BinarySensorDeviceClass.PROBLEM

    def _resolve_entities(self) -> None:
        from .helpers import resolve_entity_id

        self._entity_km_restants = resolve_entity_id(
            self._hass, "sensor", self.tracker_id, "oil_change_remaining_km"
        )
        self._entity_seuil = resolve_entity_id(
            self._hass, "number", self.tracker_id, "oil_change_alert_threshold"
        )

    def _watched_entities(self) -> list[str]:
        return [
            eid
            for eid in [self._entity_km_restants, self._entity_seuil]
            if eid is not None
        ]

    def _compute_is_on(self) -> bool:
        km_restants = self._get_float(self._entity_km_restants, 9999.0)
        seuil = self._get_float(self._entity_seuil, 500.0)
        if km_restants == 9999.0:
            return False
        return km_restants <= seuil


class GeoRideRevisionRequiseBinarySensor(_GeoRideAlerteBinarySensorBase):
    """Binary sensor: service required (km OR days ≤ threshold).

    `binary_sensor.<moto>_revision_requise`
    Replaces switch.<moto>_revision_a_faire.
    Dual criterion: km_restants ≤ seuil_km OR jours_restants ≤ 30.
    """

    def __init__(self, entry, tracker, hass) -> None:
        super().__init__(entry, tracker, hass)
        self._entity_km_restants: str | None = None
        self._entity_jours_restants: str | None = None
        self._entity_seuil_km: str | None = None
        self._attr_unique_id = f"{self.tracker_id}_service_due"
        self._attr_name = "Service – due"
        self._attr_icon = "mdi:wrench-clock"
        self._attr_device_class = BinarySensorDeviceClass.PROBLEM

    def _resolve_entities(self) -> None:
        from .helpers import resolve_entity_id

        self._entity_km_restants = resolve_entity_id(
            self._hass, "sensor", self.tracker_id, "service_remaining_km"
        )
        self._entity_jours_restants = resolve_entity_id(
            self._hass, "sensor", self.tracker_id, "service_remaining_days"
        )
        self._entity_seuil_km = resolve_entity_id(
            self._hass, "number", self.tracker_id, "service_alert_threshold"
        )

    def _watched_entities(self) -> list[str]:
        return [
            eid
            for eid in [
                self._entity_km_restants,
                self._entity_jours_restants,
                self._entity_seuil_km,
            ]
            if eid is not None
        ]

    def _compute_is_on(self) -> bool:
        km_restants = self._get_float(self._entity_km_restants, 9999.0)
        jours_restants = self._get_float(self._entity_jours_restants, 9999.0)
        seuil_km = self._get_float(self._entity_seuil_km, 500.0)
        if km_restants == 9999.0 and jours_restants == 9999.0:
            return False
        alerte_km = km_restants <= seuil_km
        alerte_jours = jours_restants <= 30
        return alerte_km or alerte_jours
