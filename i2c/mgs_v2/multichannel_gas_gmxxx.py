"""Seeed Multichannel Gas Sensor v2 (GMXXX) driver.

Rewritten to use smbus2 directly — no adafruit-blinka / board / busio dependency.
Protocol: write a 1-byte command to select the channel, then read 4 bytes (uint32 LE).
"""
import time
from smbus2 import SMBus, i2c_msg

GM_102B        = 0x01   # NO2
GM_302B        = 0x03   # C2H5OH (ethanol)
GM_502B        = 0x05   # VOC
GM_702B        = 0x07   # CO
WARMING_UP     = 0xFE
WARMING_DOWN   = 0xFF
CHANGE_I2C_ADDR = 0x55

DEFAULT_ADDR   = 0x08
DEFAULT_BUS    = 1


class MultichannelGasGMXXX:
    def __init__(self, bus_num: int = DEFAULT_BUS, address: int = DEFAULT_ADDR):
        self._bus  = SMBus(bus_num)
        self._addr = address
        self._preheat()

    def _preheat(self) -> None:
        self._write(WARMING_UP)

    def _write(self, *bytes_: int) -> None:
        msg = i2c_msg.write(self._addr, list(bytes_))
        self._bus.i2c_rdwr(msg)
        time.sleep(0.001)

    def _read32(self) -> int:
        msg = i2c_msg.read(self._addr, 4)
        self._bus.i2c_rdwr(msg)
        return int.from_bytes(bytes(msg), "little")

    def _channel(self, cmd: int) -> int:
        self._write(cmd)
        return self._read32()

    def measure_no2(self)    -> int: return self._channel(GM_102B)
    def measure_c2h5oh(self) -> int: return self._channel(GM_302B)
    def measure_voc(self)    -> int: return self._channel(GM_502B)
    def measure_co(self)     -> int: return self._channel(GM_702B)

    def close(self) -> None:
        self._bus.close()
