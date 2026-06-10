"""GeoRide Trips integration."""

import asyncio
import logging
import voluptuous as vol

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import device_registry as dr
import homeassistant.helpers.config_validation as cv

from .const import (
    DOMAIN,
    CONF_SCAN_INTERVAL,
    CONF_LIFETIME_SCAN_INTERVAL,
    CONF_TRIPS_DAYS_BACK,
    CONF_SOCKETIO_ENABLED,
    CONF_TRACKER_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_LIFETIME_SCAN_INTERVAL,
    DEFAULT_TRIPS_DAYS_BACK,
    DEFAULT_SOCKETIO_ENABLED,
    DEFAULT_TRACKER_SCAN_INTERVAL,
)
from .api import GeoRideApiError, GeoRideAuthError, GeoRideTripsAPI
from .data import GeoRideConfigEntry, GeoRideData

_LOGGER = logging.getLogger(__name__)

# Dependency platforms (number, datetime) are listed first so their entities are
# registered before the sensor/button/binary_sensor entities that resolve them by
# unique_id — avoids first-boot "entity_ids not resolved" warnings after a fresh
# install or a unique_id change.
PLATFORMS = [
    Platform.NUMBER,
    Platform.DATETIME,
    Platform.SENSOR,
    Platform.BUTTON,
    Platform.SWITCH,
    Platform.BINARY_SENSOR,
    Platform.DEVICE_TRACKER,
]

SERVICE_SET_ODOMETER = "set_odometer"
SERVICE_GET_TRIPS = "get_trips"
SERVICE_RESET_ODOMETER = "reset_odometer"

SET_ODOMETER_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): cv.entity_id,
        vol.Required("value"): vol.Coerce(float),
    }
)

GET_TRIPS_SCHEMA = vol.Schema(
    {
        vol.Required("tracker_id"): cv.string,
        vol.Optional("from_date"): cv.string,
        vol.Optional("to_date"): cv.string,
    }
)

RESET_ODOMETER_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): cv.entity_id,
    }
)


