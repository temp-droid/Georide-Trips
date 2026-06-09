"""GeoRide Socket.IO Manager — connexion temps réel persistante.

Gère une connexion Socket.IO unique par intégration (entrée de config),
partagée entre tous les trackers du compte.

Événements reçus du serveur GeoRide :
  - "position"  : lat/lon/speed/heading/moving — mise à jour device_tracker
  - "device"    : moving/stolen/crashed/batteries — mise à jour binary_sensors + fire georide_device_event
  - "alarm"     : type d'alarme — fire HA event georide_alarm_event
  - "lock"      : état verrou — fire HA event georide_lock_event

Événements HA bus publiés (compatibles doc GeorideHA) :
  - georide_device_event  : data.device_id, data.device_name, data.moving, data.stolen, data.crashed
  - georide_alarm_event   : data.device_id, data.device_name, data.type
  - georide_lock_event    : data.device_id, data.device_name, data.locked

  Filtrer par data.device_id == XX (XX = tracker_id)

Types d'alarmes disponibles (georide_alarm_event, data.type) :
  alarm_vibration, alarm_exitZone, alarm_crash, alarm_crashParking,
  alarm_deviceOffline, alarm_deviceOnline, alarm_powerCut, alarm_powerUncut,
  alarm_batteryWarning, alarm_temperatureWarning, alarm_magnetOn, alarm_magnetOff,
  alarm_sonorAlarmOn

Architecture :
  GeoRideSocketManager
    ├── connexion Socket.IO (python-socketio AsyncClient)
    ├── reconnexion automatique (backoff exponentiel)
    ├── dict de callbacks par tracker_id
    └── dispatch vers entités HA via hass.loop
"""

import asyncio
import logging
from typing import Callable, Dict, Any, Optional

from .api import GeoRideApiError
from .const import SOCKETIO_URL

_LOGGER = logging.getLogger(__name__)

# Délai initial de reconnexion (secondes), doublé à chaque tentative
RECONNECT_DELAY_INITIAL = 5
RECONNECT_DELAY_MAX = 300  # 5 minutes max


