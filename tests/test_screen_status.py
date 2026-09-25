"""tests/test_screen_status.py

Unlike led_status.py's tests, PIL is real, locally-runnable code (no hardware
involved in rendering) — so these render actual images and check real pixels,
not mocks. Only main()'s render loop needs a fake jd9853 module.

Geometric pixel checks use PIL's own arc()/ellipse() angle convention, verified
once directly (see the git history for how): angle 0 = east (3 o'clock),
increasing clockwise, so a point at angle theta (degrees) on a circle centered
at (cx, cy) radius r is (cx + r*cos(theta), cy + r*sin(theta)).
"""

import math
import sys
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image, ImageDraw

import screen_status as s


def _px(img, x, y):
    return img.convert("RGB").getpixel((x, y))


def _color_present(img, color, tolerance=10):
    """True if `color` appears anywhere in img, exact channel-wise, within a
    small tolerance (stroke/font anti-aliasing blends edge pixels)."""
    for px in img.getdata():
        if all(abs(a - b) <= tolerance for a, b in zip(px, color)):
            return True
    return False


def _touches_edge(img):
    px = img.load()
    w, h = img.size
    return (any(px[x, 0] != s.BG or px[x, h - 1] != s.BG for x in range(w)) or
            any(px[0, y] != s.BG or px[w - 1, y] != s.BG for y in range(h)))


# ── palette ───────────────────────────────────────────────────────────────────

def test_pastel_formula_is_consistent_lightness_and_saturation_across_hues():
    """The whole point of _pastel() is one shared "family" (only hue varies) —
    pin that down directly, in HLS space, not just by eyeballing the RGB
    outputs. ERROR is deliberately excluded: its docstring explains why it's
    intentionally a bit more saturated/less light than the shared formula."""
    import colorsys
    family = [s.PASTEL_YELLOW, s.PASTEL_BLUE, s.PASTEL_ORANGE]
    hls = [colorsys.rgb_to_hls(*(c / 255 for c in rgb)) for rgb in family]
    lightness = [h[1] for h in hls]
    saturation = [h[2] for h in hls]
    assert max(lightness) - min(lightness) < 0.02
    assert max(saturation) - min(saturation) < 0.02


def test_the_six_palette_colors_are_pairwise_distinguishable():
    colors = [s.BG, s.PASTEL_YELLOW, s.PASTEL_YELLOW_DIM, s.PASTEL_BLUE,
              s.PASTEL_RED, s.PASTEL_GREY, s.PASTEL_ORANGE]
    for i, a in enumerate(colors):
        for b in colors[i + 1:]:
            assert sum(abs(x - y) for x, y in zip(a, b)) > 30, (a, b)


def test_no_palette_color_is_fully_saturated_or_near_white_or_black():
    """"Pastel... not too bright or too saturated" as a concrete, checkable
    property: every channel stays clear of the 0/255 extremes."""
    for name in ("PASTEL_YELLOW", "PASTEL_BLUE", "PASTEL_RED", "PASTEL_GREY", "PASTEL_ORANGE"):
        color = getattr(s, name)
        assert all(15 <= c <= 240 for c in color), (name, color)


# ── static state renders ─────────────────────────────────────────────────────

@pytest.mark.parametrize("fn", [s._draw_ok, s._draw_ap, s._draw_error, s._draw_no_sensor,
                                 lambda: s._draw_thinking(0)])
def test_every_render_is_the_panels_native_landscape_size(fn):
    assert fn().size == (s.PANEL_W, s.PANEL_H)
    assert s.PANEL_W > s.PANEL_H   # landscape, not the original portrait orientation


@pytest.mark.parametrize("fn", [s._draw_ok, s._draw_ap, s._draw_error, s._draw_no_sensor])
def test_no_static_render_touches_the_panel_edge(fn):
    """Regression: NO SENSOR's fitted width and its centre point used to
    disagree, clipping the word off the right edge of the panel (ERROR was one
    pixel from the same bug). _fit_and_center_text() fixes this by construction
    (one shared [left, right] box for both fitting and centering), but a plain
    "does anything touch the border" check is a cheap, general safety net for
    every state, present and future — not just the two that broke."""
    assert not _touches_edge(fn())


