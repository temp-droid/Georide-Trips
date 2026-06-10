"""Constants for GeoRide Trips integration."""

DOMAIN = "georide_trips"

# REST API: the endpoints live in api.py (single source of truth).
# Do not redefine here — the old /eco-mode/on|off duplicate contradicted
# the PUT /eco actually used and verified in production.

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
CONF_DRIVE_TYPE = "drive_type"

# Default values
DEFAULT_SCAN_INTERVAL = 3600  # 1 hour
DEFAULT_LIFETIME_SCAN_INTERVAL = 86400  # 24 hours
DEFAULT_TRIPS_DAYS_BACK = 30
DEFAULT_SOCKETIO_ENABLED = True
DEFAULT_TRACKER_SCAN_INTERVAL = 300  # 5 minutes
DEFAULT_GPS_MIN_ACCURACY = 0  # 0 = disabled (no filter)
DEFAULT_GPS_MIN_DISTANCE = 10  # 10 meters (0 = disabled)
DEFAULT_DRIVE_TYPE = "chain"  # chain entities on by default
DRIVE_TYPES = ["chain", "shaft", "belt"]

# Drivetrain maintenance profiles — the single "drivetrain" maintenance slot is
# always created; its label, default intervals and time dimension adapt to the
# selected drive_type. day_interval == 0 means the slot is km-only (no time
# dimension), matching chain/belt which wear by distance only.
DRIVETRAIN_PROFILES = {
    "chain": {
        "label": "Chain",
        "km_interval": 800,
        "alert_threshold": 150,
        "day_interval": 0,
    },
    "shaft": {
        "label": "Final drive oil",
        "km_interval": 40000,
        "alert_threshold": 3000,
        "day_interval": 1095,
    },
    "belt": {
        "label": "Drive belt",
        "km_interval": 16000,
        "alert_threshold": 1500,
        "day_interval": 0,
    },
}

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
