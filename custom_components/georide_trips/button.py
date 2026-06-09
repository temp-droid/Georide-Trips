"""GeoRide Trips buttons - Refresh buttons and maintenance record buttons."""

import logging
from datetime import datetime, timezone

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up GeoRide Trips buttons from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    trackers = data["trackers"]
    coordinators = data["coordinators"]
    lifetime_coordinators = data["lifetime_coordinators"]
    api = data["api"]

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
                    "chaine",
                    icon="mdi:link-variant",
                    odometer_key="real_odometer",
                    km_key="km_dernier_entretien_chaine",
                    dt_key="date_dernier_entretien_chaine",
                ),
                GeoRideRecordMaintenanceButton(
                    hass,
                    entry,
                    tracker,
                    "vidange",
                    icon="mdi:oil",
                    odometer_key="real_odometer",
                    km_key="km_dernier_entretien_vidange",
                    dt_key="date_dernier_entretien_vidange",
                ),
                GeoRideRecordMaintenanceButton(
                    hass,
                    entry,
                    tracker,
                    "revision",
                    icon="mdi:wrench",
                    odometer_key="real_odometer",
                    km_key="km_dernier_entretien_revision",
                    dt_key="date_dernier_entretien_revision",
                ),
            ]
        )

    async_add_entities(buttons)
    _LOGGER.info("Added %d buttons for %d trackers", len(buttons), len(trackers))


class GeoRideRefreshTripsButton(ButtonEntity):
    """Button to manually refresh recent trips."""

    def __init__(self, entry, tracker, coordinator):
        """Initialize the button."""
        self.tracker_id = str(tracker.get("trackerId"))
        self.tracker_name = tracker.get("trackerName", f"Tracker {self.tracker_id}")
        self._entry = entry
        self._tracker = tracker
        self._coordinator = coordinator

        self._attr_name = f"{self.tracker_name} Refresh Trips"
        self._attr_unique_id = f"{self.tracker_id}_refresh_trips"
        self._attr_icon = "mdi:refresh"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.tracker_id)},
            name=f"{self.tracker_name} Trips",
            manufacturer="GeoRide",
            model=self._tracker.get("model", "GeoRide Tracker"),
            sw_version=str(self._tracker.get("softwareVersion", "")),
        )

    async def async_press(self) -> None:
        """Handle the button press - refresh recent trips."""
        _LOGGER.info("Manual refresh triggered for trips: %s", self.tracker_name)
        await self._coordinator.async_request_refresh()


class GeoRideRefreshOdometerButton(ButtonEntity):
    """Button to manually refresh lifetime odometer."""

    def __init__(self, entry, tracker, coordinator):
        """Initialize the button."""
        self.tracker_id = str(tracker.get("trackerId"))
        self.tracker_name = tracker.get("trackerName", f"Tracker {self.tracker_id}")
        self._entry = entry
        self._tracker = tracker
        self._coordinator = coordinator

        self._attr_name = f"{self.tracker_name} Refresh Odometer"
        self._attr_unique_id = f"{self.tracker_id}_refresh_odometer"
        self._attr_icon = "mdi:counter"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.tracker_id)},
            name=f"{self.tracker_name} Trips",
            manufacturer="GeoRide",
            model=self._tracker.get("model", "GeoRide Tracker"),
            sw_version=str(self._tracker.get("softwareVersion", "")),
        )

    async def async_press(self) -> None:
        """Handle the button press - refresh lifetime odometer."""
        _LOGGER.info("Manual refresh triggered for odometer: %s", self.tracker_name)
        await self._coordinator.async_request_refresh()


