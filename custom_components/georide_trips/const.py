"""Constants for GeoRide Trips integration."""

DOMAIN = "georide_trips"

# API REST : les endpoints vivent dans api.py (source de vérité unique).
# Ne pas redéfinir ici — l'ancien duplicata /eco-mode/on|off contredisait
# le PUT /eco réellement utilisé et vérifié en production.

# Socket.IO
SOCKETIO_URL = "https://socket.georide.com"

# Configuration keys
CONF_EMAIL = "email"
CONF_PASSWORD = "password"

# Options keys
CONF_SCAN_INTERVAL = "scan_interval"
CONF_LIFETIME_SCAN_INTERVAL = "lifetime_scan_interval"
CONF_TRIPS_DAYS_BACK = "trips_days_back"
CONF_SOCKETIO_ENABLED = "socketio_enabled"
CONF_TRACKER_SCAN_INTERVAL = "tracker_scan_interval"
CONF_GPS_MIN_ACCURACY = "gps_min_accuracy"
CONF_GPS_MIN_DISTANCE = "gps_min_distance"

# Default values
DEFAULT_SCAN_INTERVAL = 3600  # 1 heure
DEFAULT_LIFETIME_SCAN_INTERVAL = 86400  # 24 heures
DEFAULT_TRIPS_DAYS_BACK = 30
DEFAULT_SOCKETIO_ENABLED = True
DEFAULT_TRACKER_SCAN_INTERVAL = 300  # 5 minutes
DEFAULT_GPS_MIN_ACCURACY = 0  # 0 = désactivé (aucun filtre)
DEFAULT_GPS_MIN_DISTANCE = 10  # 10 mètres (0 = désactivé)

# Service attributes
ATTR_TRACKER_ID = "tracker_id"
ATTR_TRIP_ID = "trip_id"
ATTR_FROM_DATE = "from_date"
ATTR_TO_DATE = "to_date"

# Trip attributes
ATTR_NICE_NAME = "nice_name"
ATTR_START_TIME = "start_time"
ATTR_END_TIME = "end_time"
ATTR_START_ADDRESS = "start_address"
ATTR_END_ADDRESS = "end_address"
ATTR_DISTANCE = "distance"
ATTR_DURATION = "duration"
ATTR_AVERAGE_SPEED = "average_speed"
ATTR_MAX_SPEED = "max_speed"
ATTR_TRIP_COUNT = "trip_count"
ATTR_START_LATITUDE = "start_latitude"
ATTR_START_LONGITUDE = "start_longitude"
ATTR_END_LATITUDE = "end_latitude"
ATTR_END_LONGITUDE = "end_longitude"

# Conversions
KNOTS_TO_KMH = 1.852
METERS_TO_KM = 1000
