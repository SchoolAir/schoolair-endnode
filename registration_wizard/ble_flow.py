"""ble_flow.py — the SchoolAir side of Bluetooth registration, minus Bluetooth.

Improv Wi-Fi (improv.py) carries the Wi-Fi details; it has no room for our
registration token and only one connection error. The SchoolAir extension
service (ble_provision.py) adds two characteristics, whose payloads live here:

  Registration (write, encrypted)  JSON from the page, written before Improv's
                                   Send Wi-Fi settings:
                                   {"token", "nickname", "site"?, "environment"?, "migrate"?}
  Status (read, notify)            JSON {"code", "message"}, one of STATUS_CODES,
                                   updated at every step of the attempt.

Kept free of D-Bus so it can be unit-tested on a laptop.
"""

import json

SCHOOLAIR_SERVICE_UUID = "ed523557-0dc1-411c-a940-09205f17228d"
REGISTRATION_UUID      = "94db6cec-76e0-4fad-9146-059bfedaaf47"
STATUS_UUID            = "5653e59d-03fc-4e98-a58a-ba011e9e0f3c"

STATUS_CODES = (
    "idle",
    "registration_received",
    "missing_registration",
    "busy",
    "wifi_connecting",
    "wifi_auth_failed",
    "wifi_ap_not_found",
    "wifi_failed",
    "dhcp_failed",
    "no_internet",
    "registering",
    "registration_rejected",
    "done",
)

# Status code -> the wizard's reg_state word, which drives the status LED
# (see wizard._LED_STATE_MAP). Codes not listed leave the LED alone.
LED_STATES = {
    "wifi_connecting":       "connecting",
    "registering":           "heartbeat",
    "wifi_auth_failed":      "error",
    "wifi_ap_not_found":     "error",
    "wifi_failed":           "error",
    "dhcp_failed":           "error",
    "no_internet":           "error",
    "registration_rejected": "error",
    "done":                  "success",
}

ENVIRONMENTS = ("indoor", "outdoor")
MAX_REGISTRATION_BYTES = 512  # the largest attribute value BLE allows


def status_payload(code: str, message: str = "") -> bytes:
    if code not in STATUS_CODES:
        raise ValueError(f"unknown status code {code!r}")
    return json.dumps({"code": code, "message": message}, separators=(",", ":")).encode()


def parse_registration(raw: bytes) -> dict:
    """The Registration characteristic's JSON -> the fields run_registration
    needs. Raises ValueError with a message fit for the Status characteristic."""
    if len(raw) > MAX_REGISTRATION_BYTES:
        raise ValueError("Registration data is too long")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("Registration data is not valid JSON") from None
    if not isinstance(data, dict):
        raise ValueError("Registration data must be a JSON object")

    token    = str(data.get("token", "")).strip()
    nickname = str(data.get("nickname", "")).strip()
    if not token:
        raise ValueError("Registration token is missing")
    if not nickname:
        raise ValueError("Device name is missing")
    environment = str(data.get("environment") or "indoor").strip().lower()
    if environment not in ENVIRONMENTS:
        raise ValueError("Environment must be indoor or outdoor")

    return {
        "token":       token,
        "nickname":    nickname,
        "site":        str(data.get("site") or "").strip(),
        "environment": environment,
        "migrate":     bool(data.get("migrate", False)),
    }


def wifi_error_code(nmcli_error: str) -> str:
    """Best-effort reading of `nmcli con up`'s error text. NetworkManager
    reports a wrong WPA password as missing secrets (it asked for them again,
    and nobody answered), and an absent network as no network with that SSID."""
    err = nmcli_error.lower()
    if "secrets were required" in err or "no secrets" in err or "802-1x" in err:
        return "wifi_auth_failed"
    if "no network with ssid" in err or "not found" in err:
        return "wifi_ap_not_found"
    return "wifi_failed"