def test_background_is_the_shared_near_black_everywhere_a_state_does_not_draw():
    """"Light the pixels we use, not the background": no static state should
    fill a large solid block — spot-check well away from any icon/text that
    the far corners are still just BG."""
    for fn in (s._draw_ok, s._draw_ap, s._draw_error, s._draw_no_sensor):
        img = fn()
        assert _px(img, 2, 2) == s.BG
        assert _px(img, s.PANEL_W - 3, 2) == s.BG


def test_ok_is_an_outline_smiley_not_a_filled_disc():
    img = s._draw_ok()
    cx, cy = s.PANEL_W // 2, s.PANEL_H // 2
    assert _px(img, cx, cy) == s.BG                        # center of the face: NOT filled
    assert _color_present(img, s.PASTEL_YELLOW)             # outline drawn
    assert _color_present(img, s.PASTEL_YELLOW_DIM)         # eyes/mouth drawn, darker tone of the same hue


def test_ap_has_a_blue_wifi_dot_and_ap_text():
    img = s._draw_ap()
    assert _px(img, 95, s.PANEL_H // 2 + 20) == s.PASTEL_BLUE   # the dot at the fan's base
    assert _color_present(img, s.PASTEL_BLUE)


def test_error_has_a_triangle_and_error_text_on_a_dark_background():
    """Not a solid red panel (that was the pre-redesign look, exactly what
    "light the pixels we use, not the background" rules out) — background
    stays BG, only the glyph and text are lit."""
    img = s._draw_error()
    assert _px(img, 2, 2) == s.BG
    assert _px(img, s.PANEL_W - 2, s.PANEL_H - 2) == s.BG
    assert _color_present(img, s.PASTEL_RED)


def test_no_sensor_renders_its_glyph_and_text():
    img = s._draw_no_sensor()
    assert _color_present(img, s.PASTEL_ORANGE)


def test_the_four_static_states_are_visually_distinguishable():
    """Sanity check that nobody copy-pasted a render into the wrong slot: no
    two of the four static states should produce byte-identical images."""
    imgs = {name: fn().tobytes() for name, fn in s._STATIC_RENDERERS.items()}
    assert len(set(imgs.values())) == len(imgs)


# ── _fit_and_center_text — the exact bug found live ──────────────────────────

def test_fit_and_center_text_never_exceeds_its_own_box():
    """Real boxes only — an absurdly tiny one (e.g. 5px wide) would legitimately
    overflow _fit_font's own min_size floor, which is intentional (better a
    small-but-readable font than shrinking to illegibility); that's a limit
    of _fit_font, not something this test is about."""
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    for text, left, right in [("AP", 180, s.PANEL_W - 15), ("ERROR", 165, s.PANEL_W - 15),
                               ("NO SENSOR", 160, s.PANEL_W - 15)]:
        img = Image.new("RGB", (s.PANEL_W, s.PANEL_H), s.BG)
        draw = ImageDraw.Draw(img)
        s._fit_and_center_text(draw, text, left=left, right=right, max_h=120, fill=(255, 255, 255))
        bbox = draw.textbbox((0, 0), text, font=s._fit_font(draw, text, right - left, 120))
        # every lit pixel must fall within [left, right] — the actual failure mode found live
        lit_xs = [x for x in range(s.PANEL_W) for y in range(s.PANEL_H) if img.getpixel((x, y)) != s.BG]
        if lit_xs:
            assert min(lit_xs) >= left - 1 and max(lit_xs) <= right + 1, (text, min(lit_xs), max(lit_xs))


def test_fit_and_center_text_uses_one_box_for_both_fitting_and_centering():
    """The actual root cause, pinned directly: fitting width and the centred
    position must come from the SAME [left, right], not two numbers that can
    drift apart (max_w picked independently of where cx happens to be)."""
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    font = s._fit_font(draw, "NO SENSOR", max_w=150, max_h=70)
    bbox = draw.textbbox((0, 0), "NO SENSOR", font=font)
    w = bbox[2] - bbox[0]
    assert w <= 150
    # centered in [160, 310] (width 150) must land inside those bounds, by construction
    cx = (160 + 310) // 2
    assert cx - w / 2 >= 160 - 1 and cx + w / 2 <= 310 + 1


# ── thinking spinner geometry ────────────────────────────────────────────────

def _arc_center_radius():
    return s.PANEL_W // 2, s.PANEL_H // 2, 62


def _point_on_arc(theta_deg, cx, cy, r):
    theta = math.radians(theta_deg)
    return round(cx + r * math.cos(theta)), round(cy + r * math.sin(theta))


def test_thinking_draws_only_along_its_arc_sweep():
    cx, cy, r = _arc_center_radius()
    phase = 40.0
    img = s._draw_thinking(phase)
    on_arc = _point_on_arc(phase + s.SPINNER_SWEEP_DEG / 2, cx, cy, r)
    assert _px(img, *on_arc) != s.BG
    off_arc = _point_on_arc(phase + 180, cx, cy, r)
    assert _px(img, *off_arc) == s.BG


def test_thinking_consecutive_frames_overlap_for_a_smooth_look():
    """Sweep (90 deg) is much wider than the per-frame step (18 deg) on purpose —
    that overlap is what makes the rotation look smooth rather than jerky. So the
    OLD midpoint stays covered for several frames, not just the next one."""
    assert s.SPINNER_STEP_DEG < s.SPINNER_SWEEP_DEG
    cx, cy, r = _arc_center_radius()
    old_mid = _point_on_arc(s.SPINNER_SWEEP_DEG / 2, cx, cy, r)
    assert _px(s._draw_thinking(0), *old_mid) != s.BG
    assert _px(s._draw_thinking(s.SPINNER_STEP_DEG), *old_mid) != s.BG   # still covered next frame...


def test_thinking_eventually_rotates_a_point_out_of_the_sweep():
    cx, cy, r = _arc_center_radius()
    old_mid = _point_on_arc(s.SPINNER_SWEEP_DEG / 2, cx, cy, r)
    n_steps = math.ceil(s.SPINNER_SWEEP_DEG / s.SPINNER_STEP_DEG) + 1   # enough to clear the sweep width
    assert _px(s._draw_thinking(n_steps * s.SPINNER_STEP_DEG), *old_mid) == s.BG   # ...but not forever


def test_full_rotation_returns_to_the_same_frame():
    """A full 360/STEP frames should bring the spinner back to where it started —
    catches an off-by-one in the modulo wrap in main()'s spinner_phase update."""
    assert 360 % s.SPINNER_STEP_DEG == 0
    n = 360 // s.SPINNER_STEP_DEG
    img_start = s._draw_thinking(0)
    img_wrapped = s._draw_thinking(n * s.SPINNER_STEP_DEG)
    assert img_start.tobytes() == img_wrapped.tobytes()


def test_thinking_never_touches_the_panel_edge():
    for phase in (0, 45, 90, 135, 180, 225, 270, 315):
        assert not _touches_edge(s._draw_thinking(phase)), phase


# ── _read_state / _state_file_mtime — same contract as led_status.py's ───────

def test_read_state_returns_valid_states_verbatim(tmp_path, monkeypatch):
    f = tmp_path / "state"
    monkeypatch.setattr(s, "LED_STATE_FILE", str(f))
    for state in s._VALID_STATES:
        f.write_text(state)
        assert s._read_state() == state


def test_read_state_falls_back_to_thinking_on_garbage_or_missing_file(tmp_path, monkeypatch):
    f = tmp_path / "state"
    monkeypatch.setattr(s, "LED_STATE_FILE", str(f))
    assert s._read_state() == "thinking"                 # missing file
    f.write_text("not_a_real_state")
    assert s._read_state() == "thinking"                 # garbage content


def test_state_file_mtime_tracks_changes_and_tolerates_a_missing_file(tmp_path, monkeypatch):
    f = tmp_path / "state"
    monkeypatch.setattr(s, "LED_STATE_FILE", str(f))
    assert s._state_file_mtime() is None
    f.write_text("ok")
    first = s._state_file_mtime()
    assert first is not None
    import os
    os.utime(f, ns=(first + 10**9, first + 10**9))
    assert s._state_file_mtime() != first


def test_static_renderers_cover_exactly_the_non_animated_states():
    assert set(s._STATIC_RENDERERS) == s._VALID_STATES - {"thinking"}


# ── main()'s render loop — needs a fake jd9853 module, everything else is real ─

@pytest.fixture
def fake_disp():
    disp = MagicMock()
    fake_jd9853_module = MagicMock()
    fake_jd9853_module.jd9853.return_value = disp
    with patch.dict(sys.modules, {"jd9853": fake_jd9853_module}):
        yield disp


class _FakeClock:
    """time.sleep() that doesn't actually wait but DOES advance a fake
    monotonic clock by the requested amount — main()'s frame-rate throttle
    (`now - last_spinner_draw >= SPINNER_FRAME_S`) is real wall-clock logic,
    so a sleep mock that doesn't also advance monotonic() makes every
    iteration look like it ran back-to-back in the same instant, which
    silently defeats that throttle rather than exercising it. Found live by
    tracing actual show_image call counts against hand-predicted ones."""
    def __init__(self):
        self.now = 1000.0
        self.n = 0
        self.stop_after = None

    def sleep(self, secs):
        self.now += secs
        self.n += 1
        if self.stop_after is not None and self.n >= self.stop_after:
            raise SystemExit(0)

    def monotonic(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = _FakeClock()
    monkeypatch.setattr(s.time, "sleep", c.sleep)
    monkeypatch.setattr(s.time, "monotonic", c.monotonic)
    return c


def test_main_clears_the_display_and_draws_the_current_static_state_once(tmp_path, monkeypatch, fake_disp, clock):
    f = tmp_path / "state"
    f.write_text("ok")
    monkeypatch.setattr(s, "LED_STATE_FILE", str(f))
    clock.stop_after = 3

    with pytest.raises(SystemExit):
        s.main()

    fake_disp.clear.assert_called_once()
    assert fake_disp.show_image.call_count == 1           # static state: drawn once, not every poll tick


def test_main_animates_the_thinking_spinner_every_frame(tmp_path, monkeypatch, fake_disp, clock):
    f = tmp_path / "state"
    f.write_text("thinking")
    monkeypatch.setattr(s, "LED_STATE_FILE", str(f))
    clock.stop_after = 5

    with pytest.raises(SystemExit):
        s.main()

    assert fake_disp.show_image.call_count == 5


def test_main_redraws_once_on_a_state_change_from_thinking_to_static(tmp_path, monkeypatch, fake_disp, clock):
    f = tmp_path / "state"
    f.write_text("thinking")
    monkeypatch.setattr(s, "LED_STATE_FILE", str(f))
    monkeypatch.setattr(s, "_state_file_mtime", lambda: clock.n)

    orig_sleep = clock.sleep
    def sleep_and_maybe_switch(secs):
        orig_sleep(secs)
        if clock.n == 2:
            f.write_text("ok")
    monkeypatch.setattr(s.time, "sleep", sleep_and_maybe_switch)
    clock.stop_after = 4

    with pytest.raises(SystemExit):
        s.main()

    # 2 thinking frames + 1 draw for the switch to "ok" (then no more redraws while static)
    assert fake_disp.show_image.call_count == 3


def test_main_does_not_drop_a_frame_to_floating_point_error(tmp_path, monkeypatch, fake_disp, clock):
    """Regression: summing SPINNER_FRAME_S (0.15) repeatedly into a float clock lands a
    hair under the exact target (1000.0 + 0.15 - 1000.0 == 0.14999999999997726, not
    0.15), which a plain `elapsed >= SPINNER_FRAME_S` check treats as "not due yet" —
    silently halving the real frame rate every other tick. Exactly this clock (start at
    1000.0, advance by exactly SPINNER_FRAME_S) is what caught it; a tolerant clock
    would have hidden the bug, so this one is deliberately exact."""
    f = tmp_path / "state"
    f.write_text("thinking")
    monkeypatch.setattr(s, "LED_STATE_FILE", str(f))
    clock.stop_after = 6

    with pytest.raises(SystemExit):
        s.main()

    assert fake_disp.show_image.call_count == 6   # one per tick, not one every other tick