class GeoRideSocketManager:
    """Gestionnaire de connexion Socket.IO GeoRide.

    Instancié une fois par entrée de config (compte GeoRide).
    Gère tous les trackers du compte sur une seule connexion.
    """

    def __init__(self, hass, api, tracker_ids: list[str]) -> None:
        """Initialiser le manager.

        Args:
            hass: instance Home Assistant
            api: GeoRideTripsAPI (déjà authentifiée)
            tracker_ids: liste des tracker IDs à suivre
        """
        self._hass = hass
        self._api = api
        self._tracker_ids = tracker_ids

        # Callbacks enregistrés par les entités
        # structure : {tracker_id: {event_name: [callback, ...]}}
        self._callbacks: Dict[str, Dict[str, list[Callable]]] = {}

        # État de la connexion
        self._sio = None
        self._connected = False
        self._should_run = False
        self._reconnect_task: Optional[asyncio.Task] = None
        self._reconnect_delay = RECONNECT_DELAY_INITIAL

    # ─────────────────────────────────────────────────────────────────
    # API publique
    # ─────────────────────────────────────────────────────────────────

    def register_callback(
        self,
        tracker_id: str,
        event_name: str,
        callback: Callable[[Dict[str, Any]], None],
    ) -> Callable:
        """Enregistrer un callback pour un événement d'un tracker.

        Returns:
            Fonction de désenregistrement (à appeler dans async_will_remove_from_hass)
        """
        self._callbacks.setdefault(tracker_id, {}).setdefault(event_name, [])
        self._callbacks[tracker_id][event_name].append(callback)
        _LOGGER.debug(
            "Callback registered: tracker=%s event=%s", tracker_id, event_name
        )

        def unregister():
            try:
                self._callbacks[tracker_id][event_name].remove(callback)
            except (KeyError, ValueError):
                pass

        return unregister

    @property
    def connected(self) -> bool:
        """True si Socket.IO est actuellement connecté."""
        return self._connected

    async def start(self) -> None:
        """Démarrer la connexion Socket.IO (appelé depuis async_setup_entry).

        async_create_background_task (et non hass.loop.create_task) : la tâche
        est suivie par HA et annulée à l'arrêt — sinon la boucle de reconnexion
        peut survivre au unload ("Task was destroyed but it is pending").
        """
        self._should_run = True
        self._reconnect_task = self._hass.async_create_background_task(
            self._run_loop(), name="georide_trips_socketio_reconnect"
        )
        _LOGGER.info(
            "GeoRide SocketManager started for %d trackers", len(self._tracker_ids)
        )

    async def stop(self) -> None:
        """Arrêter proprement la connexion (appelé depuis async_unload_entry)."""
        self._should_run = False
        if self._reconnect_task:
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass
        await self._disconnect()
        _LOGGER.info("GeoRide SocketManager stopped")

    # ─────────────────────────────────────────────────────────────────
    # Boucle de reconnexion
    # ─────────────────────────────────────────────────────────────────

    async def _run_loop(self) -> None:
        """Boucle principale : connexion + reconnexion automatique."""
        while self._should_run:
            was_connected = self._connected
            try:
                await self._connect()
            except asyncio.CancelledError:
                break
            except Exception as err:
                _LOGGER.error("Socket.IO connection error: %s", err)

            if not self._should_run:
                break

            # Reset délai seulement si on avait réussi à se connecter
            if was_connected:
                self._reconnect_delay = RECONNECT_DELAY_INITIAL

            _LOGGER.warning(
                "Socket.IO disconnected. Reconnecting in %ds...",
                self._reconnect_delay,
            )
            await asyncio.sleep(self._reconnect_delay)
            # Backoff exponentiel plafonné
            self._reconnect_delay = min(self._reconnect_delay * 2, RECONNECT_DELAY_MAX)

    # ─────────────────────────────────────────────────────────────────
    # Connexion Socket.IO
    # ─────────────────────────────────────────────────────────────────

    async def _connect(self) -> None:
        """Établir la connexion Socket.IO et bloquer jusqu'à déconnexion."""
        try:
            import socketio  # noqa: PLC0415 — importé ici pour éviter crash au load si absent
        except ImportError:
            _LOGGER.error(
                "python-socketio non installé. Ajoutez 'python-socketio[asyncio_client]>=5.0' "
                "dans manifest.json requirements."
            )
            await asyncio.sleep(RECONNECT_DELAY_MAX)
            return

        # S'assurer qu'on a un token valide
        if not self._api.token:
            try:
                await self._api.login()
            except GeoRideApiError as err:
                _LOGGER.error(
                    "Cannot connect Socket.IO: authentication failed: %s", err
                )
                return

        self._sio = socketio.AsyncClient(
            reconnection=False,  # on gère nous-mêmes la reconnexion
            logger=False,
            engineio_logger=False,
        )

        # ── Enregistrement des handlers ───────────────────────────────

        @self._sio.event
        async def connect():
            self._connected = True
            self._reconnect_delay = RECONNECT_DELAY_INITIAL
            _LOGGER.info("Socket.IO connected to GeoRide")
            # S'abonner à tous les trackers
            for tracker_id in self._tracker_ids:
                await self._sio.emit("subscribe", tracker_id)
                _LOGGER.debug("Subscribed to tracker %s", tracker_id)

        @self._sio.event
        async def disconnect():
            self._connected = False
            _LOGGER.warning("Socket.IO disconnected from GeoRide")

        @self._sio.on("position")
        async def on_position(data):
            await self._dispatch("position", data)

        @self._sio.on("device")
        async def on_device(data):
            await self._dispatch("device", data)
            # Fire HA event global (compatible avec les automations GeorideHA)
            self._hass.bus.async_fire(
                "georide_device_event",
                {
                    "tracker_id": str(data.get("trackerId", "")),
                    "device_id": str(data.get("trackerId", "")),
                    "device_name": data.get("device_name", ""),
                    "moving": data.get("moving"),
                    "stolen": data.get("stolen"),
                    "crashed": data.get("crashed"),
                },
            )

        @self._sio.on("alarm")
        async def on_alarm(data):
            await self._dispatch("alarm", data)
            # Fire HA event global (compatible avec les automations GeorideHA)
            self._hass.bus.async_fire(
                "georide_alarm_event",
                {
                    "tracker_id": str(data.get("trackerId", "")),
                    "device_id": str(data.get("trackerId", "")),
                    # GeoRide envoie le type dans 'name', fallback sur 'type'
                    "type": data.get("name") or data.get("type", ""),
                    "device_name": data.get("trackerName")
                    or data.get("device_name", ""),
                },
            )

        @self._sio.on("lock")
        async def on_lock(data):
            await self._dispatch("lock", data)
            self._hass.bus.async_fire(
                "georide_lock_event",
                {
                    "tracker_id": str(data.get("trackerId", "")),
                    "device_id": str(data.get("trackerId", "")),
                    "locked": data.get("locked", False),
                    "device_name": data.get("device_name", ""),
                },
            )

        # ── Connexion ─────────────────────────────────────────────────

        try:
            await self._sio.connect(
                SOCKETIO_URL,
                auth={"token": self._api.token},  # selon doc officielle GeoRide
                transports=["websocket"],
                wait_timeout=15,
            )
            # Bloquer jusqu'à déconnexion
            await self._sio.wait()
        except asyncio.CancelledError:
            # Annulation (unload/arrêt HA) : doit remonter jusqu'à _run_loop,
            # jamais être avalée par le except générique ci-dessous.
            raise
        except Exception as err:
            _LOGGER.error(
                "Socket.IO connect failed: %s — %s: %s",
                err,
                type(err).__name__,
                getattr(err, "args", ""),
            )
        finally:
            self._connected = False
            try:
                await self._sio.disconnect()
            except Exception:
                pass
            self._sio = None

    async def _disconnect(self) -> None:
        """Déconnecter proprement."""
        if self._sio:
            try:
                await self._sio.disconnect()
            except Exception:
                pass
            self._sio = None
        self._connected = False

    # ─────────────────────────────────────────────────────────────────
    # Dispatch vers les entités
    # ─────────────────────────────────────────────────────────────────

    async def _dispatch(self, event_name: str, data: Dict[str, Any]) -> None:
        """Dispatcher un événement vers tous les callbacks enregistrés.

        Le tracker_id est extrait du champ 'trackerId' du payload.
        """
        tracker_id = str(data.get("trackerId", ""))
        if not tracker_id:
            _LOGGER.debug("Socket.IO event '%s' without trackerId, ignored", event_name)
            return

        _LOGGER.debug(
            "Socket.IO event '%s' for tracker %s: %s", event_name, tracker_id, data
        )

        callbacks = self._callbacks.get(tracker_id, {}).get(event_name, [])

        for cb in list(callbacks):  # copie pour éviter modification pendant itération
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(data)
                else:
                    cb(data)
            except Exception as err:
                _LOGGER.error(
                    "Error in callback for event '%s' tracker %s: %s",
                    event_name,
                    tracker_id,
                    err,
                )
