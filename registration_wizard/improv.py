"""improv.py — Improv Wi-Fi over BLE, protocol layer only (no Bluetooth here).

Spec: https://www.improv-wifi.com/ble/
ble_provision.py owns the GATT server and the provisioning flow; this module
only knows the UUIDs, the numbers, and how RPC packets are framed, so it can
be unit-tested on a laptop.

RPC packets (both directions):  command, data length, data…, checksum
where checksum is the sum of every preceding byte, keeping the low 8 bits.
Result data is a run of length-prefixed strings.
"""

# ── UUIDs ─────────────────────────────────────────────────────────────────────

SERVICE_UUID       = "00467768-6228-2272-4663-277478268000"
CURRENT_STATE_UUID = "00467768-6228-2272-4663-277478268001"
ERROR_STATE_UUID   = "00467768-6228-2272-4663-277478268002"
RPC_COMMAND_UUID   = "00467768-6228-2272-4663-277478268003"
RPC_RESULT_UUID    = "00467768-6228-2272-4663-277478268004"
CAPABILITIES_UUID  = "00467768-6228-2272-4663-277478268005"

# Advertised as service data next to SERVICE_UUID: state, capabilities, 4 reserved.
SERVICE_DATA_UUID  = "00004677-0000-1000-8000-00805f9b34fb"

# ── States, errors, commands, capabilities ────────────────────────────────────

STATE_AUTHORIZATION_REQUIRED = 0x01
STATE_AUTHORIZED             = 0x02
STATE_PROVISIONING           = 0x03
STATE_PROVISIONED            = 0x04

ERROR_NONE            = 0x00
ERROR_INVALID_RPC     = 0x01
ERROR_UNKNOWN_RPC     = 0x02
ERROR_UNABLE_TO_CONNECT = 0x03
ERROR_NOT_AUTHORIZED  = 0x04
ERROR_UNKNOWN         = 0xFF

CMD_WIFI_SETTINGS = 0x01
CMD_IDENTIFY      = 0x02
CMD_DEVICE_INFO   = 0x03
CMD_WIFI_SCAN     = 0x04

CAP_IDENTIFY    = 0x01
CAP_DEVICE_INFO = 0x02
CAP_WIFI_SCAN   = 0x04

MAX_DATA_LEN = 255  # the data-length field is one byte


class ImprovError(Exception):
    """A malformed RPC packet; `code` is the Improv error to report."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def checksum(data: bytes) -> int:
    return sum(data) & 0xFF


def service_data(state: int, capabilities: int) -> bytes:
    return bytes([state, capabilities, 0, 0, 0, 0])


def advertising_data(state: int, capabilities: int) -> bytes:
    """The advertisement's AD structures (length, type, value…): the service
    UUID (complete list of 128-bit UUIDs) and the service data under its 16-bit
    UUID, both little-endian. 28 bytes; the kernel adds the 3-byte flags, which
    fills the 31 a Bluetooth 4.x controller allows."""
    uuid128 = bytes.fromhex(SERVICE_UUID.replace("-", ""))[::-1]
    uuid16 = bytes.fromhex(SERVICE_DATA_UUID[4:8])[::-1]
    data = uuid16 + service_data(state, capabilities)
    return bytes([1 + len(uuid128), 0x07]) + uuid128 + bytes([1 + len(data), 0x16]) + data


class CommandBuffer:
    """Collects RPC Command writes until a whole packet is in.

    A packet over the ATT payload size (20 bytes on a default MTU) can arrive
    as several writes; Improv clients send them back to back.
    """

    def __init__(self):
        self._buf = bytearray()

    def reset(self) -> None:
        self._buf.clear()

    def feed(self, chunk: bytes) -> "tuple[int, bytes] | None":
        """Add a write. Returns (command, data) once a full packet is in, else None.
        Raises ImprovError (and resets) on a bad checksum."""
        self._buf += chunk
        if len(self._buf) < 3 or len(self._buf) < self._buf[1] + 3:
            return None
        packet = bytes(self._buf[: self._buf[1] + 3])
        self._buf.clear()
        return parse_command(packet)


def parse_command(packet: bytes) -> "tuple[int, bytes]":
    if len(packet) < 3 or len(packet) != packet[1] + 3:
        raise ImprovError(ERROR_INVALID_RPC, "RPC packet length does not match its header")
    if checksum(packet[:-1]) != packet[-1]:
        raise ImprovError(ERROR_INVALID_RPC, "RPC packet checksum mismatch")
    return packet[0], packet[2:-1]


def parse_wifi_settings(data: bytes) -> "tuple[str, str]":
    """Data of CMD_WIFI_SETTINGS: ssid length, ssid, password length, password."""
    try:
        ssid_len = data[0]
        ssid = data[1 : 1 + ssid_len]
        pass_len = data[1 + ssid_len]
        password = data[2 + ssid_len : 2 + ssid_len + pass_len]
    except IndexError:
        raise ImprovError(ERROR_INVALID_RPC, "Wi-Fi settings are truncated") from None
    if len(ssid) != ssid_len or len(password) != pass_len or 2 + ssid_len + pass_len != len(data):
        raise ImprovError(ERROR_INVALID_RPC, "Wi-Fi settings lengths do not add up")
    try:
        return ssid.decode("utf-8"), password.decode("utf-8")
    except UnicodeDecodeError:
        raise ImprovError(ERROR_INVALID_RPC, "Wi-Fi settings are not UTF-8") from None


def encode_strings(strings: "list[str]") -> bytes:
    out = bytearray()
    for s in strings:
        raw = s.encode("utf-8")[:255]
        out.append(len(raw))
        out += raw
    return bytes(out)


def build_result(command: int, strings: "list[str]") -> bytes:
    data = encode_strings(strings)
    if len(data) > MAX_DATA_LEN:
        raise ValueError(f"RPC result data is {len(data)} bytes; the limit is {MAX_DATA_LEN}")
    packet = bytes([command, len(data)]) + data
    return packet + bytes([checksum(packet)])


def scan_result(networks: "list[dict]") -> bytes:
    """One CMD_WIFI_SCAN result: (ssid, rssi, auth) per network, strongest first,
    keeping as many as fit in one packet. `networks` items are the wizard's
    _scan_networks() dicts: ssid, signal (0-100 %), secured."""
    strings: "list[str]" = []
    for net in networks:
        # nmcli reports signal as a percentage; Improv wants an RSSI-like dBm figure.
        triple = [net["ssid"], str(int(net["signal"]) // 2 - 100), "WPA2" if net["secured"] else "NO"]
        if len(encode_strings(strings + triple)) > MAX_DATA_LEN:
            break
        strings += triple
    return build_result(CMD_WIFI_SCAN, strings)
