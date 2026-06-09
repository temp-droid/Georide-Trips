# 🏍️ GeoRide Trips — Intégration Home Assistant

[![Version](https://img.shields.io/badge/version-2.5-blue.svg)](https://github.com/druide93/Georide-Trips)
[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![HA Version](https://img.shields.io/badge/Home%20Assistant-2024.1+-green.svg)](https://www.home-assistant.io/)

Intégration Home Assistant complète pour les trackers GPS **GeoRide**, permettant le suivi des trajets moto, le calcul d'odometer corrigé, la gestion de l'entretien (chaîne, vidange, révision), le suivi de l'autonomie carburant et les alertes de sécurité en temps réel.

---

## ✨ Fonctionnalités

| Domaine | Fonctionnalité |
|---|---|
| 🗺️ **Trajets** | Historique des 30 derniers jours, dernier trajet détaillé, notification à l'arrêt |
| 🔢 **Odometer** | Kilométrage réel avec offset configurable (km avant l'installation du tracker) |
| 📅 **Kilométrage périodique** | Compteurs journaliers, hebdomadaires et mensuels calculés automatiquement |
| ⛽ **Carburant** | Autonomie restante avec moyenne glissante sur 3 pleins, alerte sous seuil |
| 🔗 **Entretien chaîne** | Suivi km depuis le dernier entretien, alerte sous seuil configurable |
| 🛢️ **Vidange** | Suivi km depuis la dernière vidange, alerte sous seuil configurable |
| 🔧 **Révision** | Double critère km **et** jours, alerte dès que l'un des deux seuils est atteint |
| 🚨 **Sécurité** | Alarme vol, chute détectée, position en temps réel via Socket.IO |
| 🔋 **Batterie** | Niveau de batterie externe (moto) et interne (tracker) |
| 📡 **Temps réel** | Connexion Socket.IO pour mises à jour instantanées (mouvement, alarmes) |
| 🌿 **Mode éco** | Activation/désactivation du mode éco du tracker depuis HA |
| 🔒 **Verrouillage** | Verrouillage/déverrouillage du tracker à distance depuis HA |

---

## 🏗️ Architecture

L'intégration repose sur une **architecture hybride** combinant :

- **Socket.IO** (`socket.georide.com`) : mises à jour temps réel pour la position, le mouvement et les alarmes (vol, chute). La latence est quasi nulle.
- **Polling HTTP** (`api.georide.fr`) via trois coordinators indépendants :
  - **Trips Coordinator** : récupère les trajets des 30 derniers jours (polling toutes les heures par défaut). Déclenche un refresh immédiat dès qu'un nouveau trajet est détecté.
  - **Lifetime Coordinator** : cumule le kilométrage total à vie via l'API `/trips` (polling toutes les 24h). Se rafraîchit à minuit et dès qu'un nouveau trajet est détecté.
  - **Status Coordinator** : récupère l'état du tracker (batterie, statut ligne, mode éco, verrouillage) via `/user/trackers` (polling toutes les 5 minutes).

```
GeoRide API ──────► Trips Coordinator    (1h)  ──► Trajets, odometer récent
              ├───► Lifetime Coordinator  (24h) ──► Odometer total à vie
              └───► Status Coordinator   (5min) ──► Batterie, statut, verrouillage, mode éco

socket.georide.com ──► Socket.IO ──► Position, mouvement, alarmes (temps réel)
```

### Détection de fin de trajet

La fin de trajet est détectée par la transition `isLocked: False → True` du **Status Coordinator** (polling 5 min). Cette approche est plus fiable que la détection de fin de mouvement via Socket.IO, qui peut être interrompue par des arrêts temporaires (feux rouges, embouteillages).

### Snapshots kilométriques automatiques

Un `GeoRideMidnightSnapshotManager` natif Python met à jour automatiquement les snapshots sans intervention du blueprint :
- Chaque nuit à minuit → `km_debut_journee`
- Chaque lundi à minuit → `km_debut_semaine`
- Au jour configuré chaque mois → `km_debut_mois`

---

## 📦 Installation

### Via HACS (recommandé)

1. Dans HACS, aller dans **Intégrations** → menu ⋮ → **Dépôts personnalisés**
2. Ajouter `https://github.com/druide93/Georide-Trips` avec la catégorie **Intégration**
3. Rechercher **GeoRide Trips** et installer
4. Redémarrer Home Assistant

### Manuel

1. Copier le dossier `georide_trips` dans `config/custom_components/`
2. Redémarrer Home Assistant

### Configuration

1. Aller dans **Paramètres → Appareils et services → Ajouter une intégration**
2. Rechercher **GeoRide Trips**
3. Saisir l'email et le mot de passe du compte GeoRide
4. L'intégration crée automatiquement un **appareil par tracker** détecté sur le compte

> **Limitation** : une seule instance (un seul compte GeoRide) est supportée.
> Plusieurs trackers sur le même compte fonctionnent normalement.

#### Options avancées (configurables après installation)

| Option | Défaut | Description |
|---|---|---|
| Socket.IO activé | `true` | Active les mises à jour temps réel |
| Polling statut tracker | `300 s` | Intervalle de rafraîchissement batterie/statut/verrouillage (1 min – 1h) |
| Polling trajets | `3600 s` | Intervalle de rafraîchissement des trajets (5 min – 24h) |
| Polling lifetime | `86400 s` | Intervalle de rafraîchissement de l'odometer total (1h – 7j) |
| Historique trajets | `30 jours` | Fenêtre temporelle des trajets récupérés (1–365 jours) |
| Précision GPS minimale | `0 m` | Rayon max accepté en mètres — 0 = filtre désactivé |

---

## 📊 Entités créées par tracker

### Sensors (`sensor.*`)

#### Trajets
| Entité | Description | Unité |
|---|---|---|
| `*_last_trip` | Dernier trajet (état : distance en km) | km |
| `*_last_trip_details` | Détails du dernier trajet (attributs complets) | — |
| `*_total_distance` | Distance totale des trajets récents (fenêtre configurée) | km |
| `*_trip_count` | Nombre de trajets sur la période | — |

#### Kilométrage
| Entité | Description | Unité |
|---|---|---|
| `*_lifetime_odometer` | Kilométrage total brut depuis l'installation du tracker | km |
| `*_odometer` | Odometer réel = lifetime + offset (km avant installation) | km |
| `*_km_journaliers` | Km parcourus depuis minuit | km |
| `*_km_hebdomadaires` | Km parcourus depuis lundi minuit | km |
| `*_km_mensuels` | Km parcourus depuis le jour de reset mensuel configuré | km |

#### Entretien
| Entité | Description | Unité |
|---|---|---|
| `*_km_restants_chaine` | Km restants avant le prochain entretien chaîne | km |
| `*_km_restants_vidange` | Km restants avant la prochaine vidange | km |
| `*_km_restants_revision` | Km restants avant la prochaine révision | km |
| `*_jours_restants_revision` | Jours restants avant la prochaine révision | jours |

#### Carburant
| Entité | Description | Unité |
|---|---|---|
| `*_autonomie_restante` | Km restants estimés sur le plein actuel | km |

#### Tracker
| Entité | Description | Unité |
|---|---|---|
| `*_tracker_status` | Statut du tracker (online / offline) | — |
| `*_external_battery` | Tension de la batterie externe (moto) | V |
| `*_internal_battery` | Niveau de batterie interne (tracker) | % |
| `*_last_alarm` | Dernière alarme reçue via Socket.IO | — |

### Binary Sensors (`binary_sensor.*`)

| Entité | Source | Description |
|---|---|---|
| `*_en_mouvement` | Socket.IO | `on` si la moto est en mouvement |
| `*_alarme_vol` | Socket.IO | `on` si l'alarme antivol est active |
| `*_chute_detectee` | Socket.IO | `on` si une chute est détectée |
| `*_online` | Status Coordinator | `on` si le tracker est connecté |
| `*_locked` | Status Coordinator | `on` si le tracker est verrouillé |
| `*_plein_requis` | Calculé | `on` si l'autonomie restante < seuil d'alerte |
| `*_entretien_chaine_requis` | Calculé | `on` si km restants chaîne < seuil d'alerte |
| `*_vidange_requise` | Calculé | `on` si km restants vidange < seuil d'alerte |
| `*_revision_requise` | Calculé | `on` si km restants révision < seuil d'alerte |

> Les binary sensors d'entretien et carburant sont **calculés en temps réel** en Python. Le blueprint déclenche les notifications sur la transition `off → on`, ce qui garantit une notification unique par franchissement de seuil.

### Switches (`switch.*`)

| Entité | Description |
|---|---|
| `*_mode_eco` | Active / désactive le mode éco du tracker via l'API |
| `*_verrouillage` | Verrouille / déverrouille le tracker à distance via l'API |

### Buttons (`button.*`)

| Entité | Action |
|---|---|
| `*_refresh_trips` | Force le rafraîchissement des trajets récents |
| `*_refresh_odometer` | Force le rafraîchissement du kilométrage lifetime |
| `*_confirmer_le_plein` | Enregistre le plein (odometer précis + historique inter-plein) |
| `*_appliquer_autonomie_calculee` | Copie la moyenne glissante calculée dans l'autonomie totale manuelle |
| `*_enregistrer_entretien_chaine` | Enregistre le dernier entretien chaîne (odometer + date) |
| `*_enregistrer_vidange` | Enregistre la dernière vidange (odometer + date) |
| `*_enregistrer_revision` | Enregistre la dernière révision (odometer + date) |

### Numbers (`number.*`)

#### Configuration odometer
| Entité | Description | Défaut |
|---|---|---|
| `*_odometer_offset` | Km à ajouter à l'odometer tracker (km avant installation) | 0 km |

#### Configuration carburant
| Entité | Description | Défaut |
|---|---|---|
| `*_autonomie_totale` | Autonomie théorique sur un plein | 150 km |
| `*_seuil_alerte_autonomie` | Seuil d'alerte autonomie | 30 km |
| `*_km_dernier_plein` | Odometer au dernier plein (stockage) | — |
| `*_km_plein_hist_1` | Distance inter-plein n-1 (historique FIFO) | — |
| `*_km_plein_hist_2` | Distance inter-plein n-2 (historique FIFO) | — |
| `*_km_plein_hist_3` | Distance inter-plein n-3 (historique FIFO) | — |
| `*_autonomie_moyenne_calculee` | Moyenne glissante sur les 3 derniers pleins | — |
| `*_nb_pleins_enregistres` | Compteur total de pleins confirmés | — |

#### Configuration entretien chaîne
| Entité | Description | Défaut |
|---|---|---|
| `*_intervalle_km_chaine` | Km entre deux entretiens | 500 km |
| `*_seuil_alerte_chaine` | Km avant échéance pour alerter | 100 km |
| `*_km_dernier_entretien_chaine` | Odometer au dernier entretien (stockage) | — |

#### Configuration vidange
| Entité | Description | Défaut |
|---|---|---|
| `*_intervalle_km_vidange` | Km entre deux vidanges | 6000 km |
| `*_seuil_alerte_vidange` | Km avant échéance pour alerter | 500 km |
| `*_km_derniere_vidange` | Odometer à la dernière vidange (stockage) | — |

#### Configuration révision
| Entité | Description | Défaut |
|---|---|---|
| `*_intervalle_km_revision` | Km entre deux révisions | 12000 km |
| `*_intervalle_jours_revision` | Jours max entre révisions | 365 jours |
| `*_seuil_alerte_revision` | Km avant échéance pour alerter | 1000 km |
| `*_km_derniere_revision` | Odometer à la dernière révision (stockage) | — |

#### Configuration kilométrage périodique
| Entité | Description |
|---|---|
| `*_seuil_distance_trajet` | Distance minimale pour notifier un trajet |
| `*_jour_stats_mensuelles` | Jour du mois pour le reset des stats mensuelles (1–28) |
| `*_km_debut_journee` | Snapshot odometer à minuit (mis à jour automatiquement) |
| `*_km_debut_semaine` | Snapshot odometer lundi minuit (mis à jour automatiquement) |
| `*_km_debut_mois` | Snapshot odometer au jour configuré (mis à jour automatiquement) |

### Datetimes (`datetime.*`)

| Entité | Description |
|---|---|
| `*_date_dernier_entretien_chaine` | Date du dernier entretien chaîne |
| `*_date_derniere_vidange` | Date de la dernière vidange |
| `*_date_derniere_revision` | Date de la dernière révision |

### Device Tracker (`device_tracker.*`)

| Entité | Description |
|---|---|
| `*_position` | Position GPS en temps réel de la moto |

---

## 🤖 Blueprint d'automatisation

L'intégration est livrée avec un **blueprint complet** (`georide-trips.yaml` — v28.1) gérant l'ensemble des notifications et de la logique métier. **Créer une instance par moto.**

### Fonctionnalités du blueprint

**⛽ Carburant**
- Notification push quand le binary sensor `plein_requis` passe à `on`
- Enregistrement automatique du plein via le bouton **Confirmer plein** : odometer précis capturé à la fin du trajet vers la station (après verrouillage du tracker)
- Calcul de la moyenne glissante sur les 3 derniers pleins
- Notification proposant d'appliquer la nouvelle autonomie calculée via le bouton **Appliquer autonomie calculée**

**🗺️ Nouveau trajet**
- Notification à chaque arrêt si la distance dépasse le seuil configuré
- Contenu : distance, durée, vitesse moyenne, vitesse max, adresse de départ/arrivée
- Déclenchement sur verrouillage du tracker (plus fiable que la détection de mouvement)
- Fallback automatique sur changement du capteur de dernier trajet

**🔗 Entretien chaîne / 🛢️ Vidange / 🔧 Révision**
- Notification unique à la transition `off → on` du binary sensor correspondant
- Aucune notification en double lors des redémarrages de HA

**📅 Kilométrage périodique**
- Bilans hebdomadaires et mensuels en notification push et/ou persistante

**🚨 Sécurité**
- Notification immédiate en cas d'alarme vol ou de chute détectée

### Installation du blueprint

1. Copier `georide-trips.yaml` dans `config/blueprints/automation/georide_trips/`
2. Dans HA : **Paramètres → Automatisations → Blueprints**
3. Créer une automatisation depuis le blueprint **Moto GeoRide - Suivi complet**
4. Configurer les entités de chaque section (moto, capteurs, notifications…)

---

## 🔧 Calcul de l'odometer

Le tracker GeoRide ne comptabilise les km qu'à partir de sa **date d'installation**, pas depuis l'origine de la moto. L'entité `*_odometer` applique un **offset** pour restituer le kilométrage réel :

```
Odometer réel = Lifetime tracker (km depuis installation) + Offset (km avant installation)
```

L'offset est configurable directement depuis l'interface HA via `number.*_odometer_offset`. Toutes les entités d'entretien et de carburant utilisent cet odometer corrigé.

---

## ⛽ Workflow carburant

1. L'utilisateur fait le plein et appuie sur **Confirmer plein**
2. Le système attend la fin du trajet retour (verrouillage du tracker)
3. L'odomètre au plein est calculé : `odometer_actuel − distance_post_plein`
4. La distance inter-plein est enregistrée dans l'historique FIFO (3 valeurs)
5. La moyenne glissante est recalculée
6. Une notification propose d'appliquer la nouvelle autonomie via le bouton **Appliquer autonomie calculée**

> L'autonomie totale ne se met **jamais à jour automatiquement** — l'utilisateur garde le contrôle total.

---

## 📋 Prérequis

- Home Assistant 2024.1 ou supérieur
- Un compte GeoRide avec au moins un tracker actif
- Application **Home Assistant Companion** (pour les notifications push)
- Python 3.11+

### Dépendances Python (installées automatiquement)

- `aiohttp >= 3.8.0`
- `python-socketio[asyncio_client] >= 5.0`

---

## 🌐 Endpoints API utilisés

| Endpoint | Usage |
|---|---|
| `POST /user/login` | Authentification |
| `GET /user/trackers` | Liste des trackers + statut |
| `GET /tracker/{id}/trips` | Historique des trajets |
| `GET /tracker/{id}/trip/{trip_id}/positions` | Positions d'un trajet |
| `PUT /tracker/{id}/eco-mode/on` | Activer le mode éco |
| `PUT /tracker/{id}/eco-mode/off` | Désactiver le mode éco |
| `POST /tracker/{id}/toggleLock` | Verrouiller / déverrouiller le tracker |
| `POST /tracker/{id}/sonor-alarm/off` | Arrêter l'alarme sonore |
| `Socket.IO socket.georide.com` | Événements temps réel |

---

## 🛠️ Dépannage

**Le kilométrage lifetime ne se met pas à jour**
Vérifier que le coordinator lifetime n'est pas en erreur dans les logs. Le refresh est déclenché à minuit et après chaque nouveau trajet.

**L'odometer est incorrect**
Configurer `number.*_odometer_offset` avec le kilométrage de la moto au moment de l'installation du tracker.

**Les notifications d'entretien se répètent**
Le blueprint déclenche les notifications sur la transition `off → on` des binary sensors. Vérifier dans les traces d'automatisation que le binary sensor repasse bien à `off` après confirmation d'entretien.

**Socket.IO se déconnecte fréquemment**
Normal en cas de réseau instable — le polling HTTP prend le relais automatiquement. Désactiver Socket.IO dans les options si la connexion est trop instable.

**Le capteur "En mouvement" reste bloqué à `on`**
Le `StatusCoordinator` (polling 5 min) détecte automatiquement l'état réel et force le retour à `off`. Le délai maximum de correction est de 5 minutes.

**Les entités n'apparaissent pas après installation**
S'assurer que le dossier s'appelle exactement `georide_trips` et redémarrer Home Assistant complètement (pas seulement recharger la configuration).

**Les positions GPS sont imprécises**
Configurer le filtre GPS dans les options (`Précision GPS minimale`) pour ignorer les positions dont le rayon de précision dépasse le seuil défini (ex. 50 m).

---

## 📄 Licence

MIT License — Voir [LICENSE](LICENSE) pour les détails.

---

## 🤝 Contribution

Les issues et pull requests sont les bienvenus sur [GitHub](https://github.com/druide93/Georide-Trips).


> **Note** : Ce projet n'est pas affilié à GeoRide. GeoRide est une marque déposée de GeoRide SAS.
