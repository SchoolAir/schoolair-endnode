"""tests/registration_wizard/test_improv.py

registration_wizard/improv.py: Improv Wi-Fi RPC framing (checksums, chunked
writes, Wi-Fi settings, results), checked against the spec's own example.
"""

import pytest

import improv


def _packet(command: int, data: bytes) -> bytes:
    body = bytes([command, len(data)]) + data
    return body + bytes([improv.checksum(body)])


def _wifi_data(ssid: str, password: str) -> bytes:
    s, p = ssid.encode(), password.encode()
    return bytes([len(s)]) + s + bytes([len(p)]) + p


def test_parses_the_spec_example_wifi_settings():
    # The spec's example: 01 1E 0C {MyWirelessAP} 10 {mysecurepassword} CS
    packet = _packet(improv.CMD_WIFI_SETTINGS, _wifi_data("MyWirelessAP", "mysecurepassword"))
    assert packet[:3] == bytes([0x01, 0x1E, 0x0C])
    command, data = improv.parse_command(packet)
    assert command == improv.CMD_WIFI_SETTINGS
    assert improv.parse_wifi_settings(data) == ("MyWirelessAP", "mysecurepassword")


def test_open_network_has_an_empty_password():
    assert improv.parse_wifi_settings(_wifi_data("Guest", "")) == ("Guest", "")


def test_bad_checksum_is_an_invalid_rpc():
    packet = bytearray(_packet(improv.CMD_DEVICE_INFO, b""))
    packet[-1] ^= 0xFF
    with pytest.raises(improv.ImprovError) as e:
        improv.parse_command(bytes(packet))
    assert e.value.code == improv.ERROR_INVALID_RPC


def test_wifi_settings_with_lengths_that_do_not_add_up_are_rejected():
    with pytest.raises(improv.ImprovError):
        improv.parse_wifi_settings(bytes([5]) + b"abc")
    with pytest.raises(improv.ImprovError):
        improv.parse_wifi_settings(_wifi_data("a", "b") + b"extra")


def test_buffer_reassembles_a_packet_split_over_20_byte_writes():
    packet = _packet(improv.CMD_WIFI_SETTINGS, _wifi_data("SchoolNet-Building-B", "correct horse battery"))
    buf = improv.CommandBuffer()
    chunks = [packet[i:i + 20] for i in range(0, len(packet), 20)]
    assert all(buf.feed(c) is None for c in chunks[:-1])
    command, data = buf.feed(chunks[-1])
    assert improv.parse_wifi_settings(data) == ("SchoolNet-Building-B", "correct horse battery")


def test_buffer_is_empty_after_a_bad_packet():
    buf = improv.CommandBuffer()
    bad = bytearray(_packet(improv.CMD_DEVICE_INFO, b""))
    bad[-1] ^= 1
    with pytest.raises(improv.ImprovError):
        buf.feed(bytes(bad))
    assert buf.feed(_packet(improv.CMD_DEVICE_INFO, b"")) == (improv.CMD_DEVICE_INFO, b"")


def test_result_is_length_prefixed_strings_with_checksum():
    result = improv.build_result(improv.CMD_DEVICE_INFO, ["ab", "c"])
    assert result[:-1] == bytes([0x03, 5, 2]) + b"ab" + bytes([1]) + b"c"
    assert result[-1] == improv.checksum(result[:-1])


def test_scan_result_keeps_strongest_networks_that_fit_one_packet():
    networks = [{"ssid": f"network-number-{i:02d}", "signal": 90 - i, "secured": i % 2 == 0} for i in range(40)]
    result = improv.scan_result(networks)
    assert result[1] <= improv.MAX_DATA_LEN
    data = result[2:-1]
    strings, i = [], 0
    while i < len(data):
        strings.append(data[i + 1:i + 1 + data[i]].decode())
        i += 1 + data[i]
    assert len(strings) % 3 == 0 and len(strings) > 0
    assert strings[:3] == ["network-number-00", "-55", "WPA2"]
    assert strings[4:6] == ["-56", "NO"]


def test_service_data_is_state_capabilities_and_four_reserved_bytes():
    assert improv.service_data(improv.STATE_AUTHORIZED, 0x06) == bytes([2, 6, 0, 0, 0, 0])


def test_advertising_data_fits_a_bluetooth_4_advertisement_with_the_flags():
    data = improv.advertising_data(improv.STATE_AUTHORIZED, 0x06)
    assert len(data) + 3 <= 31  # the kernel adds the 3-byte flags
    # 128-bit service UUID list, little-endian
    assert data[:2] == bytes([17, 0x07])
    assert data[2:18] == bytes.fromhex("00467768-6228-2272-4663-277478268000".replace("-", ""))[::-1]
    # service data under 0x4677: state, capabilities, 4 reserved
    assert data[18:] == bytes([9, 0x16, 0x77, 0x46, 2, 6, 0, 0, 0, 0])
