# 🏍️ GeoRide Trips — Home Assistant Integration

[![Version](https://img.shields.io/badge/version-2.6-blue.svg)](https://github.com/temp-droid/Georide-Trips)
[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![HA Version](https://img.shields.io/badge/Home%20Assistant-2024.6+-green.svg)](https://www.home-assistant.io/)

> **🍴 This is a personal fork.** The original **GeoRide Trips** integration was created by **[druide93](https://github.com/druide93/Georide-Trips)** — all the hard work of reverse-engineering the GeoRide API and building the integration is his. This fork only adds incremental changes on top (English translation, adaptive drivetrain maintenance, internal refactors, reauth flow). It is a **personal copy, not actively maintained**, provided as-is and **not accepting contributions** — for the canonical, maintained project, please use [druide93/Georide-Trips](https://github.com/druide93/Georide-Trips).

Complete Home Assistant integration for **GeoRide** GPS trackers, providing motorcycle trip tracking, corrected odometer calculation, maintenance management (drivetrain, oil change, service), fuel range tracking and real-time security alerts.

> **⚠️ Breaking change (2.6)**: all maintenance and fuel entity `unique_id`s were renamed from French to English (e.g. `intervalle_km_chaine` → `drivetrain_km_interval`). On upgrade, Home Assistant creates new entities and previously stored maintenance intervals, last-service dates and refuel history reset to defaults — note your values before upgrading and re-enter them. The `odometer_offset` is **not** affected and survives the upgrade.

---

## ✨ Features

| Domain | Feature |
|---|---|
| 🗺️ **Trips** | History of the last 30 days, detailed last trip, notification on stop |
| 🔢 **Odometer** | Real mileage with configurable offset (km before the tracker was installed) |
| 📅 **Periodic mileage** | Daily, weekly and monthly counters computed automatically |
| ⛽ **Fuel** | Remaining range with a rolling average over 3 refuels, alert below threshold |
| 🔗 **Drivetrain maintenance** | Adaptive slot (chain / shaft / belt) — label, intervals and time dimension adapt to the configured drive type |
| 🛢️ **Oil change** | Tracks km since the last oil change, alert below configurable threshold |
| 🔧 **Service** | Dual criterion km **and** days, alert as soon as either threshold is reached |
| 🚨 **Security** | Theft alarm, fall detected, real-time position via Socket.IO |
| 🔋 **Battery** | External battery level (motorcycle) and internal (tracker) |
| 📡 **Real time** | Socket.IO connection for instant updates (movement, alarms) |
| 🌿 **Eco mode** | Enable/disable the tracker's eco mode from HA |
| 🔒 **Locking** | Lock/unlock the tracker remotely from HA |
| 🔑 **Reauth** | If the GeoRide password changes, HA prompts you to re-enter it instead of silently failing |

---

## 🏗️ Architecture

The integration relies on a **hybrid architecture** combining:

- **Socket.IO** (`socket.georide.com`): real-time updates for position, movement and alarms (theft, fall). Latency is near zero.
- **HTTP Polling** (`api.georide.fr`) via three independent coordinators:
  - **Trips Coordinator**: fetches the trips from the last 30 days (polling every hour by default). Triggers an immediate refresh as soon as a new trip is detected.
  - **Lifetime Coordinator**: accumulates the total lifetime mileage via the `/trips` API (polling every 24h). Refreshes at midnight and as soon as a new trip is detected.
  - **Status Coordinator**: fetches the tracker state (battery, line status, eco mode, locking) via `/user/trackers` (polling every 5 minutes).

```
GeoRide API ──────► Trips Coordinator    (1h)  ──► Trips, recent odometer
              ├───► Lifetime Coordinator  (24h) ──► Lifetime odometer
              └───► Status Coordinator   (5min) ──► Battery, status, lock, eco mode

socket.georide.com ──► Socket.IO ──► Position, movement, alarms (real time)
```

### End-of-trip detection

The end of a trip is detected by the `isLocked: False → True` transition of the **Status Coordinator** (5 min polling). This approach is more reliable than detecting the end of movement via Socket.IO, which can be interrupted by temporary stops (red lights, traffic jams).

### Automatic mileage snapshots

A native Python `GeoRideMidnightSnapshotManager` automatically updates the snapshots without any blueprint intervention:
- Every night at midnight → `odometer_at_day_start`
- Every Monday at midnight → `odometer_at_week_start`
- On the configured day each month → `odometer_at_month_start`

---

## 📦 Installation

### Via HACS (recommended)

1. In HACS, go to **Integrations** → ⋮ menu → **Custom repositories**
2. Add `https://github.com/temp-droid/Georide-Trips` with the **Integration** category
3. Search for **GeoRide Trips** and install
4. Restart Home Assistant

### Manual

1. Copy the `georide_trips` folder into `config/custom_components/`
2. Restart Home Assistant

### Configuration

1. Go to **Settings → Devices & services → Add integration**
2. Search for **GeoRide Trips**
3. Enter the email and password of the GeoRide account
4. The integration automatically creates **one device per tracker** detected on the account

> **Limitation**: only a single instance (a single GeoRide account) is supported.
> Multiple trackers on the same account work normally.

#### Advanced options (configurable after installation)

| Option | Default | Description |
|---|---|---|
| Socket.IO enabled | `true` | Enables real-time updates |
| Tracker status polling | `300 s` | Battery/status/locking refresh interval (1 min – 1h) |
| Trips polling | `3600 s` | Trips refresh interval (5 min – 24h) |
| Lifetime polling | `86400 s` | Total odometer refresh interval (1h – 7d) |
| Trip history | `30 days` | Time window of fetched trips (1–365 days) |
| Minimum GPS accuracy | `0 m` | Max accepted radius in meters — 0 = filter disabled |
| Minimum GPS distance | `10 m` | Positions closer than this are ignored (GPS micro-drift filter) — 0 = disabled |
| Drivetrain type | `chain` | `chain` (800 km), `shaft` (final drive oil — 40000 km or 3 years) or `belt` (16000 km); adapts the drivetrain maintenance entities |

---

## 📊 Entities created per tracker

### Sensors (`sensor.*`)

#### Trips
| Entity | Description | Unit |
|---|---|---|
| `*_last_trip` | Last trip (state: distance in km) | km |
| `*_last_trip_details` | Details of the last trip (full attributes) | — |
| `*_total_distance` | Total distance of recent trips (configured window) | km |
| `*_trip_count` | Number of trips over the period | — |

#### Mileage
| Entity | Description | Unit |
|---|---|---|
| `*_lifetime_odometer` | Total raw mileage since the tracker was installed | km |
| `*_odometer` | Real odometer = lifetime + offset (km before installation) | km |
| `*_daily_mileage` | Km traveled since midnight | km |
| `*_weekly_mileage` | Km traveled since Monday midnight | km |
| `*_monthly_mileage` | Km traveled since the configured monthly reset day | km |

#### Maintenance
| Entity | Description | Unit |
|---|---|---|
| `*_drivetrain_remaining_km` | Km remaining before the next drivetrain maintenance | km |
| `*_drivetrain_remaining_days` | Days remaining before the next drivetrain maintenance (shaft only) | days |
| `*_oil_change_remaining_km` | Km remaining before the next oil change | km |
| `*_service_remaining_km` | Km remaining before the next service | km |
| `*_service_remaining_days` | Days remaining before the next service | days |

#### Fuel
| Entity | Description | Unit |
|---|---|---|
| `*_remaining_range` | Estimated km remaining on the current tank | km |

#### Tracker
| Entity | Description | Unit |
|---|---|---|
| `*_tracker_status` | Tracker status (online / offline) | — |
| `*_external_battery` | External battery voltage (motorcycle) | V |
| `*_internal_battery` | Internal battery level (tracker) | % |
| `*_last_alarm` | Last alarm received via Socket.IO | — |

### Binary Sensors (`binary_sensor.*`)

| Entity | Source | Description |
|---|---|---|
| `*_moving` | Socket.IO | `on` if the motorcycle is moving |
| `*_stolen` | Socket.IO | `on` if the anti-theft alarm is active |
| `*_crashed` | Socket.IO | `on` if a fall is detected |
| `*_online` | Status Coordinator | `on` if the tracker is connected |
| `*_locked` | Status Coordinator | `on` if the tracker is locked |
| `*_refuel_needed` | Computed | `on` if the remaining range < alert threshold |
| `*_drivetrain_due` | Computed | `on` if remaining drivetrain km (or days, shaft) < alert threshold |
| `*_oil_change_due` | Computed | `on` if remaining oil-change km < alert threshold |
| `*_service_due` | Computed | `on` if remaining service km or days < alert threshold |

> The maintenance and fuel binary sensors are **computed in real time** in Python. The blueprint triggers notifications on the `off → on` transition, which guarantees a single notification per threshold crossing.

### Switches (`switch.*`)

| Entity | Description |
|---|---|
| `*_eco_mode` | Enable / disable the tracker's eco mode via the API |
| `*_lock` | Lock / unlock the tracker remotely via the API |

### Buttons (`button.*`)

| Entity | Action |
|---|---|
| `*_refresh_trips` | Force a refresh of the recent trips |
| `*_refresh_odometer` | Force a refresh of the lifetime mileage |
| `*_confirm_refuel` | Record the refuel (precise odometer + inter-refuel history) |
| `*_apply_calculated_range` | Copy the computed rolling average into the manual total range |
| `*_record_drivetrain` | Record the last drivetrain maintenance (odometer + date) |
| `*_record_oil_change` | Record the last oil change (odometer + date) |
| `*_record_service` | Record the last service (odometer + date) |

### Numbers (`number.*`)

#### Odometer configuration
| Entity | Description | Default |
|---|---|---|
| `*_odometer_offset` | Km to add to the tracker odometer (km before installation) | 0 km |

#### Fuel configuration
| Entity | Description | Default |
|---|---|---|
| `*_fuel_total_range` | Theoretical range on a full tank | 150 km |
| `*_fuel_range_alert_threshold` | Range alert threshold | 30 km |
| `*_fuel_km_at_last_refuel` | Odometer at the last refuel (storage) | — |
| `*_fuel_distance_between_refuels_1` | Inter-refuel distance n-1 (FIFO history) | — |
| `*_fuel_distance_between_refuels_2` | Inter-refuel distance n-2 (FIFO history) | — |
| `*_fuel_distance_between_refuels_3` | Inter-refuel distance n-3 (FIFO history) | — |
| `*_fuel_calculated_average_range` | Rolling average over the last 3 refuels | — |
| `*_fuel_recorded_refuel_count` | Total counter of confirmed refuels | — |

#### Drivetrain maintenance configuration

The defaults adapt to the configured drivetrain type (chain 800 km, shaft 40000 km + 1095 days, belt 16000 km).

| Entity | Description | Default (chain) |
|---|---|---|
| `*_drivetrain_km_interval` | Km between two maintenances | 800 km |
| `*_drivetrain_day_interval` | Max days between maintenances (0 = km-only, shaft: 1095) | 0 days |
| `*_drivetrain_alert_threshold` | Km before due date to alert | 150 km |
| `*_drivetrain_km_at_last_service` | Odometer at the last maintenance (storage) | — |

#### Oil change configuration
| Entity | Description | Default |
|---|---|---|
| `*_oil_change_km_interval` | Km between two oil changes | 6000 km |
| `*_oil_change_alert_threshold` | Km before due date to alert | 500 km |
| `*_oil_change_km_at_last_oil_change` | Odometer at the last oil change (storage) | — |

#### Service configuration
| Entity | Description | Default |
|---|---|---|
| `*_service_km_interval` | Km between two services | 12000 km |
| `*_service_day_interval` | Max days between services | 365 days |
| `*_service_alert_threshold` | Km before due date to alert | 1000 km |
| `*_service_km_at_last_service` | Odometer at the last service (storage) | — |

#### Periodic mileage configuration
| Entity | Description |
|---|---|
| `*_trip_notification_threshold` | Minimum distance to notify a trip |
| `*_odometer_at_day_start` | Odometer snapshot at midnight (updated automatically) |
| `*_odometer_at_week_start` | Odometer snapshot at Monday midnight (updated automatically) |
| `*_odometer_at_month_start` | Odometer snapshot on the configured day (updated automatically) |

### Datetimes (`datetime.*`)

| Entity | Description |
|---|---|
| `*_drivetrain_last_service_date` | Date of the last drivetrain maintenance |
| `*_oil_change_last_oil_change_date` | Date of the last oil change |
| `*_service_last_service_date` | Date of the last service |
| `*_refuel_pending_at` | Timestamp of the pending refuel confirmation (internal) |

### Device Tracker (`device_tracker.*`)

| Entity | Description |
|---|---|
| `*_position` | Real-time GPS position of the motorcycle |

---

## 🤖 Automation blueprint

The integration ships with a **complete blueprint** (`georide-trips.yaml` — v28.1) handling all notifications and business logic. **Create one instance per motorcycle.**

### Blueprint features

**⛽ Fuel**
- Push notification when the `refuel_needed` binary sensor turns `on`
- Automatic refuel recording via the **Confirm refuel** button: precise odometer captured at the end of the trip to the station (after the tracker is locked)
- Rolling-average computation over the last 3 refuels
- Notification offering to apply the newly computed range via the **Apply computed range** button

**🗺️ New trip**
- Notification on every stop if the distance exceeds the configured threshold
- Content: distance, duration, average speed, max speed, departure/arrival address
- Triggered on tracker locking (more reliable than movement detection)
- Automatic fallback on a change of the last-trip sensor

**🔗 Drivetrain maintenance / 🛢️ Oil change / 🔧 Service**
- Single notification on the `off → on` transition of the corresponding binary sensor
- No duplicate notifications on HA restarts

**📅 Periodic mileage**
- Weekly and monthly summaries as push and/or persistent notifications

**🚨 Security**
- Immediate notification on a theft alarm or a detected fall

### Installing the blueprint

1. Copy `georide-trips.yaml` into `config/blueprints/automation/georide_trips/`
2. In HA: **Settings → Automations → Blueprints**
3. Create an automation from the **Moto GeoRide - Suivi complet** blueprint
4. Configure the entities of each section (motorcycle, sensors, notifications…)

---

## 🔧 Odometer calculation

The GeoRide tracker only counts km from its **installation date**, not from the motorcycle's origin. The `*_odometer` entity applies an **offset** to restore the real mileage:

```
Real odometer = Tracker lifetime (km since installation) + Offset (km before installation)
```

The offset is configurable directly from the HA interface via `number.*_odometer_offset`. All maintenance and fuel entities use this corrected odometer.

---

## ⛽ Fuel workflow

1. The user refuels and presses **Confirm refuel**
2. The system waits for the end of the return trip (tracker locking)
3. The odometer at the refuel is computed: `current_odometer − distance_after_refuel`
4. The inter-refuel distance is recorded in the FIFO history (3 values)
5. The rolling average is recomputed
6. A notification offers to apply the newly computed range via the **Apply computed range** button

> The total range **never updates automatically** — the user keeps full control.

---

## 📋 Requirements

- Home Assistant 2024.6 or higher
- A GeoRide account with at least one active tracker
- **Home Assistant Companion** app (for push notifications)
- Python 3.12+

### Python dependencies (installed automatically)

- `aiohttp >= 3.8.0`
- `python-socketio[asyncio_client] >= 5.0`

---

## 🌐 API endpoints used

| Endpoint | Usage |
|---|---|
| `POST /user/login` | Authentication |
| `GET /user/trackers` | List of trackers + status |
| `GET /tracker/{id}/trips` | Trip history |
| `GET /tracker/{id}/trip/{trip_id}/positions` | Positions of a trip |
| `PUT /tracker/{id}/eco` | Enable / disable eco mode |
| `POST /tracker/{id}/toggleLock` | Lock / unlock the tracker |
| `POST /tracker/{id}/sonor-alarm/off` | Stop the audible alarm |
| `Socket.IO socket.georide.com` | Real-time events |

---

## 🛠️ Troubleshooting

**The lifetime mileage does not update**
Check that the lifetime coordinator is not in error in the logs. The refresh is triggered at midnight and after each new trip.

**The odometer is incorrect**
Configure `number.*_odometer_offset` with the motorcycle's mileage at the time the tracker was installed.

**Maintenance notifications repeat**
The blueprint triggers notifications on the `off → on` transition of the binary sensors. Check in the automation traces that the binary sensor does return to `off` after maintenance confirmation.

**Socket.IO disconnects frequently**
Normal on an unstable network — HTTP polling takes over automatically. Disable Socket.IO in the options if the connection is too unstable.

**The "In movement" sensor stays stuck at `on`**
The `StatusCoordinator` (5 min polling) automatically detects the real state and forces a return to `off`. The maximum correction delay is 5 minutes.

**Entities do not appear after installation**
Make sure the folder is named exactly `georide_trips` and fully restart Home Assistant (not just reload the configuration).

**GPS positions are inaccurate**
Configure the GPS filter in the options (`Minimum GPS accuracy`) to ignore positions whose accuracy radius exceeds the defined threshold (e.g. 50 m).

**The integration stopped working after a GeoRide password change**
Home Assistant raises a re-authentication notification — open **Settings → Devices & services** and enter the new password in the GeoRide Trips reauth prompt. No need to delete and re-add the integration.

**The "Last alarm" sensor shows `Unknown`**
Expected until an alarm fires while the integration is running — it is fed by live Socket.IO events and does not backfill alarms received before installation. It populates (and persists) on the next theft/fall/vibration/power-cut event.

**The integration icon shows "icon not available"**
Local brand images live in `custom_components/georide_trips/brand/`. Home Assistant only scans that folder at startup, so a full restart is required after first adding it (a config reload or browser refresh is not enough).

---

## 🙏 Credits

The original **GeoRide Trips** integration — including the GeoRide API reverse-engineering and the entire integration design — was written by **[druide93](https://github.com/druide93/Georide-Trips)**. Full credit for the project goes to him.

## 📦 Project status

This is **my personal copy** of druide93's integration, with adaptations for my own setup (a shaft-drive Honda NT700V). I am **not planning to maintain it actively** and it is provided **as-is, with no support, no warranty, and no contributions accepted** (issues and pull requests will not be monitored). If you want the maintained, canonical project, use **[druide93/Georide-Trips](https://github.com/druide93/Georide-Trips)**.

## 📄 License

MIT License — See [LICENSE](LICENSE) for details.

---

> **Note**: This project is not affiliated with GeoRide. GeoRide is a registered trademark of GeoRide SAS.
