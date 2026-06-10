"""GeoRide Socket.IO Manager — persistent real-time connection.

Manages a single Socket.IO connection per integration (config entry),
shared across all trackers of the account.

Events received from the GeoRide server:
  - "position"  : lat/lon/speed/heading/moving — device_tracker update
  - "device"    : moving/stolen/crashed/batteries — binary_sensors update + fire georide_device_event
  - "alarm"     : alarm type — fire HA event georide_alarm_event
  - "lock"      : lock state — fire HA event georide_lock_event

HA bus events published (compatible with the GeorideHA docs):
  - georide_device_event  : data.device_id, data.device_name, data.moving, data.stolen, data.crashed
  - georide_alarm_event   : data.device_id, data.device_name, data.type
  - georide_lock_event    : data.device_id, data.device_name, data.locked

  Filter by data.device_id == XX (XX = tracker_id)

Available alarm types (georide_alarm_event, data.type):
  alarm_vibration, alarm_exitZone, alarm_crash, alarm_crashParking,
  alarm_deviceOffline, alarm_deviceOnline, alarm_powerCut, alarm_powerUncut,
  alarm_batteryWarning, alarm_temperatureWarning, alarm_magnetOn, alarm_magnetOff,
  alarm_sonorAlarmOn

Architecture:
  GeoRideSocketManager
    ├── Socket.IO connection (python-socketio AsyncClient)
    ├── automatic reconnection (exponential backoff)
    ├── dict of callbacks per tracker_id
    └── dispatch to HA entities via hass.loop
"""

import asyncio
import logging
from typing import Callable, Dict, Any, Optional

from .api import GeoRideApiError
from .const import SOCKETIO_URL

_LOGGER = logging.getLogger(__name__)

# Initial reconnection delay (seconds), doubled on each attempt
RECONNECT_DELAY_INITIAL = 5
RECONNECT_DELAY_MAX = 300  # 5 minutes max


class GeoRideSocketManager:
    """GeoRide Socket.IO connection manager.

    Instantiated once per config entry (GeoRide account).
    Manages all trackers of the account over a single connection.
    """

    def __init__(self, hass, api, tracker_ids: list[str]) -> None:
        """Initialize the manager.

        Args:
            hass: Home Assistant instance
            api: GeoRideTripsAPI (already authenticated)
            tracker_ids: list of tracker IDs to follow
        """
        self._hass = hass
        self._api = api
        self._tracker_ids = tracker_ids

        # Callbacks registered by the entities
        # structure: {tracker_id: {event_name: [callback, ...]}}
        self._callbacks: Dict[str, Dict[str, list[Callable]]] = {}

        # Connection state
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
        """Register a callback for an event of a tracker.

        Returns:
            Unregister function (to call in async_will_remove_from_hass)
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
        """True if Socket.IO is currently connected."""
        return self._connected

    async def start(self) -> None:
        """Start the Socket.IO connection (called from async_setup_entry).

        async_create_background_task (rather than hass.loop.create_task): the task
        is tracked by HA and cancelled on shutdown — otherwise the reconnection
        loop can survive the unload ("Task was destroyed but it is pending").
        """
        self._should_run = True
        self._reconnect_task = self._hass.async_create_background_task(
            self._run_loop(), name="georide_trips_socketio_reconnect"
        )
        _LOGGER.info(
            "GeoRide SocketManager started for %d trackers", len(self._tracker_ids)
        )

    async def stop(self) -> None:
        """Cleanly stop the connection (called from async_unload_entry)."""
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
    # Reconnection loop
    # ─────────────────────────────────────────────────────────────────

    async def _run_loop(self) -> None:
        """Main loop: connection + automatic reconnection."""
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

            # Reset delay only if we had successfully connected
            if was_connected:
                self._reconnect_delay = RECONNECT_DELAY_INITIAL

            _LOGGER.warning(
                "Socket.IO disconnected. Reconnecting in %ds...",
                self._reconnect_delay,
            )
            await asyncio.sleep(self._reconnect_delay)
            # Capped exponential backoff
            self._reconnect_delay = min(self._reconnect_delay * 2, RECONNECT_DELAY_MAX)

    # ─────────────────────────────────────────────────────────────────
    # Socket.IO connection
    # ─────────────────────────────────────────────────────────────────

    async def _connect(self) -> None:
        """Establish the Socket.IO connection and block until disconnection."""
        try:
            import socketio  # noqa: PLC0415 — imported here to avoid a crash at load time if missing
        except ImportError:
            _LOGGER.error(
                "python-socketio not installed. Add 'python-socketio[asyncio_client]>=5.0' "
                "to manifest.json requirements."
            )
            await asyncio.sleep(RECONNECT_DELAY_MAX)
            return

        # Make sure we have a valid token
        if not self._api.token:
            try:
                await self._api.login()
            except GeoRideApiError as err:
                _LOGGER.error(
                    "Cannot connect Socket.IO: authentication failed: %s", err
                )
                return

        self._sio = socketio.AsyncClient(
            reconnection=False,  # we handle reconnection ourselves
            logger=False,
            engineio_logger=False,
        )

        # ── Handler registration ──────────────────────────────────────

        @self._sio.event
        async def connect():
            self._connected = True
            self._reconnect_delay = RECONNECT_DELAY_INITIAL
            _LOGGER.info("Socket.IO connected to GeoRide")
            # Subscribe to all trackers
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
            # Fire a global HA event (compatible with GeorideHA automations)
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
            # Fire a global HA event (compatible with GeorideHA automations)
            self._hass.bus.async_fire(
                "georide_alarm_event",
                {
                    "tracker_id": str(data.get("trackerId", "")),
                    "device_id": str(data.get("trackerId", "")),
                    # GeoRide sends the type in 'name', fallback to 'type'
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

        # ── Connection ────────────────────────────────────────────────

        try:
            await self._sio.connect(
                SOCKETIO_URL,
                auth={"token": self._api.token},  # per the official GeoRide docs
                transports=["websocket"],
                wait_timeout=15,
            )
            # Block until disconnection
            await self._sio.wait()
        except asyncio.CancelledError:
            # Cancellation (unload/HA shutdown): must propagate up to _run_loop,
            # never be swallowed by the generic except below.
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
        """Disconnect cleanly."""
        if self._sio:
            try:
                await self._sio.disconnect()
            except Exception:
                pass
            self._sio = None
        self._connected = False

    # ─────────────────────────────────────────────────────────────────
    # Dispatch to entities
    # ─────────────────────────────────────────────────────────────────

    async def _dispatch(self, event_name: str, data: Dict[str, Any]) -> None:
        """Dispatch an event to all registered callbacks.

        The tracker_id is extracted from the payload's 'trackerId' field.
        """
        tracker_id = str(data.get("trackerId", ""))
        if not tracker_id:
            _LOGGER.debug("Socket.IO event '%s' without trackerId, ignored", event_name)
            return

        _LOGGER.debug(
            "Socket.IO event '%s' for tracker %s: %s", event_name, tracker_id, data
        )

        callbacks = self._callbacks.get(tracker_id, {}).get(event_name, [])

        for cb in list(callbacks):  # copy to avoid modification during iteration
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
