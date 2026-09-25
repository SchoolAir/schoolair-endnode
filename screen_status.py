#!/usr/bin/env python3
"""screen_status.py — status display driver for the Waveshare 1.47" Touch LCD.

PROTOTYPE, one physical unit — this is not (yet) part of the fleet: it isn't
installed by schoolair_setup.sh or wired into deploy/, and nothing else in the
repo depends on it. Promote it (add to schoolair_setup.sh's install list, gate
it like schoolair-led.service) once it's actually wanted on more than this one
dev unit.

Mirrors led_status.py's architecture on purpose: reads the SAME LED_STATE_FILE
that wizard.py / netwatch.sh / jobs/ingest.py already write ("ok", "thinking",
"ap", "error", "no_sensor") and renders a simple graphic per state — no
producer-side changes needed at all, this is just a second, independent reader
of a signal that already exists. Unlike led_status.py, this process never
WRITES LED_STATE_FILE (only ever reads it) — two independent writers of a
shared state file is exactly the kind of race this project already had to fix
once (see led_status.py's own startup-reset comment); one reset owner (the LED
daemon) is enough.

Look, live-tuned by eye on the real panel (dark background with only the
content lit, not filled panels; a pastel palette instead of saturated
primaries; landscape, bigger fitted text):
  - Panel used landscape (320x172, native pixels rotated — jd9853.show_image()
    auto-detects this from the image's own dimensions, no driver change
    needed): more horizontal room for icon+text side by side than the
    original portrait layout had. Getting landscape to render correctly at
    all took fixing two real bugs in the vendored jd9853.py driver — a
    landscape-only double pixel write, and a fixed hardware column offset
    applied to the wrong axis after the 90-degree MADCTL rotation — see
    i2c/lcd147/README.md. Confirmed live with a minimal corner-marker
    diagnostic before trusting it with the real drawing code again.
  - BG is near-black; every state draws OUTLINE/stroke shapes and text in its
    own colour, not filled blocks — "light the pixels we use, not the
    background" was explicit. A solid-red ERROR panel, which an earlier pass
    of this file had, is exactly what that rules out.
  - Palette (PASTEL_* below) is generated from one shared HSL formula (see
    _pastel()) so every state's colour has the same family look — matched
    lightness/saturation, only the hue differs — rather than picked by eye
    per state. ERROR is deliberately a little more saturated/less light than
    the shared formula: same family, just a bit more assertive, since an
    error is the one state that should read as slightly more urgent even in
    a soft palette. Yellow/orange started 20 degrees apart on the wheel,
    which pastel-desaturates to two colours too close to tell apart without
    reading the label — widened to 55/15 degrees (checked against every
    palette pair, not just that one) once that was caught.
  - Text size is computed per word via _fit_font()/_fit_and_center_text(), not
    a fixed guessed size: it finds the largest font that fits a given box, so
    "AP" and "NO SENSOR" each get the biggest legible size for their own
    length instead of one constant that's too small for short words or
    overflows for long ones. _fit_and_center_text() exists specifically
    because an earlier version passed the fitting box and the centred
    position as two separate, independently-chosen numbers that quietly
    disagreed — NO SENSOR clipped off the right edge of the panel, ERROR was
    one pixel from the same bug — fixed by making both come from the same
    [left, right] bounds.

States, per the request that started this (not every choice is specified —
no_sensor's design is this file's own placeholder, flagged in its own
docstring below):
  ok         a smiley face
  ap         "AP" + a wifi/broadcast glyph
  error      "ERROR" + a warning-triangle glyph
  thinking   a rotating arc (loading spinner)
  no_sensor  placeholder — not specified, see _draw_no_sensor()

Hardware: Waveshare's own jd9853 (SPI display) + axs5106l (I2C touch, address
0x63 — confirmed not colliding with the SEN6x sensor's 0x6b on the same bus)
drivers, vendored (with two bug fixes, see above) in i2c/lcd147/ (see that
directory's README for pin assignment and origin). Touch input isn't read or
acted on yet — this only drives the display.

All the drawing functions below take no hardware and return a plain PIL
Image, so they're fully unit-testable without a Pi. Hardware setup and the
render loop live in main(), which defers every hardware import (pigpio-style)
so importing this module elsewhere never requires spidev/gpiozero/RPi.GPIO/
smbus2 to be installed.
"""

