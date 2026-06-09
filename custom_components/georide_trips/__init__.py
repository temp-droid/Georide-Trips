"""GeoRide Trips integration."""

import asyncio
import logging
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
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

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SWITCH,
    Platform.DATETIME,
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


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
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

    # Login + liste des trackers.
    # Auth refusée → échec définitif (mauvais identifiants, pas de retry).
    # Erreur transport/API → ConfigEntryNotReady pour que HA retente plus tard.
    try:
        await api.login()
        trackers = await api.get_trackers()
    except GeoRideAuthError as err:
        _LOGGER.error("Failed to login to GeoRide API: %s", err)
        return False
    except GeoRideApiError as err:
        raise ConfigEntryNotReady(f"GeoRide API unavailable: {err}") from err

    _LOGGER.info("Found %d GeoRide trackers", len(trackers))

    # Create coordinators
    from .sensor import (
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

    # Premier refresh trips + status en PARALLÈLE pour tous les trackers.
    # Le lifetime (historique complet, potentiellement des années) est différé
    # en tâche de fond plus bas pour ne pas bloquer le setup (timeout HA).
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
        # Échec partiel : on continue, le coordinator en échec retentera
        # selon son update_interval (entités indisponibles en attendant).
        for failure in failures:
            _LOGGER.warning(
                "Initial refresh failed (will retry on schedule): %s", failure
            )

    # Câbler la détection de verrouillage sur chaque coordinator récent
    # (via StatusCoordinator polling 5 min — indépendant du Socket.IO)
    for tracker in trackers:
        tracker_id = str(tracker.get("trackerId"))
        coordinators[tracker_id].attach_status_coordinator(
            tracker_status_coordinators[tracker_id]
        )
    _LOGGER.info(
        "TripsCoordinators attached to StatusCoordinator (lock detection active)"
    )

    # Créer le socket_manager AVANT le setup des plateformes
    # pour que les entités puissent s'y abonner dans async_added_to_hass
    socket_manager = None
    if socketio_enabled:
        from .socket_manager import GeoRideSocketManager

        tracker_ids = [str(t.get("trackerId")) for t in trackers]
        socket_manager = GeoRideSocketManager(hass, api, tracker_ids)
        _LOGGER.info("GeoRide Socket.IO manager created (will start after platforms)")

    # Store all data (socket_manager déjà disponible pour les entités)
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "api": api,
        "trackers": trackers,
        "email": entry.data[CONF_EMAIL],
        "coordinators": coordinators,
        "lifetime_coordinators": lifetime_coordinators,
        "tracker_status_coordinators": tracker_status_coordinators,
        "socket_manager": socket_manager,  # déjà prêt pour async_added_to_hass
    }

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
            name=f"{tracker_name} Trips",
            sw_version=str(tracker.get("softwareVersion", "")),
        )

    # Setup platforms — les entités s'abonneront au socket_manager dans async_added_to_hass
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Premier fetch lifetime DIFFÉRÉ : séquentiel (un tracker à la fois pour
    # ménager l'API), suivi par HA et annulé automatiquement à l'unload.
    # Les capteurs odometer restent "unknown" (jamais 0) tant que la base
    # lifetime n'est pas arrivée, grâce au gating _offset_ready.
    async def _initial_lifetime_refresh() -> None:
        for lifetime_coordinator in lifetime_coordinators.values():
            await lifetime_coordinator.async_refresh()

    entry.async_create_background_task(
        hass,
        _initial_lifetime_refresh(),
        name="georide_trips_initial_lifetime_refresh",
    )

    # Démarrer la connexion Socket.IO APRÈS le setup des plateformes
    # (les abonnements sont en place, on peut recevoir des événements)
    if socket_manager is not None:
        await socket_manager.start()
        _LOGGER.info("GeoRide Socket.IO manager started")
    else:
        _LOGGER.info("GeoRide Socket.IO disabled by option")

    # Register service set_odometer
    async def handle_set_odometer(call: ServiceCall):
        """Handle set_odometer service."""
        entity_id = call.data["entity_id"]
        value = call.data["value"]

        entity = hass.data["entity_components"]["sensor"].get_entity(entity_id)
        if entity and hasattr(entity, "set_odometer"):
            entity.set_odometer(value)
        else:
            _LOGGER.error(
                "Entity %s not found or doesn't support set_odometer", entity_id
            )

    hass.services.async_register(
        DOMAIN, SERVICE_SET_ODOMETER, handle_set_odometer, schema=SET_ODOMETER_SCHEMA
    )

    # Register service reset_odometer
    async def handle_reset_odometer(call: ServiceCall):
        """Handle reset_odometer service — set offset to 0."""
        entity_id = call.data["entity_id"]

        entity = hass.data["entity_components"]["sensor"].get_entity(entity_id)
        if entity and hasattr(entity, "set_odometer"):
            # Reset = set offset to 0, so odometer = tracker_km only
            from .sensor import GeoRideRealOdometerSensor

            if isinstance(entity, GeoRideRealOdometerSensor):
                base_km, delta_km, _ = entity._compute_tracker_km()
                entity.set_odometer(base_km + delta_km)
                _LOGGER.info("Odometer reset for %s (offset → 0)", entity_id)
            else:
                _LOGGER.error("Entity %s is not a GeoRideRealOdometerSensor", entity_id)
        else:
            _LOGGER.error("Entity %s not found or doesn't support reset", entity_id)

    hass.services.async_register(
        DOMAIN,
        SERVICE_RESET_ODOMETER,
        handle_reset_odometer,
        schema=RESET_ODOMETER_SCHEMA,
    )

    # Register service get_trips (supports_response => résultat visible dans Developer Tools)
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

        # Find API instance for this entry
        api_instance = None
        for entry_data in hass.data.get(DOMAIN, {}).values():
            if isinstance(entry_data, dict) and "api" in entry_data:
                api_instance = entry_data["api"]
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

        # Fire event (pour automations)
        hass.bus.async_fire(f"{DOMAIN}_trips_result", result)

        # Retourner le résultat => affiché dans Developer Tools > Services
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


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload entry when options change."""
    _LOGGER.info("Options changed, reloading GeoRide Trips")
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.info("Unloading GeoRide Trips")

    # Arrêter Socket.IO proprement avant le unload des plateformes
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    socket_manager = entry_data.get("socket_manager")
    if socket_manager:
        await socket_manager.stop()
        _LOGGER.info("GeoRide Socket.IO manager stopped")

    # Annuler les refreshs minuit des coordinators lifetime
    lifetime_coordinators = entry_data.get("lifetime_coordinators", {})
    for lifetime_coordinator in lifetime_coordinators.values():
        lifetime_coordinator.unschedule_midnight_refresh()

    # Désabonner les coordinators récents du StatusCoordinator (lock detection)
    coordinators = entry_data.get("coordinators", {})
    for coordinator in coordinators.values():
        coordinator.detach_status_coordinator()

    if len(hass.data.get(DOMAIN, {})) <= 1:
        hass.services.async_remove(DOMAIN, SERVICE_SET_ODOMETER)
        hass.services.async_remove(DOMAIN, SERVICE_RESET_ODOMETER)
        hass.services.async_remove(DOMAIN, SERVICE_GET_TRIPS)

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok
