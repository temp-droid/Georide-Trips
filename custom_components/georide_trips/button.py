"""GeoRide Trips buttons - Refresh buttons and maintenance record buttons."""

import logging
from datetime import datetime, timezone

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    CONF_DRIVE_TYPE,
    DEFAULT_DRIVE_TYPE,
    DRIVETRAIN_PROFILES,
)
from .data import GeoRideConfigEntry
from .helpers import GeoRideEntityMixin

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GeoRideConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up GeoRide Trips buttons from a config entry."""
    data = entry.runtime_data
    trackers = data.trackers
    coordinators = data.coordinators
    lifetime_coordinators = data.lifetime_coordinators
    api = data.api

    profile = DRIVETRAIN_PROFILES.get(
        entry.options.get(CONF_DRIVE_TYPE, DEFAULT_DRIVE_TYPE),
        DRIVETRAIN_PROFILES["chain"],
    )

    buttons = []
    for tracker in trackers:
        tracker_id = str(tracker.get("trackerId"))

        buttons.extend(
            [
                GeoRideRefreshTripsButton(entry, tracker, coordinators[tracker_id]),
                GeoRideRefreshOdometerButton(
                    entry, tracker, lifetime_coordinators[tracker_id]
                ),
                GeoRideConfirmerPleinButton(
                    hass,
                    entry,
                    tracker,
                    api=api,
                    coordinator=coordinators[tracker_id],
                ),
                GeoRideAppliquerAutonomieButton(
                    hass,
                    entry,
                    tracker,
                ),
                GeoRideRecordMaintenanceButton(
                    hass,
                    entry,
                    tracker,
                    "oil_change",
                    icon="mdi:oil",
                    odometer_key="real_odometer",
                    km_key="oil_change_km_at_last_oil_change",
                    dt_key="oil_change_last_oil_change_date",
                ),
                GeoRideRecordMaintenanceButton(
                    hass,
                    entry,
                    tracker,
                    "service",
                    icon="mdi:wrench",
                    odometer_key="real_odometer",
                    km_key="service_km_at_last_service",
                    dt_key="service_last_service_date",
                ),
            ]
        )

        # Drivetrain maintenance record button — always created; label adapts
        # to the selected drive_type (chain / shaft / belt).
        buttons.append(
            GeoRideRecordMaintenanceButton(
                hass,
                entry,
                tracker,
                "drivetrain",
                icon="mdi:link-variant",
                odometer_key="real_odometer",
                km_key="drivetrain_km_at_last_service",
                dt_key="drivetrain_last_service_date",
                name=f"Record {profile['label'].lower()} service",
            )
        )

    async_add_entities(buttons)
    _LOGGER.info("Added %d buttons for %d trackers", len(buttons), len(trackers))


class GeoRideRefreshTripsButton(GeoRideEntityMixin, ButtonEntity):
    """Button to manually refresh recent trips."""

    def __init__(self, entry, tracker, coordinator):
        """Initialize the button."""
        self.tracker_id = str(tracker.get("trackerId"))
        self.tracker_name = tracker.get("trackerName", f"Tracker {self.tracker_id}")
        self._entry = entry
        self._tracker = tracker
        self._coordinator = coordinator

        self._attr_name = "Refresh trips"
        self._attr_unique_id = f"{self.tracker_id}_refresh_trips"
        self._attr_icon = "mdi:refresh"

    async def async_press(self) -> None:
        """Handle the button press - refresh recent trips."""
        _LOGGER.info("Manual refresh triggered for trips: %s", self.tracker_name)
        await self._coordinator.async_request_refresh()


class GeoRideRefreshOdometerButton(GeoRideEntityMixin, ButtonEntity):
    """Button to manually refresh lifetime odometer."""

    def __init__(self, entry, tracker, coordinator):
        """Initialize the button."""
        self.tracker_id = str(tracker.get("trackerId"))
        self.tracker_name = tracker.get("trackerName", f"Tracker {self.tracker_id}")
        self._entry = entry
        self._tracker = tracker
        self._coordinator = coordinator

        self._attr_name = "Refresh odometer"
        self._attr_unique_id = f"{self.tracker_id}_refresh_odometer"
        self._attr_icon = "mdi:counter"

    async def async_press(self) -> None:
        """Handle the button press - refresh lifetime odometer."""
        _LOGGER.info("Manual refresh triggered for odometer: %s", self.tracker_name)
        await self._coordinator.async_request_refresh()


class GeoRideRecordMaintenanceButton(GeoRideEntityMixin, ButtonEntity):
    """Button to record a maintenance event (drivetrain, oil change, service)."""

    LABEL = {
        "drivetrain": "Record drivetrain service",
        "oil_change": "Record oil change",
        "service": "Record service",
    }

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        tracker: dict,
        maintenance_type: str,
        icon: str,
        odometer_key: str,
        km_key: str,
        dt_key: str,
        name: str | None = None,
    ) -> None:
        """Initialize the maintenance record button."""
        self._hass = hass
        self._entry = entry
        self._tracker = tracker
        self._maintenance_type = maintenance_type
        self._odometer_key = odometer_key
        self._km_key = km_key
        self._dt_key = dt_key

        # Entity_ids resolved in async_added_to_hass
        self._odometer_entity: str | None = None
        self._km_entity: str | None = None
        self._dt_entity: str | None = None

        self.tracker_id = str(tracker.get("trackerId"))
        self.tracker_name = tracker.get("trackerName", f"Tracker {self.tracker_id}")

        self._attr_name = name or self.LABEL.get(maintenance_type, maintenance_type)
        self._attr_unique_id = f"{self.tracker_id}_record_{maintenance_type}"
        self._attr_icon = icon

    async def async_added_to_hass(self) -> None:
        """Resolve the entity_ids via the registry."""
        await super().async_added_to_hass()
        from .helpers import resolve_entity_id

        self._odometer_entity = resolve_entity_id(
            self._hass,
            "sensor",
            self.tracker_id,
            self._odometer_key,
        )
        self._km_entity = resolve_entity_id(
            self._hass,
            "number",
            self.tracker_id,
            self._km_key,
        )
        self._dt_entity = resolve_entity_id(
            self._hass,
            "datetime",
            self.tracker_id,
            self._dt_key,
        )

    async def async_press(self) -> None:
        """Record maintenance: snapshot odometer KM + current datetime."""
        if not self._odometer_entity or not self._km_entity or not self._dt_entity:
            _LOGGER.error(
                "Cannot record %s for %s: entity_id not resolved (odometer=%s, km=%s, dt=%s)",
                self._maintenance_type,
                self.tracker_name,
                self._odometer_entity,
                self._km_entity,
                self._dt_entity,
            )
            return

        odometer_state = self._hass.states.get(self._odometer_entity)
        if odometer_state is None or odometer_state.state in ("unknown", "unavailable"):
            _LOGGER.warning(
                "Cannot record %s for %s: odometer entity '%s' unavailable",
                self._maintenance_type,
                self.tracker_name,
                self._odometer_entity,
            )
            return

        try:
            odometer_km = float(odometer_state.state)
        except ValueError:
            _LOGGER.error(
                "Cannot parse odometer value '%s' for %s",
                odometer_state.state,
                self.tracker_name,
            )
            return

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        # Update the KM
        await self._hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": self._km_entity, "value": odometer_km},
            blocking=True,
        )

        # Update the date
        await self._hass.services.async_call(
            "datetime",
            "set_value",
            {"entity_id": self._dt_entity, "datetime": now_str},
            blocking=True,
        )

        _LOGGER.info(
            "Recorded %s for %s: %.1f km on %s",
            self._maintenance_type,
            self.tracker_name,
            odometer_km,
            now_str,
        )


class GeoRideConfirmerPleinButton(GeoRideEntityMixin, ButtonEntity):
    """Button to confirm a refuel — precise odometer computation in 2 steps.

    Step 1 (async_press) — immediate:
        • Store refuel_pending_at = now() (datetime)
        • Turn off the "Refuel" switch
        • Subscribe to the coordinator's next confirmed end of trip (on_stop_confirmed)

    Step 2 (_on_stop_confirmed_for_plein) — triggered on lock:
        • API call get_trips(refuel_pending_at → now) → post-refuel distance
        • odometer_au_plein = odometer_actuel - distance_post_plein
        • Compute inter-refuel distance
        • FIFO history rotation (hist_3 ← hist_2 ← hist_1 ← new)
        • Recompute rolling average (max 3 refuels)
        • Update fuel_km_at_last_refuel + fuel_recorded_refuel_count
        • Reset refuel_pending_at = None (sentinel epoch 1970)
    """

    HIST_SLOTS = 3

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        tracker: dict,
        api,
        coordinator,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._tracker = tracker
        self._api = api
        self._coordinator = coordinator

        self.tracker_id = str(tracker.get("trackerId"))
        self.tracker_name = tracker.get("trackerName", f"Tracker {self.tracker_id}")

        self._attr_name = "Confirm refuel"
        self._attr_unique_id = f"{self.tracker_id}_confirm_refuel"
        self._attr_icon = "mdi:gas-station-outline"

        # Management of the stop_confirmed subscription
        self._unregister_stop_cb: callable | None = None

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _get_float(self, entity_id: str, default: float = 0.0) -> float:
        state = self._hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return default
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return default

    def _number_entity_id(self, key: str) -> str | None:
        """Resolve a number's entity_id from its key via the entity registry."""
        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(self._hass)
        unique_id = f"{self.tracker_id}_{key}"
        return registry.async_get_entity_id("number", DOMAIN, unique_id)

    def _datetime_entity_id(self, key: str) -> str | None:
        """Resolve a datetime's entity_id from its key via the entity registry."""
        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(self._hass)
        unique_id = f"{self.tracker_id}_{key}"
        return registry.async_get_entity_id("datetime", DOMAIN, unique_id)

    def _get_number(self, key: str, default: float = 0.0) -> float:
        """Read a number's value by its key (via the entity registry)."""
        entity_id = self._number_entity_id(key)
        if entity_id is None:
            _LOGGER.warning(
                "%s: entity_id not found for number key '%s'",
                self.tracker_name,
                key,
            )
            return default
        return self._get_float(entity_id, default)

    def _get_datetime(self, key: str) -> datetime | None:
        """Read a datetime's value by its key. Returns None if missing or sentinel 1970."""
        entity_id = self._datetime_entity_id(key)
        if entity_id is None:
            _LOGGER.warning(
                "%s: entity_id not found for datetime key '%s'",
                self.tracker_name,
                key,
            )
            return None
        state = self._hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            dt = datetime.fromisoformat(state.state.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            # Sentinel: epoch 1970 = no pending refuel
            if dt.year == 1970:
                return None
            return dt
        except (ValueError, AttributeError):
            return None

    async def _set_number(self, key: str, value: float) -> None:
        entity_id = self._number_entity_id(key)
        if entity_id is None:
            _LOGGER.error(
                "%s: entity_id not found for number key '%s' (unique_id=%s_%s)",
                self.tracker_name,
                key,
                self.tracker_id,
                key,
            )
            return
        await self._hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": entity_id, "value": value},
            blocking=True,
        )

    async def _set_datetime(self, key: str, value: datetime | None) -> None:
        """Write a datetime by its key. None → sentinel epoch 1970."""
        entity_id = self._datetime_entity_id(key)
        if entity_id is None:
            _LOGGER.error(
                "%s: entity_id not found for datetime key '%s' (unique_id=%s_%s)",
                self.tracker_name,
                key,
                self.tracker_id,
                key,
            )
            return
        if value is None:
            value = datetime(1970, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        elif value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        await self._hass.services.async_call(
            "datetime",
            "set_value",
            {"entity_id": entity_id, "datetime": value.strftime("%Y-%m-%d %H:%M:%S")},
            blocking=True,
        )

    def _cancel_pending(self) -> None:
        """Cancel the in-progress stop_confirmed subscription."""
        if self._unregister_stop_cb:
            self._unregister_stop_cb()
            self._unregister_stop_cb = None

    async def async_added_to_hass(self) -> None:
        """At startup, process or re-subscribe a pending refuel if one exists."""
        await super().async_added_to_hass()
        plein_pending = self._get_datetime("refuel_pending_at")
        if plein_pending is not None:
            if self._is_tracker_locked():
                # Tracker locked → the post-refuel trip is over, we can compute
                _LOGGER.info(
                    "%s: refuel_pending_at non-null at startup (%s) and tracker locked "
                    "— attempting deferred computation",
                    self.tracker_name,
                    plein_pending.isoformat(),
                )
                self._hass.async_create_task(self._compute_and_record_plein())
            else:
                # Tracker unlocked → trip in progress, re-subscribe the callback
                _LOGGER.info(
                    "%s: refuel_pending_at non-null at startup (%s) and tracker unlocked "
                    "— re-subscribing to wait for lock",
                    self.tracker_name,
                    plein_pending.isoformat(),
                )
                self._unregister_stop_cb = self._coordinator.on_stop_confirmed(
                    self._on_stop_confirmed_for_plein
                )

    # ── Lock state helpers ──────────────────────────────────────────────────

    def _is_tracker_locked(self) -> bool:
        """Check whether the tracker is currently locked.

        Uses the binary_sensor via the registry (up to date in real time via
        Socket.IO), or falls back to the coordinator's public is_locked
        property (polling 5 min).
        binary_sensor lock: off = locked, on = unlocked.
        """
        from .helpers import resolve_entity_id

        lock_entity = resolve_entity_id(
            self._hass, "binary_sensor", self.tracker_id, "locked"
        )
        if lock_entity:
            state = self._hass.states.get(lock_entity)
            if state and state.state not in ("unknown", "unavailable"):
                return state.state == "off"  # off = locked

        # Fallback: the coordinator's public state (attached StatusCoordinator)
        locked = self._coordinator.is_locked
        return bool(locked) if locked is not None else False

    # ── Step 1: immediate press ───────────────────────────────────────────────

    async def async_press(self) -> None:
        """Step 1 — Timestamp the refuel + wait for the next lock.

        If the tracker is already locked (refuel confirmed while stopped), the
        computation runs immediately without waiting for a future lock.
        """
        # If a refuel is already pending, cancel it cleanly
        if self._unregister_stop_cb:
            _LOGGER.warning(
                "%s: new refuel press while a refuel was already pending — cancelling",
                self.tracker_name,
            )
            self._cancel_pending()

        # Snapshot: refuel timestamp only
        now = datetime.now(timezone.utc)

        await self._set_datetime("refuel_pending_at", now)

        # If the tracker is already locked → immediate computation
        if self._is_tracker_locked():
            _LOGGER.info(
                "%s: refuel recorded at %s — tracker already locked, immediate computation",
                self.tracker_name,
                now.strftime("%H:%M:%S"),
            )
            await self._compute_and_record_plein()
            return

        _LOGGER.info(
            "%s: refuel recorded at %s — waiting for lock for precise computation",
            self.tracker_name,
            now.strftime("%H:%M:%S"),
        )

        # Subscribe to the next lock (one-shot)
        self._unregister_stop_cb = self._coordinator.on_stop_confirmed(
            self._on_stop_confirmed_for_plein
        )

    # ── Step 2: confirmed end of trip ────────────────────────────────────────

    def _on_stop_confirmed_for_plein(self) -> None:
        """Callback called by the coordinator when the tracker locks."""
        # _unregister_stop_cb is already consumed (one-shot), clean up the reference
        self._unregister_stop_cb = None
        self._hass.async_create_task(self._compute_and_record_plein())

    # ── Computation on lock ───────────────────────────────────────────────────

    async def _compute_and_record_plein(self) -> None:
        """Compute the odometer at refuel and record all the metrics.

        Logic:
        - refuel_pending_at  = refuel timestamp (UTC datetime)
        - Refresh the coordinator to ensure the HA odometer includes the trip
        - API call get_trips(refuel_pending_at → now) → distance traveled AFTER the refuel
        - odometer_au_plein = odometer_actuel - distance_post_plein
        """
        from .const import METERS_TO_KM
        from .helpers import resolve_entity_id

        plein_dt = self._get_datetime("refuel_pending_at")
        fuel_km_at_last_refuel = self._get_number("fuel_km_at_last_refuel")

        # ── Refresh the coordinator to ensure the odometer is up to date ──────
        # Without this refresh, the trip that just ended is not yet integrated
        # into sensor.real_odometer, which would skew the odometer_au_plein computation.
        _LOGGER.debug(
            "%s: refresh coordinator before reading odometer (ensure trip is integrated)",
            self.tracker_name,
        )
        await self._coordinator.async_request_refresh()

        odometer_entity = resolve_entity_id(
            self._hass, "sensor", self.tracker_id, "real_odometer"
        )
        odometer_actuel = self._get_float(odometer_entity) if odometer_entity else 0.0

        if plein_dt is None:
            _LOGGER.warning(
                "%s: _compute_and_record_plein called without a valid refuel_pending_at",
                self.tracker_name,
            )
            return

        if odometer_actuel <= 0:
            _LOGGER.error(
                "%s: invalid current odometer (%.1f), aborting",
                self.tracker_name,
                odometer_actuel,
            )
            await self._set_datetime("refuel_pending_at", None)
            return

        # ── Distance traveled AFTER the refuel (refuel_pending_at → now) ────────
        now_dt = datetime.now(timezone.utc)
        distance_post_plein = await self._fetch_post_plein_distance(
            plein_dt, now_dt, METERS_TO_KM
        )

        odometer_au_plein = round(odometer_actuel - distance_post_plein, 2)
        _LOGGER.info(
            "%s: refuel odometer = %.1f km (current=%.1f - post_refuel=%.1f km)",
            self.tracker_name,
            odometer_au_plein,
            odometer_actuel,
            distance_post_plein,
        )

        if odometer_au_plein <= 0:
            _LOGGER.error(
                "%s: invalid refuel odometer (%.1f), aborting",
                self.tracker_name,
                odometer_au_plein,
            )
            await self._set_datetime("refuel_pending_at", None)
            return

        # ── First refuel: just snapshot, no inter-refuel computation ──────
        if fuel_km_at_last_refuel == 0:
            _LOGGER.info(
                "%s: first refuel — odometer snapshot = %.1f km",
                self.tracker_name,
                odometer_au_plein,
            )
            await self._set_number("fuel_km_at_last_refuel", odometer_au_plein)
            await self._set_number("fuel_recorded_refuel_count", 1)
            await self._set_datetime("refuel_pending_at", None)
            return

        # ── Inter-refuel distance computation ──────────────────────────────
        distance_inter_plein = round(odometer_au_plein - fuel_km_at_last_refuel, 1)
        if distance_inter_plein <= 0:
            _LOGGER.warning(
                "%s: negative inter-refuel distance (%.1f km), aborting",
                self.tracker_name,
                distance_inter_plein,
            )
            await self._set_datetime("refuel_pending_at", None)
            return

        # ── FIFO history rotation ────────────────────────────────────────────
        hist_1 = self._get_number("fuel_distance_between_refuels_1")
        hist_2 = self._get_number("fuel_distance_between_refuels_2")

        await self._set_number("fuel_distance_between_refuels_3", hist_2)
        await self._set_number("fuel_distance_between_refuels_2", hist_1)
        await self._set_number("fuel_distance_between_refuels_1", distance_inter_plein)

        # ── Rolling average (non-zero slots, max HIST_SLOTS) ───────────────
        slots = [s for s in [distance_inter_plein, hist_1, hist_2] if s > 0]
        slots = slots[: self.HIST_SLOTS]
        moyenne = round(sum(slots) / len(slots))

        nb_pleins = int(self._get_number("fuel_recorded_refuel_count")) + 1

        await self._set_number("fuel_calculated_average_range", moyenne)
        await self._set_number("fuel_recorded_refuel_count", nb_pleins)
        await self._set_number("fuel_km_at_last_refuel", odometer_au_plein)
        await self._set_datetime("refuel_pending_at", None)

        _LOGGER.info(
            "%s: refuel confirmed — odometer=%.1f km, inter-refuel=%.1f km, "
            "average=%d km (%d value(s)), nb_pleins=%d",
            self.tracker_name,
            odometer_au_plein,
            distance_inter_plein,
            moyenne,
            len(slots),
            nb_pleins,
        )

    async def _fetch_post_plein_distance(
        self, plein_dt: datetime, now_dt: datetime, meters_to_km: float
    ) -> float:
        """Call the get_trips API between refuel_pending_at and now.

        Returns the distance traveled AFTER the refuel in km, or 0.0 if the API fails.
        """
        try:
            _LOGGER.debug(
                "%s: fetch post-refuel trips: %s → %s",
                self.tracker_name,
                plein_dt.isoformat(),
                now_dt.isoformat(),
            )

            trips = await self._api.get_trips(
                self.tracker_id,
                from_date=plein_dt,
                to_date=now_dt,
            )

            if trips is None:
                _LOGGER.warning("%s: get_trips API returned None", self.tracker_name)
                return 0.0

            distance_km = round(
                sum(t.get("distance", 0) for t in trips) / meters_to_km, 2
            )

            _LOGGER.info(
                "%s: post-refuel distance API = %.1f km (%d segment(s) between %s and %s)",
                self.tracker_name,
                distance_km,
                len(trips),
                plein_dt.strftime("%H:%M"),
                now_dt.strftime("%H:%M"),
            )
            return distance_km

        except Exception as err:
            _LOGGER.error(
                "%s: error fetching post-refuel distance: %s",
                self.tracker_name,
                err,
            )
            return 0.0


class GeoRideAppliquerAutonomieButton(GeoRideEntityMixin, ButtonEntity):
    """Button to apply the computed average range as the reference range.

    `button.<moto>_appliquer_autonomie_calculee`

    Copies the value of `number.<moto>_carburant_autonomie_moyenne_calculee`
    into `number.<moto>_autonomie_totale`.

    Usable:
    - Manually from the HA UI
    - Via the blueprint after a "new average available" notification
      (mobile action ✅ Apply)

    Pre-condition: fuel_recorded_refuel_count >= 2 and fuel_calculated_average_range > 0.
    If not met, logs a warning and does nothing.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, tracker: dict) -> None:
        self._hass = hass
        self._entry = entry
        self._tracker = tracker

        self.tracker_id = str(tracker.get("trackerId"))
        self.tracker_name = tracker.get("trackerName", f"Tracker {self.tracker_id}")

        self._attr_name = "Apply calculated range"
        self._attr_unique_id = f"{self.tracker_id}_apply_calculated_range"
        self._attr_icon = "mdi:check-circle-outline"

    def _get_float(self, entity_id: str, default: float = 0.0) -> float:
        state = self._hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return default
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return default

    async def async_press(self) -> None:
        """Copy fuel_calculated_average_range → fuel_total_range."""
        from .helpers import resolve_entity_id

        entity_moyenne = resolve_entity_id(
            self._hass, "number", self.tracker_id, "fuel_calculated_average_range"
        )
        entity_nb_pleins = resolve_entity_id(
            self._hass, "number", self.tracker_id, "fuel_recorded_refuel_count"
        )
        entity_totale = resolve_entity_id(
            self._hass, "number", self.tracker_id, "fuel_total_range"
        )

        if not entity_moyenne or not entity_nb_pleins or not entity_totale:
            _LOGGER.error(
                "%s: unable to resolve the fuel entities via the registry",
                self.tracker_name,
            )
            return

        nb_pleins = self._get_float(entity_nb_pleins)
        moyenne = self._get_float(entity_moyenne)

        if nb_pleins < 2 or moyenne <= 0:
            _LOGGER.warning(
                "%s: cannot apply the computed range "
                "(nb_pleins=%.0f, average=%.1f km) — need at least 2 refuels.",
                self.tracker_name,
                nb_pleins,
                moyenne,
            )
            return

        await self._hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": entity_totale, "value": moyenne},
            blocking=True,
        )

        _LOGGER.info(
            "%s: fuel_total_range updated → %.1f km (average over %.0f refuels)",
            self.tracker_name,
            moyenne,
            nb_pleins,
        )