class GeoRideRecordMaintenanceButton(ButtonEntity):
    """Button to record a maintenance event (chain, oil change, revision)."""

    LABEL = {
        "chaine": "Enregistrer entretien chaîne",
        "vidange": "Enregistrer vidange",
        "revision": "Enregistrer révision",
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
    ) -> None:
        """Initialize the maintenance record button."""
        self._hass = hass
        self._entry = entry
        self._tracker = tracker
        self._maintenance_type = maintenance_type
        self._odometer_key = odometer_key
        self._km_key = km_key
        self._dt_key = dt_key

        # Entity_id résolus dans async_added_to_hass
        self._odometer_entity: str | None = None
        self._km_entity: str | None = None
        self._dt_entity: str | None = None

        self.tracker_id = str(tracker.get("trackerId"))
        self.tracker_name = tracker.get("trackerName", f"Tracker {self.tracker_id}")

        label = self.LABEL.get(maintenance_type, maintenance_type)
        self._attr_name = f"{self.tracker_name} {label}"
        self._attr_unique_id = f"{self.tracker_id}_record_{maintenance_type}"
        self._attr_icon = icon

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.tracker_id)},
            name=f"{self.tracker_name} Trips",
            manufacturer="GeoRide",
            model=self._tracker.get("model", "GeoRide Tracker"),
            sw_version=str(self._tracker.get("softwareVersion", "")),
        )

    async def async_added_to_hass(self) -> None:
        """Résoudre les entity_id via le registry."""
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

        # Mise à jour du KM
        await self._hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": self._km_entity, "value": odometer_km},
            blocking=True,
        )

        # Mise à jour de la date
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


