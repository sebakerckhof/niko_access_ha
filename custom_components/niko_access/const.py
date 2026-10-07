"""Constants for the Niko Access integration."""

from datetime import timedelta

DOMAIN = "niko_access"
MANUFACTURER = "Niko (Hikvision OEM)"

CONF_PASSWORD_MD5 = "password_md5"
CONF_FEATURE_CODE = "feature_code"
CONF_API_URL = "api_url"
CONF_LOCK_IDS = "lock_ids"
CONF_SCAN_INTERVAL = "scan_interval"

# Empty = use the locks marked enabled in locksParams (lockIds are 1-based).
DEFAULT_LOCK_IDS = ""
# Used when locksParams cannot be read and nothing is configured.
FALLBACK_LOCK_IDS = [1]
DEFAULT_SCAN_INTERVAL = 3
MIN_SCAN_INTERVAL = 2

# Camera (local CPD7 stream). The door station's LAN IP is required because the
# cloud reports it empty; channel defaults to 1, main stream (H.264 1280x720).
CONF_CAMERA_IP = "camera_ip"
CONF_CAMERA_CHANNEL = "camera_channel"
CONF_CAMERA_QUALITY = "camera_quality"
DEFAULT_CAMERA_CHANNEL = 1
DEFAULT_CAMERA_QUALITY = "main"
CAMERA_QUALITIES = ["main", "sub"]

# MJPEG transcode (ffmpeg H.264 -> JPEG) defaults.
MJPEG_FPS = 8
MJPEG_QUALITY = 5
MJPEG_WIDTH = 1280
MJPEG_HEIGHT = 720

# Device list / online state changes slowly; refresh it every N call polls.
DEVICE_REFRESH_INTERVAL = timedelta(minutes=5)