import colorsys
import math
import os
import sys
import time

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "i2c", "lcd147"))

LED_STATE_FILE = "/run/schoolair-led-state"
_VALID_STATES = {"ok", "thinking", "ap", "error", "no_sensor"}

PANEL_W, PANEL_H = 320, 172   # landscape: jd9853.show_image() detects this from the
                               # image size itself (== (height, width) of its native
                               # portrait mode) and rotates accordingly — no driver change.

STATE_POLL_S = 1.0            # how often main() checks LED_STATE_FILE for a change, in any static state
SPINNER_FRAME_S = 0.15        # ~6.7 FPS while "thinking" — see main()'s note on why this isn't higher
_FRAME_EPSILON_S = 1e-6       # tolerance on the frame-due check below — time.monotonic() is a float, and
                               # `elapsed >= SPINNER_FRAME_S` can spuriously miss by float error alone
                               # (e.g. summing 0.15 repeatedly lands a hair under the exact target), not just
                               # under real scheduling jitter. Found by a test using an exact-interval fake
                               # clock; real sleep() usually overshoots enough to not hit this, but "usually"
                               # isn't a real guarantee — this just removes the sensitivity to it either way.
SPINNER_SWEEP_DEG = 90        # arc length of the spinner
SPINNER_STEP_DEG = 18         # how far the spinner rotates per frame (SPINNER_FRAME_S apart)


def _pastel(hue_deg: float, light: float = 0.72, sat: float = 0.55) -> tuple:
    """One point in the shared palette formula: fixed lightness/saturation
    "family", hue is the only thing that varies state to state. Kept as a
    function (not just the tuples below) so the formula itself — not just
    its output — is visible and reviewable, and so a future state can be
    added in the same family with one line."""
    r, g, b = colorsys.hls_to_rgb(hue_deg / 360, light, sat)
    return (round(r * 255), round(g * 255), round(b * 255))


BG = (8, 8, 10)                                   # near-black, not pure black (avoids any crush look)
# Hues: yellow/orange started at 50/30 degrees, which pastel-desaturates to two colours
# only 26 "distance" apart (sum of per-channel diffs) — too close once you're not reading
# the label text. 55/15 keeps every pair >= 52 apart (checked against all pairs, not just
# this one) without tightening red/orange, which some other spacings did instead.
PASTEL_YELLOW      = _pastel(55)                  # ok / smiley
PASTEL_YELLOW_DIM  = _pastel(55, light=0.40, sat=0.60)   # darker tone of the SAME hue, for detail strokes
PASTEL_BLUE        = _pastel(210)                 # ap
PASTEL_RED         = _pastel(355, light=0.68, sat=0.62)  # error — see module docstring on why this
PASTEL_GREY        = _pastel(220, light=0.75, sat=0.12)  # thinking — deliberately near-neutral
PASTEL_ORANGE      = _pastel(15)                  # no_sensor

_FONT_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def _font(size: int) -> "ImageFont.FreeTypeFont | ImageFont.ImageFont":
    """A bold TTF at the given size, falling back to PIL's crude built-in bitmap
    font (always available, never fails) if DejaVu isn't installed — better a
    tiny fallback font than a crashed render loop over a missing font file."""
    for path in _FONT_PATHS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _fit_font(draw: "ImageDraw.ImageDraw", text: str, max_w: int, max_h: int,
              start: int = 160, min_size: int = 14) -> "ImageFont.FreeTypeFont | ImageFont.ImageFont":
    """The largest font (of _font()'s bold TTF, so this only makes sense while
    that's available — the PIL bitmap fallback ignores size entirely) that
    fits `text` within max_w x max_h. Every text render below uses this
    instead of one guessed constant, so a short word ("AP") gets to be much
    bigger than a long one ("NO SENSOR") rather than both settling for
    whatever fits the longer one."""
    size = start
    while size > min_size:
        font = _font(size)
        bbox = draw.textbbox((0, 0), text, font=font)
        if bbox[2] - bbox[0] <= max_w and bbox[3] - bbox[1] <= max_h:
            return font
        size -= 2
    return _font(min_size)


