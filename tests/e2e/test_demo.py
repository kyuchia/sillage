"""
The static demo in a real browser: Chromium, Firefox and WebKit, desktop and phone.

See conftest.py for how to run these and what "ready" means. Scene names and counts
come from docs/scenes/manifest.json as served, so re-baking scenes does not break them.
"""

import re
import pytest
from playwright.sync_api import expect


# ---- loading -------------------------------------------------------------------------

def test_map_and_trail_overlay_render(demo):
    # MapLibre's basemap canvas plus deck.gl's overlay canvas.
    canvases = demo.page.locator("#map canvas")
    expect(canvases.first).to_be_visible()
    assert canvases.count() >= 2, "deck.gl overlay canvas missing"


def test_first_scene_loads_without_a_warning(demo, manifest):
    first = manifest["scenes"][0]
    expect(demo.page.locator("#banner")).to_contain_text(first["label"])
    assert demo.page.locator("#dial").get_attribute("data-level") in ("", None)
    # One legend row per mode, and the scene's modes are switched on.
    rows = demo.page.locator("#modeList .mode.on")
    assert rows.count() == len(first["modes"])


def test_no_console_errors(demo):
    assert demo.page_errors == [], "uncaught exceptions"
    assert demo.console_errors == []


def test_every_request_succeeds(demo, manifest):
    assert demo.failed_requests == []
    assert demo.bad_responses == []
    urls = {u: s for u, s in demo.responses}
    first_file = manifest["scenes"][0]["file"]
    assert any(u.endswith("scenes/manifest.json") and s < 400 for u, s in urls.items())
    assert any(u.endswith("scenes/" + first_file) and s == 200 for u, s in urls.items())


# ---- controls: on the lightest scene (see light_demo in conftest.py) ---------------

def test_controls_panel_opens_and_closes(light_demo, manifest):
    demo = light_demo
    page = demo.page
    side = page.locator("#side")
    page.locator("#ctlLink").click()
    expect(side).to_have_attribute("data-open", "true")
    expect(page.locator("#sceneList .sceneRow")).to_have_count(len(manifest["scenes"]))
    expect(page.locator("#sceneList .sceneRow").first).to_be_visible()
    if demo.phone:
        # On a phone the panel is a screen of its own.
        box = side.bounding_box()
        assert round(box["width"]) == page.viewport_size["width"]
    page.locator("#sideClose").click()
    expect(side).to_have_attribute("data-open", "false")


def test_switching_scene_loads_it(light_demo, manifest):
    demo = light_demo
    page = demo.page
    target = next(sc for sc in manifest["scenes"] if sc["slug"] != demo.scene["slug"])
    page.locator("#ctlLink").click()
    row = page.locator("#sceneList .sceneRow", has_text=target["label"])
    with page.expect_response(lambda r: r.url.endswith("scenes/" + target["file"])) as info:
        row.click()
    assert info.value.status == 200
    expect(page).to_have_url(re.compile(r"[?&]scene=" + re.escape(target["slug"])))
    demo.wait_ready(target["label"])
    assert demo.page_errors == [] and demo.console_errors == []
    assert demo.bad_responses == [] and demo.failed_requests == []


def test_layer_toggle_hides_and_shows_a_mode(light_demo):
    demo = light_demo
    page = demo.page
    page.locator("#ctlLink").click()
    row = page.locator("#modeList .mode.on").first
    row.click()
    expect(row).to_have_class(re.compile(r"\bhidden\b"))
    row.click()
    expect(row).not_to_have_class(re.compile(r"\bhidden\b"))


def test_about_dialog_opens_and_closes(light_demo):
    demo = light_demo
    page = demo.page
    page.locator("#aboutLink").click()
    expect(page.locator("body")).to_have_attribute("data-about", "open")
    expect(page.locator("#aboutBox")).to_be_visible()
    page.locator("#aboutClose").click()
    expect(page.locator("#aboutBox")).to_be_hidden()


# ---- desktop only: the bottom strip ---------------------------------------------------
# The strip fades out after a few idle seconds and comes back on pointer movement, as
# for a visitor; the tests move the mouse over it first.

def show_strip(page):
    box = page.locator("#playBtn").bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    expect(page.locator("#dial")).not_to_have_class(re.compile(r"\bfaded\b"))


def test_clock_runs_and_pause_stops_it(light_demo):
    demo = light_demo
    if demo.phone:
        pytest.skip("no clock or transport on a phone, by design")
    page = demo.page
    clock = page.locator("#clockTime")
    expect(clock).to_have_text(re.compile(r"^\d\d:\d\d:\d\d$"))
    first = clock.text_content()
    expect(clock).not_to_have_text(first)                      # playing at 60×

    play = page.locator("#playBtn")
    show_strip(page)
    play.click()
    expect(play).to_have_attribute("aria-label", "Play")
    paused = clock.text_content()
    page.wait_for_timeout(1_500)
    assert clock.text_content() == paused
    show_strip(page)
    play.click()
    expect(play).to_have_attribute("aria-label", "Pause")


def test_time_slider_moves_the_clock(light_demo):
    demo = light_demo
    if demo.phone:
        pytest.skip("no time slider on a phone, by design")
    page = demo.page
    clock = page.locator("#clockTime")
    show_strip(page)
    page.locator("#playBtn").click()                            # hold the clock still
    before = clock.text_content()
    page.locator("#timeSlider").evaluate(
        "el => { el.value = Math.floor(el.max / 2); el.dispatchEvent(new Event('input', {bubbles: true})); }"
    )
    expect(clock).not_to_have_text(before)


# ---- phone only: layout ----------------------------------------------------------------

def test_phone_layout_fits_the_screen(light_demo):
    demo = light_demo
    if not demo.phone:
        pytest.skip("phone layout only")
    page = demo.page
    width = page.viewport_size["width"]
    assert page.evaluate("document.documentElement.scrollWidth") <= width, "horizontal scroll"
    expect(page.locator("#dial")).to_be_hidden()               # no bottom strip on a phone
    for sel in ("#lockup", "#aboutLink", "#ctlLink"):
        el = page.locator(sel)
        expect(el).to_be_visible()
        box = el.bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= width, f"{sel} off screen"
        assert box["y"] + box["height"] <= page.viewport_size["height"], f"{sel} off screen"
