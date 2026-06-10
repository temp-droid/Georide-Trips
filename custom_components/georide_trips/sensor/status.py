"""GeoRide status sensors — tracker status, batteries, last alarm."""

import logging

from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import UnitOfElectricPotential, EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..coordinator import GeoRideTrackerStatusCoordinator
from ..helpers import GeoRideEntityMixin

_LOGGER = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════
# SENSORS — TRACKER STATUS (fed by GeoRideTrackerStatusCoordinator)
# ════════════════════════════════════════════════════════════════════════════


class GeoRideTrackerStatusSensor(GeoRideEntityMixin, CoordinatorEntity, SensorEntity):
    """Sensor exposing the tracker's network status (online / offline)."""

    def __init__(self, coordinator: GeoRideTrackerStatusCoordinator, entry, tracker):
        super().__init__(coordinator)
        self.tracker_id = str(tracker.get("trackerId"))
        self.tracker_name = tracker.get("trackerName", f"Tracker {self.tracker_id}")
        self._entry = entry
        self._tracker = tracker
        self._attr_name = "Status"
        self._attr_unique_id = f"{self.tracker_id}_tracker_status"
        self._attr_icon = "mdi:signal"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> str | None:
        data = self.coordinator.data
        if not data:
            return None
        return data.get("status")

    @property
    def icon(self) -> str:
        if self.coordinator.data and self.coordinator.data.get("status") == "online":
            return "mdi:signal"
        return "mdi:signal-off"


class GeoRideExternalBatterySensor(GeoRideEntityMixin, CoordinatorEntity, SensorEntity):
    """Sensor for the external battery voltage (GeoRide 3 only)."""

    def __init__(self, coordinator: GeoRideTrackerStatusCoordinator, entry, tracker):
        super().__init__(coordinator)
        self.tracker_id = str(tracker.get("trackerId"))
        self.tracker_name = tracker.get("trackerName", f"Tracker {self.tracker_id}")
        self._entry = entry
        self._tracker = tracker
        self._attr_name = "External battery"
        self._attr_unique_id = f"{self.tracker_id}_external_battery"
        self._attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT
        self._attr_device_class = SensorDeviceClass.VOLTAGE
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_icon = "mdi:battery-charging"
        self._attr_suggested_display_precision = 2

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data
        if not data:
            return None
        voltage = data.get("externalBatteryVoltage")
        if voltage is None:
            return None
        try:
            return round(float(voltage), 2)
        except (ValueError, TypeError):
            return None

    @property
    def available(self) -> bool:
        """Available only if the tracker returns this value (GeoRide 3)."""
        if not self.coordinator.last_update_success:
            return False
        data = self.coordinator.data
        return bool(data and data.get("externalBatteryVoltage") is not None)


class GeoRideInternalBatterySensor(GeoRideEntityMixin, CoordinatorEntity, SensorEntity):
    """Sensor for the internal battery voltage (GeoRide 3 only)."""

    def __init__(self, coordinator: GeoRideTrackerStatusCoordinator, entry, tracker):
        super().__init__(coordinator)
        self.tracker_id = str(tracker.get("trackerId"))
        self.tracker_name = tracker.get("trackerName", f"Tracker {self.tracker_id}")
        self._entry = entry
        self._tracker = tracker
        self._attr_name = "Internal battery"
        self._attr_unique_id = f"{self.tracker_id}_internal_battery"
        self._attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT
        self._attr_device_class = SensorDeviceClass.VOLTAGE
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_icon = "mdi:battery"
        self._attr_suggested_display_precision = 2

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data
        if not data:
            return None
        voltage = data.get("internalBatteryVoltage")
        if voltage is None:
            return None
        try:
            return round(float(voltage), 2)
        except (ValueError, TypeError):
            return None

    @property
    def available(self) -> bool:
        """Available only if the tracker returns this value (GeoRide 3)."""
        if not self.coordinator.last_update_success:
            return False
        data = self.coordinator.data
        return bool(data and data.get("internalBatteryVoltage") is not None)


# ════════════════════════════════════════════════════════════════════════════
# SENSOR — LAST ALARM (fed by Socket.IO)
# ════════════════════════════════════════════════════════════════════════════


class GeoRideLastAlarmSensor(GeoRideEntityMixin, RestoreEntity, SensorEntity):
    """Sensor exposing the type of the last alarm received via Socket.IO."""

    def __init__(self, entry, tracker):
        self.tracker_id = str(tracker.get("trackerId"))
        self.tracker_name = tracker.get("trackerName", f"Tracker {self.tracker_id}")
        self._entry = entry
        self._tracker = tracker
        self._attr_name = "Last alarm"
        self._attr_unique_id = f"{self.tracker_id}_last_alarm"
        self._attr_icon = "mdi:alarm-light"
        self._attr_entity_category = None
        self._state: str | None = None
        self._alarm_timestamp: str | None = None
        self._device_name: str | None = None
        self._unregister_alarm: callable | None = None

    @property
    def native_value(self) -> str | None:
        return self._state

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "timestamp": self._alarm_timestamp,
            "device_name": self._device_name,
            "tracker_id": self.tracker_id,
        }

    async def async_added_to_hass(self) -> None:
        """Restore the state and register with the socket_manager."""
        await super().async_added_to_hass()

        # Restoration from the recorder
        last_state = await self.async_get_last_state()
        if last_state and last_state.state not in ("unknown", "unavailable"):
            self._state = last_state.state
            self._alarm_timestamp = last_state.attributes.get("timestamp")
            self._device_name = last_state.attributes.get("device_name")

        # Socket.IO callback registration
        socket_manager = self._entry.runtime_data.socket_manager
        if socket_manager:
            self._unregister_alarm = socket_manager.register_callback(
                self.tracker_id, "alarm", self._handle_alarm
            )
            _LOGGER.debug(
                "LastAlarmSensor %s registered with socket_manager", self.tracker_id
            )
        else:
            _LOGGER.debug(
                "LastAlarmSensor %s: no socket_manager available at startup "
                "(normal if Socket.IO starts after the entities)",
                self.tracker_id,
            )

    async def async_will_remove_from_hass(self) -> None:
        """Unregister from the socket_manager."""
        if self._unregister_alarm:
            self._unregister_alarm()
            self._unregister_alarm = None

    def _handle_alarm(self, data: dict) -> None:
        """Callback called by socket_manager on an alarm."""
        # GeoRide sends the type in 'name' (e.g. "sonorAlarmOn"),
        # fallback to 'alarmType' or 'type' for compatibility.
        alarm_type = data.get("name") or data.get("alarmType") or data.get("type")
        if not alarm_type:
            return

        self._state = alarm_type
        self._alarm_timestamp = data.get("timestamp") or data.get("date")
        self._device_name = data.get("device_name") or self.tracker_name

        self.schedule_update_ha_state()
        _LOGGER.info(
            "LastAlarmSensor %s: new alarm %s",
            self.tracker_id,
            alarm_type,
        )