def _centered_text(draw: "ImageDraw.ImageDraw", cx: int, cy: int, text: str, font, fill) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text((cx - w / 2 - bbox[0], cy - h / 2 - bbox[1]), text, font=font, fill=fill)


def _fit_and_center_text(draw: "ImageDraw.ImageDraw", text: str, left: int, right: int, max_h: int, fill) -> None:
    """Fits `text` to the width [left, right] and centers it there — one
    source of truth for both the fitting box and the final position, unlike
    passing a separate (cx, max_w) pair that can silently drift out of sync
    (found live: NO SENSOR's max_w and its centre point disagreed enough to
    clip the word off the right edge of the panel; ERROR was one pixel from
    the same bug). left/right are themselves the only numbers each caller
    needs to get right."""
    font = _fit_font(draw, text, max_w=right - left, max_h=max_h)
    _centered_text(draw, (left + right) // 2, PANEL_H // 2, text, font, fill)


def _blank() -> "Image.Image":
    return Image.new("RGB", (PANEL_W, PANEL_H), BG)


def _draw_ok() -> "Image.Image":
    """A smiley face — outline only (not a filled disc): stays consistent with
    "light the pixels we use", and reads clearly enough at this size."""
    img = _blank()
    draw = ImageDraw.Draw(img)
    cx, cy, r = PANEL_W // 2, PANEL_H // 2, 68
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=PASTEL_YELLOW, width=6)
    eye_dx, eye_dy, eye_r = 24, 16, 7
    for dx in (-eye_dx, eye_dx):
        draw.ellipse((cx + dx - eye_r, cy - eye_dy - eye_r, cx + dx + eye_r, cy - eye_dy + eye_r),
                     fill=PASTEL_YELLOW_DIM)
    mouth_r = 38
    draw.arc((cx - mouth_r, cy - mouth_r + 8, cx + mouth_r, cy + mouth_r + 8), start=25, end=155,
              fill=PASTEL_YELLOW_DIM, width=6)
    return img


def _draw_ap() -> "Image.Image":
    """Wifi/broadcast glyph on the left, "AP" fit to the remaining width on
    the right — landscape's wide aspect ratio is what makes room for this
    side-by-side layout instead of the old stacked one."""
    img = _blank()
    draw = ImageDraw.Draw(img)
    icon_cx, base_y = 95, PANEL_H // 2 + 20
    draw.ellipse((icon_cx - 6, base_y - 6, icon_cx + 6, base_y + 6), fill=PASTEL_BLUE)
    for r in (26, 48, 70):
        draw.arc((icon_cx - r, base_y - r, icon_cx + r, base_y + r), start=200, end=340,
                  fill=PASTEL_BLUE, width=7)
    _fit_and_center_text(draw, "AP", left=180, right=PANEL_W - 15, max_h=120, fill=PASTEL_BLUE)
    return img


def _draw_error() -> "Image.Image":
    """Warning-triangle glyph + "ERROR", fit to the panel — same layout
    pattern as _draw_ap(), same family colour (PASTEL_RED)."""
    img = _blank()
    draw = ImageDraw.Draw(img)
    cx, cy, tri_r = 90, PANEL_H // 2 + 6, 58
    pts = [(cx, cy - tri_r), (cx - tri_r * 0.87, cy + tri_r * 0.6), (cx + tri_r * 0.87, cy + tri_r * 0.6)]
    draw.polygon(pts, outline=PASTEL_RED, width=6)
    draw.line((cx, cy - 18, cx, cy + 14), fill=PASTEL_RED, width=7)
    draw.ellipse((cx - 4, cy + 26, cx + 4, cy + 34), fill=PASTEL_RED)
    _fit_and_center_text(draw, "ERROR", left=165, right=PANEL_W - 15, max_h=110, fill=PASTEL_RED)
    return img


