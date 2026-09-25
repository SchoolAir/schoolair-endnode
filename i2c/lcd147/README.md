# Waveshare 1.47inch Touch LCD driver (vendored)

`jd9853.py` (display, JD9853 over SPI) and `axs5106l.py` (touch, AXS5106L over I2C,
address 0x63) are Waveshare's own Raspberry Pi demo drivers, unmodified, from:

  https://www.waveshare.com/wiki/1.47inch_Touch_LCD
  (Raspberry Pi Demo -> Python/jd9853.py, Python/axs5106l.py)

Same pattern as `i2c/sen6x/raspberry-pi-i2c-sen6x/` — a vendored third-party driver,
not our own code. Panel is 172x320. Pin assignment (matches deploy wiring notes):

  LCD_RST = GPIO27   LCD_DC = GPIO25   LCD_BL = GPIO18 (PWM)
  LCD_CS/MOSI/SCLK   = hardware SPI0 (spidev(0,0))
  TP_SDA/TP_SCL      = hardware I2C1 (shared with the SEN6x sensor, address 0x6b —
                        confirmed non-colliding with the touch chip's 0x63)
  TP_INT = GPIO4     TP_RST = GPIO17

Prototype hardware, one unit only — not yet wired into schoolair_setup.sh's
fleet install list. See screen_status.py at the repo root for the status-display
daemon built on top of these.

## Deviation from Waveshare's original

`jd9853.py`'s `show_image()` had a real bug in landscape mode: the landscape
branch wrote the pixel buffer via its own loop, then fell through to a second,
unconditional write loop at the end of the method — writing the same buffer
twice with no `set_windows()` re-issued in between. Portrait mode was
unaffected (it never had that first loop). Found live: real image corruption
on the actual panel the first time landscape mode was ever exercised. Fixed
by removing the landscape branch's own loop; the trailing loop now does the
one real write for both orientations, same as it always implicitly did for
portrait.
