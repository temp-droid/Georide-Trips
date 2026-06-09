"""GeoRide Trips device tracker - Position GPS de la moto via Socket.IO.

Entité device_tracker rattachée au device de chaque moto :
- Position GPS en temps réel via événements Socket.IO (event "position")
- Fallback : récupération initiale via API REST au démarrage
- Pas de polling : les mises à jour arrivent dès que GeoRide envoie une position

Filtres appliqués sur les événements Socket.IO :
- Précision GPS (radius) : positions trop imprécises ignorées entièrement
- Statut moving=False : attributs mis à jour en mémoire, état HA NON écrit
  → pas d'entrée recorder quand la moto est à l'arrêt → plus de traits parasites
- Distance minimale : micro-dérives GPS ignorées (seuil configurable, défaut 10m)
"""

import logging
import math

from homeassistant.components.device_tracker import SourceType
from homeassistant.components.device_tracker.config_entry import TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import GeoRideTripsAPI
from .const import (
    DOMAIN,
    CONF_GPS_MIN_ACCURACY,
    DEFAULT_GPS_MIN_ACCURACY,
    CONF_GPS_MIN_DISTANCE,
    DEFAULT_GPS_MIN_DISTANCE,
)

_LOGGER = logging.getLogger(__name__)


def _haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calcule la distance en mètres entre deux coordonnées GPS (formule Haversine)."""
    R = 6_371_000  # rayon terrestre en mètres
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up GeoRide Trips device tracker from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    trackers = data["trackers"]
    api: GeoRideTripsAPI = data["api"]

    entities = []
    for tracker in trackers:
        entities.append(GeoRidePositionTracker(hass, entry, tracker, api))

    async_add_entities(entities)
    _LOGGER.info(
        "Added %d device_tracker entities for %d trackers", len(entities), len(trackers)
    )


class GeoRidePositionTracker(TrackerEntity):
    """Device tracker représentant la position GPS d'une moto GeoRide.

    Mises à jour via Socket.IO event "position" — temps réel, sans polling.
    Fallback initial via API REST pour avoir une position dès le démarrage.

    Filtres actifs (dans l'ordre d'application) :
    1. Précision GPS (radius > seuil) → ignoré entièrement
    2. moving=False → attributs mis à jour en mémoire, état HA NON écrit
    3. Distance < seuil_min → micro-dérive ignorée, état HA NON écrit
    4. Sinon → async_write_ha_state() → entrée recorder
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        tracker: dict,
        api: GeoRideTripsAPI,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._tracker = tracker
        self._api = api
        self._socket_manager = None

        self._tracker_id = str(tracker.get("trackerId"))
        self._tracker_name = tracker.get("trackerName", f"Tracker {self._tracker_id}")

        self._attr_unique_id = f"{self._tracker_id}_position"
        self._attr_name = f"{self._tracker_name} Position"
        self._attr_icon = "mdi:motorbike"

        # Position
        self._latitude: float | None = None
        self._longitude: float | None = None
        self._gps_accuracy: int = 0
        self._fix_time: str | None = None
        self._speed: float | None = None
        self._heading: float | None = None
        self._altitude: float | None = None
        self._is_moving: bool = False

        # Désenregistrement Socket.IO
        self._unsub_socket: list = []

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._tracker_id)},
            name=f"{self._tracker_name} Trips",
            manufacturer="GeoRide",
            model=self._tracker.get("model", "GeoRide Tracker"),
            sw_version=str(self._tracker.get("softwareVersion", "")),
        )

    # ── TrackerEntity properties ─────────────────────────────────────────────

    @property
    def latitude(self) -> float | None:
        return self._latitude

    @property
    def longitude(self) -> float | None:
        return self._longitude

    @property
    def source_type(self) -> SourceType:
        return SourceType.GPS

    @property
    def gps_accuracy(self) -> int:
        return self._gps_accuracy

    @property
    def extra_state_attributes(self) -> dict:
        attrs = {
            "tracker_id": self._tracker_id,
            "is_moving": self._is_moving,
            "source": "socket.io",
        }
        if self._fix_time:
            attrs["fix_time"] = self._fix_time
        if self._speed is not None:
            attrs["speed_kmh"] = round(self._speed, 1)  # déjà en km/h
        if self._heading is not None:
            attrs["heading"] = self._heading
        if self._altitude is not None:
            attrs["altitude"] = self._altitude
        return attrs

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def async_added_to_hass(self) -> None:
        """Démarrage : position initiale + abonnement Socket.IO."""
        await super().async_added_to_hass()

        # Récupérer le socket_manager depuis hass.data (disponible ici, après setup complet)
        entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        self._socket_manager = entry_data.get("socket_manager")

        # Abonnement aux événements position via Socket.IO
        if self._socket_manager:
            unsub = self._socket_manager.register_callback(
                self._tracker_id, "position", self._handle_position_event
            )
            self._unsub_socket.append(unsub)
            _LOGGER.info(
                "Device tracker '%s' abonné aux événements position Socket.IO",
                self._tracker_name,
            )
        else:
            _LOGGER.warning(
                "Pas de socket_manager disponible pour '%s' — pas de mises à jour temps réel",
                self._tracker_name,
            )

        # Position initiale via API REST en tâche de fond : get_last_position
        # enchaîne plusieurs appels API et ne doit pas bloquer l'ajout de
        # l'entité (un événement Socket.IO peut la fournir avant, tant mieux).
        self._entry.async_create_background_task(
            self.hass,
            self._async_fetch_initial_position(),
            name=f"georide_trips_initial_position_{self._tracker_id}",
        )

    async def async_will_remove_from_hass(self) -> None:
        """Nettoyage des abonnements."""
        for unsub in self._unsub_socket:
            unsub()
        self._unsub_socket.clear()

    # ── Handlers Socket.IO ───────────────────────────────────────────────────

    @callback
    def _handle_position_event(self, data: dict) -> None:
        """Reçoit un événement 'position' depuis Socket.IO et met à jour l'état.

        Logique de filtrage (dans l'ordre) :
        1. lat/lon manquants → ignoré
        2. Précision GPS insuffisante → ignoré entièrement
        3. moving=False → attributs mis à jour en mémoire, état HA NON écrit
           (pas d'entrée recorder → pas de traits parasites sur la carte)
        4. Distance < seuil_min → micro-dérive ignorée, état HA NON écrit
        5. Sinon → async_write_ha_state() → entrée recorder
        """
        _LOGGER.debug("Position Socket.IO pour %s : %s", self._tracker_name, data)

        lat = data.get("latitude")
        lon = data.get("longitude")
        if lat is None or lon is None:
            _LOGGER.warning(
                "Événement position sans lat/lon pour %s : %s", self._tracker_name, data
            )
            return

        # ── 1. Filtre précision GPS ──────────────────────────────────────────
        accuracy = int(data.get("radius", 0) or 0)
        min_accuracy = self._entry.options.get(
            CONF_GPS_MIN_ACCURACY, DEFAULT_GPS_MIN_ACCURACY
        )
        if min_accuracy > 0 and accuracy > min_accuracy:
            _LOGGER.debug(
                "Position ignorée pour %s : précision insuffisante (radius=%dm > seuil=%dm)",
                self._tracker_name,
                accuracy,
                min_accuracy,
            )
            return

        lat = float(lat)
        lon = float(lon)
        is_moving = bool(data.get("moving", False))

        # ── 2. Filtre moving=False ───────────────────────────────────────────
        # Les attributs en mémoire sont mis à jour (speed, heading, fix_time…)
        # mais async_write_ha_state() n'est PAS appelé → aucune entrée recorder
        # → pas de point sur la carte → pas de trait parasite entre sessions.
        if not is_moving:
            self._gps_accuracy = accuracy
            self._fix_time = data.get("fixtime") or data.get("fixTime")
            self._speed = data.get("speed")
            self._heading = data.get("heading")
            self._altitude = data.get("altitude")
            self._is_moving = False
            _LOGGER.debug(
                "Position non enregistrée pour %s : tracker à l'arrêt (moving=False)",
                self._tracker_name,
            )
            return

        # ── 3. Filtre distance minimale (anti micro-dérive) ──────────────────
        min_distance = self._entry.options.get(
            CONF_GPS_MIN_DISTANCE, DEFAULT_GPS_MIN_DISTANCE
        )
        if (
            min_distance > 0
            and self._latitude is not None
            and self._longitude is not None
        ):
            distance_m = _haversine_distance(self._latitude, self._longitude, lat, lon)
            if distance_m < min_distance:
                _LOGGER.debug(
                    "Position ignorée pour %s : déplacement trop faible (%.1fm < seuil=%dm)",
                    self._tracker_name,
                    distance_m,
                    min_distance,
                )
                return

        # ── 4. Mise à jour complète ──────────────────────────────────────────
        self._latitude = lat
        self._longitude = lon
        self._gps_accuracy = accuracy
        self._fix_time = data.get("fixtime") or data.get("fixTime")
        self._speed = data.get("speed")
        self._heading = data.get("heading")
        self._altitude = data.get("altitude")
        self._is_moving = True

        self.async_write_ha_state()
        _LOGGER.debug(
            "Position mise à jour pour %s : lat=%.5f lon=%.5f moving=True",
            self._tracker_name,
            self._latitude,
            self._longitude,
        )

    # ── Fallback API REST ────────────────────────────────────────────────────

    async def _async_fetch_initial_position(self) -> None:
        """Récupère la dernière position connue via API REST au démarrage."""
        try:
            position = await self._api.get_last_position(self._tracker_id)
            if position:
                accuracy = int(position.get("radius", 0) or 0)
                min_accuracy = self._entry.options.get(
                    CONF_GPS_MIN_ACCURACY, DEFAULT_GPS_MIN_ACCURACY
                )
                if min_accuracy > 0 and accuracy > min_accuracy:
                    _LOGGER.debug(
                        "Position initiale ignorée pour %s : précision insuffisante (radius=%dm > seuil=%dm)",
                        self._tracker_name,
                        accuracy,
                        min_accuracy,
                    )
                    return
                self._latitude = position.get("latitude")
                self._longitude = position.get("longitude")
                self._gps_accuracy = accuracy
                self._fix_time = position.get("fixtime") or position.get("fixTime")
                self._speed = position.get("speed")
                self._heading = position.get("heading")
                self._altitude = position.get("altitude")
                self.async_write_ha_state()
                _LOGGER.info(
                    "Position initiale (API) pour %s : lat=%s lon=%s",
                    self._tracker_name,
                    self._latitude,
                    self._longitude,
                )
            else:
                _LOGGER.debug(
                    "Pas de position initiale disponible pour %s", self._tracker_name
                )
        except Exception as err:
            _LOGGER.error(
                "Erreur récupération position initiale pour %s : %s",
                self._tracker_name,
                err,
            )