async def async_setup(hass: HomeAssistant, config: dict):
    """Set up the GeoRide Trips component from YAML (legacy)."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: GeoRideConfigEntry) -> bool:
    """Set up GeoRide Trips from a config entry."""
    _LOGGER.info("Setting up GeoRide Trips for %s", entry.data[CONF_EMAIL])

    # Read options
    scan_interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    lifetime_scan_interval = entry.options.get(
        CONF_LIFETIME_SCAN_INTERVAL, DEFAULT_LIFETIME_SCAN_INTERVAL
    )
    trips_days_back = entry.options.get(CONF_TRIPS_DAYS_BACK, DEFAULT_TRIPS_DAYS_BACK)
    socketio_enabled = entry.options.get(
        CONF_SOCKETIO_ENABLED, DEFAULT_SOCKETIO_ENABLED
    )
    tracker_scan_interval = entry.options.get(
        CONF_TRACKER_SCAN_INTERVAL, DEFAULT_TRACKER_SCAN_INTERVAL
    )

    _LOGGER.info(
        "Options: scan_interval=%ss, lifetime=%ss, days_back=%s, socketio=%s, tracker_scan=%ss",
        scan_interval,
        lifetime_scan_interval,
        trips_days_back,
        socketio_enabled,
        tracker_scan_interval,
    )

    # Create API client
    session = async_get_clientsession(hass)
    api = GeoRideTripsAPI(entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD], session)

    # Login + list of trackers.
    # Auth rejected → ConfigEntryAuthFailed so HA starts a reauth flow.
    # Transport/API error → ConfigEntryNotReady so HA retries later.
    try:
        await api.login()
        trackers = await api.get_trackers()
    except GeoRideAuthError as err:
        raise ConfigEntryAuthFailed(f"GeoRide credentials rejected: {err}") from err
    except GeoRideApiError as err:
        raise ConfigEntryNotReady(f"GeoRide API unavailable: {err}") from err

    _LOGGER.info("Found %d GeoRide trackers", len(trackers))

    # Create coordinators
    from .coordinator import (
        GeoRideTripsCoordinator,
        GeoRideLifetimeTripsCoordinator,
        GeoRideTrackerStatusCoordinator,
    )

    coordinators = {}
    lifetime_coordinators = {}
    tracker_status_coordinators = {}

    for tracker in trackers:
        tracker_id = str(tracker.get("trackerId"))
        tracker_name = tracker.get("trackerName", f"Tracker {tracker_id}")

        coordinator = GeoRideTripsCoordinator(
            hass,
            api,
            tracker_id,
            tracker_name,
            scan_interval,
            trips_days_back,
        )

        lifetime_coordinator = GeoRideLifetimeTripsCoordinator(
            hass,
            api,
            tracker_id,
            tracker_name,
            tracker.get("activationDate"),
            lifetime_scan_interval,
        )

        status_coordinator = GeoRideTrackerStatusCoordinator(
            hass,
            api,
            tracker_id,
            tracker_name,
            scan_interval=tracker_scan_interval,
        )

        coordinators[tracker_id] = coordinator
        lifetime_coordinators[tracker_id] = lifetime_coordinator
        tracker_status_coordinators[tracker_id] = status_coordinator

    # First trips + status refresh in PARALLEL for all trackers.
    # The lifetime (full history, potentially years) is deferred to a
    # background task below so it doesn't block setup (HA timeout).
    first_refreshes = [
        coro
        for tracker_id in coordinators
        for coro in (
            coordinators[tracker_id].async_config_entry_first_refresh(),
            tracker_status_coordinators[tracker_id].async_config_entry_first_refresh(),
        )
    ]
    results = await asyncio.gather(*first_refreshes, return_exceptions=True)
    failures = [r for r in results if isinstance(r, Exception)]
    if failures:
        if len(failures) == len(first_refreshes):
            raise ConfigEntryNotReady(
                f"All initial refreshes failed: {failures[0]}"
            ) from failures[0]
        # Partial failure: we continue, the failed coordinator will retry
        # on its update_interval (entities unavailable in the meantime).
        for failure in failures:
            _LOGGER.warning(
                "Initial refresh failed (will retry on schedule): %s", failure
            )

    # Wire lock detection onto each recent coordinator
    # (via StatusCoordinator polling every 5 min — independent of Socket.IO)
    for tracker in trackers:
        tracker_id = str(tracker.get("trackerId"))
        coordinators[tracker_id].attach_status_coordinator(
            tracker_status_coordinators[tracker_id]
        )
    _LOGGER.info(
        "TripsCoordinators attached to StatusCoordinator (lock detection active)"
    )

    # Create the socket_manager BEFORE setting up the platforms
    # so entities can subscribe to it in async_added_to_hass
    socket_manager = None
    if socketio_enabled:
        from .socket_manager import GeoRideSocketManager

        tracker_ids = [str(t.get("trackerId")) for t in trackers]
        socket_manager = GeoRideSocketManager(hass, api, tracker_ids)
        _LOGGER.info("GeoRide Socket.IO manager created (will start after platforms)")

    # Store all data (socket_manager already available for entities)
    entry.runtime_data = GeoRideData(
        api=api,
        trackers=trackers,
        email=entry.data[CONF_EMAIL],
        coordinators=coordinators,
        lifetime_coordinators=lifetime_coordinators,
        tracker_status_coordinators=tracker_status_coordinators,
        socket_manager=socket_manager,  # already ready for async_added_to_hass
    )

    # Register devices
    device_registry = dr.async_get(hass)
    for tracker in trackers:
        tracker_id = str(tracker.get("trackerId"))
        tracker_name = tracker.get("trackerName", f"Tracker {tracker_id}")

        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, tracker_id)},
            manufacturer="GeoRide",
            model=tracker.get("model", "GeoRide Tracker"),
            name=tracker_name,
            sw_version=str(tracker.get("softwareVersion", "")),
        )

    # Setup platforms — entities will subscribe to the socket_manager in async_added_to_hass
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # First lifetime fetch DEFERRED: sequential (one tracker at a time to
    # spare the API), tracked by HA and automatically cancelled on unload.
    # The odometer sensors stay "unknown" (never 0) until the lifetime
    # baseline has arrived, thanks to the _offset_ready gating.
    async def _initial_lifetime_refresh() -> None:
        for lifetime_coordinator in lifetime_coordinators.values():
            await lifetime_coordinator.async_refresh()

    entry.async_create_background_task(
        hass,
        _initial_lifetime_refresh(),
        name="georide_trips_initial_lifetime_refresh",
    )

    # Start the Socket.IO connection AFTER setting up the platforms
    # (subscriptions are in place, we can receive events)
    if socket_manager is not None:
        await socket_manager.start()
        _LOGGER.info("GeoRide Socket.IO manager started")
    else:
        _LOGGER.info("GeoRide Socket.IO disabled by option")

    # Odometer services — implemented via the entity registry and the
    # odometer_offset number only (never via HA's internal entity objects,
    # which are not a supported API).
    def _resolve_offset_entity(entity_id: str) -> str:
        """From the real_odometer sensor entity_id, find the offset number.

        Raises HomeAssistantError if the entity is not a GeoRide odometer
        or if the odometer_offset number cannot be found.
        """
        from homeassistant.helpers import entity_registry as er

        from .helpers import resolve_entity_id

        registry = er.async_get(hass)
        reg_entry = registry.async_get(entity_id)
        if (
            reg_entry is None
            or reg_entry.platform != DOMAIN
            or not reg_entry.unique_id.endswith("_real_odometer")
        ):
            raise HomeAssistantError(
                f"{entity_id} is not a GeoRide Trips real odometer sensor"
            )
        tracker_id = reg_entry.unique_id.removesuffix("_real_odometer")
        offset_entity_id = resolve_entity_id(
            hass, "number", tracker_id, "odometer_offset"
        )
        if offset_entity_id is None:
            raise HomeAssistantError(
                f"odometer_offset number not found for tracker {tracker_id}"
            )
        return offset_entity_id

    async def handle_set_odometer(call: ServiceCall):
        """Set the displayed odometer to `value` by adjusting the offset.

        new_offset = value - (displayed_state - current_offset)
        """
        entity_id = call.data["entity_id"]
        value = call.data["value"]
        offset_entity_id = _resolve_offset_entity(entity_id)

        state = hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            raise HomeAssistantError(
                f"{entity_id} has no value yet (lifetime data still loading?) "
                "— retry once the odometer shows a value"
            )
        offset_state = hass.states.get(offset_entity_id)
        current_offset = (
            float(offset_state.state)
            if offset_state
            and offset_state.state not in (None, "unknown", "unavailable")
            else 0.0
        )
        tracker_km = float(state.state) - current_offset
        new_offset = round(value - tracker_km, 2)

        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": offset_entity_id, "value": new_offset},
            blocking=True,
        )
        _LOGGER.info(
            "Odometer set for %s: %.1f km (offset %.1f → %.1f)",
            entity_id,
            value,
            current_offset,
            new_offset,
        )

    hass.services.async_register(
        DOMAIN, SERVICE_SET_ODOMETER, handle_set_odometer, schema=SET_ODOMETER_SCHEMA
    )

    async def handle_reset_odometer(call: ServiceCall):
        """Handle reset_odometer service — offset to 0, odometer = tracker_km alone."""
        entity_id = call.data["entity_id"]
        offset_entity_id = _resolve_offset_entity(entity_id)

        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": offset_entity_id, "value": 0.0},
            blocking=True,
        )
        _LOGGER.info("Odometer reset for %s (offset → 0)", entity_id)

    hass.services.async_register(
        DOMAIN,
        SERVICE_RESET_ODOMETER,
        handle_reset_odometer,
        schema=RESET_ODOMETER_SCHEMA,
    )

    # Register service get_trips (supports_response => result visible in Developer Tools)
    async def handle_get_trips(call: ServiceCall):
        """Handle get_trips service call."""
        from datetime import datetime as dt
        from homeassistant.core import SupportsResponse  # noqa: F401 (used below)

        tracker_id = call.data["tracker_id"]
        from_date_str = call.data.get("from_date")
        to_date_str = call.data.get("to_date")

        from_date = None
        to_date = None

        if from_date_str:
            try:
                from_date = dt.fromisoformat(from_date_str)
            except ValueError:
                _LOGGER.error(
                    "Invalid from_date format: %s (expected ISO 8601)", from_date_str
                )
                return {"error": f"Invalid from_date format: {from_date_str}"}

        if to_date_str:
            try:
                to_date = dt.fromisoformat(to_date_str)
            except ValueError:
                _LOGGER.error(
                    "Invalid to_date format: %s (expected ISO 8601)", to_date_str
                )
                return {"error": f"Invalid to_date format: {to_date_str}"}

        # Find API instance for any loaded entry
        api_instance = None
        for loaded_entry in hass.config_entries.async_loaded_entries(DOMAIN):
            api_instance = loaded_entry.runtime_data.api
            break

        if not api_instance:
            _LOGGER.error("GeoRide API not found for get_trips service")
            return {"error": "GeoRide API not found"}

        try:
            trips = await api_instance.get_trips(tracker_id, from_date, to_date)
        except GeoRideApiError as err:
            _LOGGER.error("get_trips service failed: %s", err)
            return {"error": str(err)}

        _LOGGER.info(
            "get_trips: tracker=%s from=%s to=%s => %d trips found",
            tracker_id,
            from_date_str,
            to_date_str,
            len(trips),
        )

        result = {
            "tracker_id": tracker_id,
            "from_date": from_date_str,
            "to_date": to_date_str,
            "trip_count": len(trips),
            "trips": trips,
        }

        # Fire event (for automations)
        hass.bus.async_fire(f"{DOMAIN}_trips_result", result)

        # Return the result => displayed in Developer Tools > Services
        return result

    from homeassistant.core import SupportsResponse

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_TRIPS,
        handle_get_trips,
        schema=GET_TRIPS_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    # Reload on options change
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


async def async_reload_entry(hass: HomeAssistant, entry: GeoRideConfigEntry) -> None:
    """Reload entry when options change."""
    _LOGGER.info("Options changed, reloading GeoRide Trips")
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: GeoRideConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.info("Unloading GeoRide Trips")

    data = entry.runtime_data

    # Stop Socket.IO cleanly before unloading the platforms
    if data.socket_manager:
        await data.socket_manager.stop()
        _LOGGER.info("GeoRide Socket.IO manager stopped")

    # Cancel the midnight refreshes of the lifetime coordinators
    for lifetime_coordinator in data.lifetime_coordinators.values():
        lifetime_coordinator.unschedule_midnight_refresh()

    # Unsubscribe the recent coordinators from the StatusCoordinator (lock detection)
    for coordinator in data.coordinators.values():
        coordinator.detach_status_coordinator()

    # Remove shared services when the last entry is being unloaded
    if len(hass.config_entries.async_loaded_entries(DOMAIN)) <= 1:
        hass.services.async_remove(DOMAIN, SERVICE_SET_ODOMETER)
        hass.services.async_remove(DOMAIN, SERVICE_RESET_ODOMETER)
        hass.services.async_remove(DOMAIN, SERVICE_GET_TRIPS)

    # runtime_data is cleared automatically by HA on unload.
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