class GeoRideConfirmerPleinButton(ButtonEntity):
    """Bouton pour confirmer un plein — calcul odometer précis en 2 étapes.

    Étape 1 (async_press) — immédiate :
        • Stocker plein_pending_at = now() (datetime)
        • Éteindre le switch "Faire le plein"
        • S'abonner à la prochaine fin de trajet confirmée (on_stop_confirmed) du coordinator

    Étape 2 (_on_stop_confirmed_for_plein) — déclenchée au verrouillage :
        • Appel API get_trips(plein_pending_at → now) → distance post-plein
        • odometer_au_plein = odometer_actuel - distance_post_plein
        • Calcul distance inter-plein
        • Rotation FIFO historique (hist_3 ← hist_2 ← hist_1 ← nouveau)
        • Recalcul moyenne glissante (max 3 pleins)
        • Mise à jour km_dernier_plein + nb_pleins_enregistres
        • Reset plein_pending_at = None (sentinel epoch 1970)
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
        self._prefix = self.tracker_name.lower().replace(" ", "_")

        self._attr_name = f"{self.tracker_name} Confirmer le plein"
        self._attr_unique_id = f"{self.tracker_id}_confirmer_plein"
        self._attr_icon = "mdi:gas-station-outline"

        # Gestion de l'abonnement au stop_confirmed
        self._unregister_stop_cb: callable | None = None

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.tracker_id)},
            name=f"{self.tracker_name} Trips",
            manufacturer="GeoRide",
            model=self._tracker.get("model", "GeoRide Tracker"),
            sw_version=str(self._tracker.get("softwareVersion", "")),
        )

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
        """Résoudre l'entity_id d'un number à partir de sa clé via l'entity registry."""
        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(self._hass)
        unique_id = f"{self.tracker_id}_{key}"
        return registry.async_get_entity_id("number", DOMAIN, unique_id)

    def _datetime_entity_id(self, key: str) -> str | None:
        """Résoudre l'entity_id d'un datetime à partir de sa clé via l'entity registry."""
        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(self._hass)
        unique_id = f"{self.tracker_id}_{key}"
        return registry.async_get_entity_id("datetime", DOMAIN, unique_id)

    def _get_number(self, key: str, default: float = 0.0) -> float:
        """Lire la valeur d'un number par sa clé (via entity registry)."""
        entity_id = self._number_entity_id(key)
        if entity_id is None:
            _LOGGER.warning(
                "%s: entity_id introuvable pour la clé number '%s'",
                self.tracker_name,
                key,
            )
            return default
        return self._get_float(entity_id, default)

    def _get_datetime(self, key: str) -> datetime | None:
        """Lire la valeur d'un datetime par sa clé. Retourne None si absent ou sentinel 1970."""
        entity_id = self._datetime_entity_id(key)
        if entity_id is None:
            _LOGGER.warning(
                "%s: entity_id introuvable pour la clé datetime '%s'",
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
            # Sentinel : epoch 1970 = pas de plein en attente
            if dt.year == 1970:
                return None
            return dt
        except (ValueError, AttributeError):
            return None

    async def _set_number(self, key: str, value: float) -> None:
        entity_id = self._number_entity_id(key)
        if entity_id is None:
            _LOGGER.error(
                "%s: entity_id introuvable pour la clé number '%s' (unique_id=%s_%s)",
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
        """Écrire un datetime par sa clé. None → sentinel epoch 1970."""
        entity_id = self._datetime_entity_id(key)
        if entity_id is None:
            _LOGGER.error(
                "%s: entity_id introuvable pour la clé datetime '%s' (unique_id=%s_%s)",
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
        """Annuler l'abonnement stop_confirmed en cours."""
        if self._unregister_stop_cb:
            self._unregister_stop_cb()
            self._unregister_stop_cb = None

    async def async_added_to_hass(self) -> None:
        """Au démarrage, traiter ou réinscrire un plein en attente s'il existe."""
        await super().async_added_to_hass()
        plein_pending = self._get_datetime("plein_pending_at")
        if plein_pending is not None:
            if self._is_tracker_locked():
                # Tracker verrouillé → le trajet post-plein est terminé, on peut calculer
                _LOGGER.info(
                    "%s: plein_pending_at non-null au démarrage (%s) et tracker verrouillé "
                    "— tentative de calcul différé",
                    self.tracker_name,
                    plein_pending.isoformat(),
                )
                self._hass.async_create_task(self._compute_and_record_plein())
            else:
                # Tracker déverrouillé → en cours de trajet, réinscrire le callback
                _LOGGER.info(
                    "%s: plein_pending_at non-null au démarrage (%s) et tracker déverrouillé "
                    "— réinscription attente verrouillage",
                    self.tracker_name,
                    plein_pending.isoformat(),
                )
                self._unregister_stop_cb = self._coordinator.on_stop_confirmed(
                    self._on_stop_confirmed_for_plein
                )

    # ── Helpers lock state ──────────────────────────────────────────────────

    def _is_tracker_locked(self) -> bool:
        """Vérifier si le tracker est actuellement verrouillé.

        Utilise le binary_sensor via le registry (à jour en temps réel via
        Socket.IO), ou fallback sur la propriété publique is_locked du
        coordinator (polling 5 min).
        binary_sensor lock : off = verrouillé, on = déverrouillé.
        """
        from .helpers import resolve_entity_id

        lock_entity = resolve_entity_id(
            self._hass, "binary_sensor", self.tracker_id, "verrouille"
        )
        if lock_entity:
            state = self._hass.states.get(lock_entity)
            if state and state.state not in ("unknown", "unavailable"):
                return state.state == "off"  # off = locked

        # Fallback : état public du coordinator (StatusCoordinator attaché)
        locked = self._coordinator.is_locked
        return bool(locked) if locked is not None else False

    # ── Étape 1 : press immédiat ──────────────────────────────────────────────

    async def async_press(self) -> None:
        """Étape 1 — Horodatage du plein + attendre le prochain verrouillage.

        Si le tracker est déjà verrouillé (plein confirmé à l'arrêt), le calcul
        est exécuté immédiatement sans attendre un verrouillage futur.
        """
        # Si un plein est déjà en attente, l'annuler proprement
        if self._unregister_stop_cb:
            _LOGGER.warning(
                "%s: nouveau press plein alors qu'un plein était déjà en attente — annulation",
                self.tracker_name,
            )
            self._cancel_pending()

        # Snapshot : horodatage du plein uniquement
        now = datetime.now(timezone.utc)

        await self._set_datetime("plein_pending_at", now)

        # Si le tracker est déjà verrouillé → calcul immédiat
        if self._is_tracker_locked():
            _LOGGER.info(
                "%s: plein enregistré à %s — tracker déjà verrouillé, calcul immédiat",
                self.tracker_name,
                now.strftime("%H:%M:%S"),
            )
            await self._compute_and_record_plein()
            return

        _LOGGER.info(
            "%s: plein enregistré à %s — attente verrouillage pour calcul précis",
            self.tracker_name,
            now.strftime("%H:%M:%S"),
        )

        # S'abonner au prochain verrouillage (one-shot)
        self._unregister_stop_cb = self._coordinator.on_stop_confirmed(
            self._on_stop_confirmed_for_plein
        )

    # ── Étape 2 : fin de trajet confirmée ────────────────────────────────────

    def _on_stop_confirmed_for_plein(self) -> None:
        """Callback appelé par le coordinator lors du verrouillage du tracker."""
        # _unregister_stop_cb est déjà consommé (one-shot), on nettoie la référence
        self._unregister_stop_cb = None
        self._hass.async_create_task(self._compute_and_record_plein())

    # ── Calcul au verrouillage ────────────────────────────────────────────────

    async def _compute_and_record_plein(self) -> None:
        """Calculer l'odometer au plein et enregistrer toutes les métriques.

        Logique :
        - plein_pending_at  = horodatage du plein (datetime UTC)
        - Refresh du coordinator pour s'assurer que l'odometer HA intègre le trajet
        - Appel API get_trips(plein_pending_at → now) → distance parcourue APRÈS le plein
        - odometer_au_plein = odometer_actuel - distance_post_plein
        """
        from .const import METERS_TO_KM
        from .helpers import resolve_entity_id

        plein_dt = self._get_datetime("plein_pending_at")
        km_dernier_plein = self._get_number("km_dernier_plein")

        # ── Refresh coordinator pour garantir que l'odometer est à jour ───────
        # Sans ce refresh, le trajet venant de se terminer n'est pas encore intégré
        # dans sensor.real_odometer, ce qui fausserait le calcul odometer_au_plein.
        _LOGGER.debug(
            "%s: refresh coordinator avant lecture odometer (garantir intégration du trajet)",
            self.tracker_name,
        )
        await self._coordinator.async_request_refresh()

        odometer_entity = resolve_entity_id(
            self._hass, "sensor", self.tracker_id, "real_odometer"
        )
        odometer_actuel = self._get_float(odometer_entity) if odometer_entity else 0.0

        if plein_dt is None:
            _LOGGER.warning(
                "%s: _compute_and_record_plein appelé sans plein_pending_at valide",
                self.tracker_name,
            )
            return

        if odometer_actuel <= 0:
            _LOGGER.error(
                "%s: odometer actuel invalide (%.1f), abandon",
                self.tracker_name,
                odometer_actuel,
            )
            await self._set_datetime("plein_pending_at", None)
            return

        # ── Distance parcourue APRÈS le plein (plein_pending_at → maintenant) ──
        now_dt = datetime.now(timezone.utc)
        distance_post_plein = await self._fetch_post_plein_distance(
            plein_dt, now_dt, METERS_TO_KM
        )

        odometer_au_plein = round(odometer_actuel - distance_post_plein, 2)
        _LOGGER.info(
            "%s: odometer plein = %.1f km (actuel=%.1f - post_plein=%.1f km)",
            self.tracker_name,
            odometer_au_plein,
            odometer_actuel,
            distance_post_plein,
        )

        if odometer_au_plein <= 0:
            _LOGGER.error(
                "%s: odometer au plein invalide (%.1f), abandon",
                self.tracker_name,
                odometer_au_plein,
            )
            await self._set_datetime("plein_pending_at", None)
            return

        # ── Premier plein : juste snapshot, pas de calcul inter-plein ─────
        if km_dernier_plein == 0:
            _LOGGER.info(
                "%s: premier plein — snapshot odometer = %.1f km",
                self.tracker_name,
                odometer_au_plein,
            )
            await self._set_number("km_dernier_plein", odometer_au_plein)
            await self._set_number("nb_pleins_enregistres", 1)
            await self._set_datetime("plein_pending_at", None)
            return

        # ── Calcul distance inter-plein ────────────────────────────────────
        distance_inter_plein = round(odometer_au_plein - km_dernier_plein, 1)
        if distance_inter_plein <= 0:
            _LOGGER.warning(
                "%s: distance inter-plein négative (%.1f km), abandon",
                self.tracker_name,
                distance_inter_plein,
            )
            await self._set_datetime("plein_pending_at", None)
            return

        # ── Rotation FIFO historique ────────────────────────────────────────
        hist_1 = self._get_number("km_plein_hist_1")
        hist_2 = self._get_number("km_plein_hist_2")

        await self._set_number("km_plein_hist_3", hist_2)
        await self._set_number("km_plein_hist_2", hist_1)
        await self._set_number("km_plein_hist_1", distance_inter_plein)

        # ── Moyenne glissante (slots non-nuls, max HIST_SLOTS) ─────────────
        slots = [s for s in [distance_inter_plein, hist_1, hist_2] if s > 0]
        slots = slots[: self.HIST_SLOTS]
        moyenne = round(sum(slots) / len(slots))

        nb_pleins = int(self._get_number("nb_pleins_enregistres")) + 1

        await self._set_number("autonomie_moyenne_calculee", moyenne)
        await self._set_number("nb_pleins_enregistres", nb_pleins)
        await self._set_number("km_dernier_plein", odometer_au_plein)
        await self._set_datetime("plein_pending_at", None)

        _LOGGER.info(
            "%s: plein confirmé — odometer=%.1f km, inter-plein=%.1f km, "
            "moyenne=%d km (%d valeur(s)), nb_pleins=%d",
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
        """Appeler l'API get_trips entre plein_pending_at et maintenant.

        Retourne la distance parcourue APRÈS le plein en km, ou 0.0 si l'API échoue.
        """
        try:
            _LOGGER.debug(
                "%s: fetch trips post-plein : %s → %s",
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
                _LOGGER.warning("%s: API get_trips a retourné None", self.tracker_name)
                return 0.0

            distance_km = round(
                sum(t.get("distance", 0) for t in trips) / meters_to_km, 2
            )

            _LOGGER.info(
                "%s: distance post-plein API = %.1f km (%d segment(s) entre %s et %s)",
                self.tracker_name,
                distance_km,
                len(trips),
                plein_dt.strftime("%H:%M"),
                now_dt.strftime("%H:%M"),
            )
            return distance_km

        except Exception as err:
            _LOGGER.error(
                "%s: erreur fetch distance post-plein : %s",
                self.tracker_name,
                err,
            )
            return 0.0


class GeoRideAppliquerAutonomieButton(ButtonEntity):
    """Bouton pour appliquer l'autonomie moyenne calculée comme autonomie de référence.

    `button.<moto>_appliquer_autonomie_calculee`

    Copie la valeur de `number.<moto>_carburant_autonomie_moyenne_calculee`
    dans `number.<moto>_autonomie_totale`.

    Utilisable :
    - Manuellement depuis l'UI HA
    - Via le blueprint après notification de nouvelle moyenne disponible
      (action mobile ✅ Appliquer)

    Pré-condition : nb_pleins_enregistres >= 2 et autonomie_moyenne_calculee > 0.
    Si non satisfaite, log un warning et ne fait rien.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, tracker: dict) -> None:
        self._hass = hass
        self._entry = entry
        self._tracker = tracker

        self.tracker_id = str(tracker.get("trackerId"))
        self.tracker_name = tracker.get("trackerName", f"Tracker {self.tracker_id}")
        self._prefix = self.tracker_name.lower().replace(" ", "_")

        self._attr_name = f"{self.tracker_name} Appliquer autonomie calculée"
        self._attr_unique_id = f"{self.tracker_id}_appliquer_autonomie_calculee"
        self._attr_icon = "mdi:check-circle-outline"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.tracker_id)},
            name=f"{self.tracker_name} Trips",
            manufacturer="GeoRide",
            model=self._tracker.get("model", "GeoRide Tracker"),
            sw_version=str(self._tracker.get("softwareVersion", "")),
        )

    def _get_float(self, entity_id: str, default: float = 0.0) -> float:
        state = self._hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return default
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return default

    async def async_press(self) -> None:
        """Copier autonomie_moyenne_calculee → autonomie_totale."""
        from .helpers import resolve_entity_id

        entity_moyenne = resolve_entity_id(
            self._hass, "number", self.tracker_id, "autonomie_moyenne_calculee"
        )
        entity_nb_pleins = resolve_entity_id(
            self._hass, "number", self.tracker_id, "nb_pleins_enregistres"
        )
        entity_totale = resolve_entity_id(
            self._hass, "number", self.tracker_id, "autonomie_totale"
        )

        if not entity_moyenne or not entity_nb_pleins or not entity_totale:
            _LOGGER.error(
                "%s: impossible de résoudre les entités carburant via le registry",
                self.tracker_name,
            )
            return

        nb_pleins = self._get_float(entity_nb_pleins)
        moyenne = self._get_float(entity_moyenne)

        if nb_pleins < 2 or moyenne <= 0:
            _LOGGER.warning(
                "%s: impossible d'appliquer l'autonomie calculée "
                "(nb_pleins=%.0f, moyenne=%.1f km) — besoin d'au moins 2 pleins.",
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
            "%s: autonomie_totale mise à jour → %.1f km (moyenne sur %.0f pleins)",
            self.tracker_name,
            moyenne,
            nb_pleins,
        )