def _draw_no_sensor() -> "Image.Image":
    """Placeholder — "no_sensor" wasn't specified when this was requested (only
    ok/ap/error/thinking were). Kept simple and easy to swap: change this one
    function to change the whole state's look. Glyph: a sensor "eye" with a
    disabled-slash through it, same layout pattern as ap/error."""
    img = _blank()
    draw = ImageDraw.Draw(img)
    cx, cy, r = 90, PANEL_H // 2, 45
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=PASTEL_ORANGE, width=6)
    draw.ellipse((cx - 10, cy - 10, cx + 10, cy + 10), fill=PASTEL_ORANGE)
    draw.line((cx - r - 10, cy + r + 10, cx + r + 10, cy - r - 10), fill=PASTEL_ORANGE, width=7)
    _fit_and_center_text(draw, "NO SENSOR", left=160, right=PANEL_W - 15, max_h=70, fill=PASTEL_ORANGE)
    return img


def _draw_thinking(phase_deg: float) -> "Image.Image":
    """One frame of the loading spinner: a fixed-length arc at a rotating start
    angle. phase_deg is the arc's start angle in degrees (any value — PIL's
    arc() takes angles mod 360 itself, so the caller doesn't need to wrap it)."""
    img = _blank()
    draw = ImageDraw.Draw(img)
    cx, cy, r = PANEL_W // 2, PANEL_H // 2, 62
    draw.arc((cx - r, cy - r, cx + r, cy + r), start=phase_deg, end=phase_deg + SPINNER_SWEEP_DEG,
              fill=PASTEL_GREY, width=12)
    return img


_STATIC_RENDERERS = {
    "ok": _draw_ok,
    "ap": _draw_ap,
    "error": _draw_error,
    "no_sensor": _draw_no_sensor,
}


def _read_state() -> str:
    """Same contract as led_status.py's own _read_state(): unrecognized or
    missing content falls back to "thinking"."""
    try:
        with open(LED_STATE_FILE) as f:
            s = f.read().strip()
        if s in _VALID_STATES:
            return s
    except OSError:
        pass
    return "thinking"


def _state_file_mtime():
    """Same rationale as led_status.py's version: os.stat (a few tens of us) is
    far cheaper than open+read+close, and the state changes at most a few
    times a minute — no reason to pay read cost every poll."""
    try:
        return os.stat(LED_STATE_FILE).st_mtime_ns
    except OSError:
        return None


def main() -> None:
    import jd9853  # deferred: Pi-only (spidev/gpiozero), keeps this module importable/testable elsewhere

    disp = jd9853.jd9853()
    disp.clear()

    last_state = None
    raw_state = _read_state()
    state_mtime = _state_file_mtime()
    spinner_phase = 0.0
    last_spinner_draw = 0.0

    print(f"[screen] jd9853 ready — {PANEL_W}x{PANEL_H}")

    while True:
        now = time.monotonic()
        mtime = _state_file_mtime()
        if mtime != state_mtime:
            raw_state = _read_state()
            state_mtime = mtime

        if raw_state != last_state:
            if raw_state in _STATIC_RENDERERS:
                disp.show_image(_STATIC_RENDERERS[raw_state]())
            last_state = raw_state
            spinner_phase = 0.0
            last_spinner_draw = 0.0  # force an immediate first spinner frame below

        if raw_state == "thinking" and now - last_spinner_draw >= SPINNER_FRAME_S - _FRAME_EPSILON_S:
            # Full-panel show_image() every frame, same as the static states — simplest
            # correct thing, and untested against real hardware yet (see the deploy
            # notes). If SPI/CPU cost turns out to matter once measured on the device,
            # the ready optimization is a windowed update of just the spinner's bounding
            # box via disp.show_image_windows() instead of the whole panel.
            disp.show_image(_draw_thinking(spinner_phase))
            spinner_phase = (spinner_phase + SPINNER_STEP_DEG) % 360
            last_spinner_draw = now

        time.sleep(SPINNER_FRAME_S if raw_state == "thinking" else STATE_POLL_S)


if __name__ == "__main__":
    main()
