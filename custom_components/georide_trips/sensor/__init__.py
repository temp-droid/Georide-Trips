"""GeoRide Trips sensors - SIMPLE COMPLETE VERSION."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from ..const import (
    DOMAIN,
    CONF_DRIVE_TYPE,
    DEFAULT_DRIVE_TYPE,
    DRIVETRAIN_PROFILES,
)

_LOGGER = logging.getLogger(__name__)

# Conversion constants
MILLISECONDS_TO_MINUTES = 60000
MILLISECONDS_TO_HOURS = 3600000

# Re-export the coordinators from their dedicated module for backwards
# compatibility with any external import of `.sensor`.
from ..coordinator import (  # noqa: E402
    GeoRideTripsCoordinator,
    GeoRideLifetimeTripsCoordinator,
    GeoRideTrackerStatusCoordinator,
)
from ..snapshot import GeoRideMidnightSnapshotManager  # noqa: E402

from .trips import (  # noqa: E402
    GeoRideLastTripSensor,
    GeoRideLastTripDetailsSensor,
    GeoRideTotalDistanceSensor,
    GeoRideTripCountSensor,
)
from .mileage import (  # noqa: E402
    _GeoRideKmPeriodBase,
    GeoRideKmJournaliersSensor,
    GeoRideKmHebdomadairesSensor,
    GeoRideKmMensuelsSensor,
)
from .odometer import (  # noqa: E402
    GeoRideLifetimeOdometerSensor,
    GeoRideRealOdometerSensor,
)
from .fuel import GeoRideAutonomySensor  # noqa: E402
from .maintenance import (  # noqa: E402
    _GeoRideEntretienKmBase,
    GeoRideKmRestantsDrivetrainSensor,
    GeoRideKmRestantsVidangeSensor,
    GeoRideKmRestantsRevisionSensor,
    GeoRideJoursRestantsRevisionSensor,
    GeoRideJoursRestantsDrivetrainSensor,
)
from .status import (  # noqa: E402
    GeoRideTrackerStatusSensor,
    GeoRideExternalBatterySensor,
    GeoRideInternalBatterySensor,
    GeoRideLastAlarmSensor,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up GeoRide Trips sensors from a config entry."""
    _LOGGER.info("Setting up GeoRide Trips sensors from config entry")

    data = hass.data[DOMAIN][entry.entry_id]
    trackers = data["trackers"]
    coordinators = data["coordinators"]
    lifetime_coordinators = data["lifetime_coordinators"]
    tracker_status_coordinators = data["tracker_status_coordinators"]

    profile = DRIVETRAIN_PROFILES.get(
        entry.options.get(CONF_DRIVE_TYPE, DEFAULT_DRIVE_TYPE),
        DRIVETRAIN_PROFILES["chain"],
    )

    sensors = []
    for tracker in trackers:
        tracker_id = str(tracker.get("trackerId"))
        coordinator = coordinators[tracker_id]
        lifetime_coordinator = lifetime_coordinators[tracker_id]
        status_coordinator = tracker_status_coordinators[tracker_id]

        # Schedule the lifetime coordinator's midnight refresh
        lifetime_coordinator.schedule_midnight_refresh()

        # As soon as a new trip is detected → immediate refresh of the lifetime coordinator
        def _on_new_trip(lc=lifetime_coordinator):
            hass.async_create_task(lc.async_request_refresh())

        unregister_new_trip = coordinator.on_new_trip(_on_new_trip)
        entry.async_on_unload(unregister_new_trip)

        odometer_sensor = GeoRideRealOdometerSensor(
            lifetime_coordinator, coordinator, status_coordinator, entry, tracker, hass
        )
        autonomy_sensor = GeoRideAutonomySensor(entry, tracker, hass, odometer_sensor)

        # Midnight snapshots manager — replaces the blueprint's 'midnight' trigger
        midnight_manager = GeoRideMidnightSnapshotManager(
            hass, entry, tracker, odometer_sensor
        )
        midnight_manager.setup()
        entry.async_on_unload(midnight_manager.unschedule)

        sensors.extend(
            [
                GeoRideLastTripSensor(coordinator, entry, tracker),
                GeoRideLastTripDetailsSensor(coordinator, entry, tracker),
                GeoRideTotalDistanceSensor(coordinator, entry, tracker),
                GeoRideTripCountSensor(coordinator, entry, tracker),
                GeoRideLifetimeOdometerSensor(lifetime_coordinator, entry, tracker),
                # RealOdometer listens to both coordinators: lifetime (solid base)
                # + recent coordinator (new intraday trips)
                odometer_sensor,
                # Remaining range sensor (reactive on odometer + fuel entities)
                autonomy_sensor,
                # Periodic km sensors — computed in Python, reactive on odometer + snapshot
                GeoRideKmJournaliersSensor(entry, tracker, hass, odometer_sensor),
                GeoRideKmHebdomadairesSensor(entry, tracker, hass, odometer_sensor),
                GeoRideKmMensuelsSensor(entry, tracker, hass, odometer_sensor),
                # Maintenance sensors — remaining km and remaining days computed in Python
                GeoRideKmRestantsVidangeSensor(entry, tracker, hass, odometer_sensor),
                GeoRideKmRestantsRevisionSensor(entry, tracker, hass, odometer_sensor),
                GeoRideJoursRestantsRevisionSensor(entry, tracker, hass),
                # Sensors fed by the status coordinator (/user/trackers data)
                GeoRideTrackerStatusSensor(status_coordinator, entry, tracker),
                GeoRideExternalBatterySensor(status_coordinator, entry, tracker),
                GeoRideInternalBatterySensor(status_coordinator, entry, tracker),
                # Last alarm sensor (fed by Socket.IO)
                GeoRideLastAlarmSensor(entry, tracker),
            ]
        )

        # Drivetrain maintenance sensors — always created; label adapts to the
        # selected drive_type. Time dimension only matters when day_interval>0.
        sensors.append(
            GeoRideKmRestantsDrivetrainSensor(
                entry, tracker, hass, odometer_sensor, profile["label"]
            )
        )
        sensors.append(
            GeoRideJoursRestantsDrivetrainSensor(entry, tracker, hass, profile["label"])
        )

    async_add_entities(sensors)
    _LOGGER.info("Added %d sensors for %d trackers", len(sensors), len(trackers))


