# SchoolAir Gatekeeper – deployment configuration
# Edit this file before deploying to each unit.

# Primary (AWS) server endpoints
HEARTBEAT_URL       = "https://dashboard.schoolair.org/aqc/v1/register"
VALIDATE_URL        = "https://dashboard.schoolair.org/aqc/v1/validate-token"
NEW_SERVER_BASE_URL = "https://dashboard.schoolair.org"
HEARTBEAT_TIMEOUT   = 15  # seconds

# Local storage
CONFIG_DIR           = "/home/admin/.config/schoolair"
STAGING_FILE         = CONFIG_DIR + "/staging.json"
STATUS_FILE          = CONFIG_DIR + "/status.json"
ERROR_FILE           = CONFIG_DIR + "/last_error.txt"
NODE_RED_TOKEN_FILE  = "/home/admin/.device_token"

# Networking
AP_INTERFACE          = "wlan0"
AP_IP                 = "192.168.4.1"
# Name of the NetworkManager connection that holds the AP / hotspot profile.
# Check with:  nmcli con show
AP_CONNECTION_NAME     = "SchoolAir_AP"

# Web server
SERVER_PORT = 80

# pi-main .env path — wizard writes AUTH_TOKEN here after registration
# ADJUST THIS PATH if pi-main is deployed elsewhere on the device
PI_MAIN_ENV_PATH = "/home/admin/schoolair/.env"

# Systemd service names
TELEMETRY_SERVICE = "schoolair"