__all__ = [
    "async_setup_entry",
    "MILLISECONDS_TO_MINUTES",
    "MILLISECONDS_TO_HOURS",
    "GeoRideTripsCoordinator",
    "GeoRideLifetimeTripsCoordinator",
    "GeoRideTrackerStatusCoordinator",
    "GeoRideMidnightSnapshotManager",
    "GeoRideLastTripSensor",
    "GeoRideLastTripDetailsSensor",
    "GeoRideTotalDistanceSensor",
    "GeoRideTripCountSensor",
    "_GeoRideKmPeriodBase",
    "GeoRideKmJournaliersSensor",
    "GeoRideKmHebdomadairesSensor",
    "GeoRideKmMensuelsSensor",
    "GeoRideLifetimeOdometerSensor",
    "GeoRideRealOdometerSensor",
    "GeoRideAutonomySensor",
    "_GeoRideEntretienKmBase",
    "GeoRideKmRestantsDrivetrainSensor",
    "GeoRideKmRestantsVidangeSensor",
    "GeoRideKmRestantsRevisionSensor",
    "GeoRideJoursRestantsRevisionSensor",
    "GeoRideJoursRestantsDrivetrainSensor",
    "GeoRideTrackerStatusSensor",
    "GeoRideExternalBatterySensor",
    "GeoRideInternalBatterySensor",
    "GeoRideLastAlarmSensor",
]
