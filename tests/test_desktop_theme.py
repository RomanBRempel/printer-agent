"""Theme engine and preference store — no Qt, no display needed."""

from __future__ import annotations

import json

import pytest

from printer_agent.desktop.prefs import Preferences, load_preferences, save_preferences
from printer_agent.desktop.theme import (
    ACCENT_PRESETS,
    ThemeMode,
    build_palette,
    build_stylesheet,
    mix,
    readable_text_on,
    relative_luminance,
    with_alpha,
)


def test_build_palette_dark_and_light_differ():
    light = build_palette(ThemeMode.light, "blue")
    dark = build_palette(ThemeMode.dark, "blue")

    assert light.dark is False
    assert dark.dark is True
    assert relative_luminance(light.window) > relative_luminance(dark.window)
    assert relative_luminance(light.text) < relative_luminance(dark.text)


def test_dark_mode_lightens_the_accent_for_contrast():
    light = build_palette(ThemeMode.light, "blue")
    dark = build_palette(ThemeMode.dark, "blue")

    assert relative_luminance(dark.accent) > relative_luminance(light.accent)


@pytest.mark.parametrize("accent", sorted(ACCENT_PRESETS))
@pytest.mark.parametrize("mode", [ThemeMode.light, ThemeMode.dark])
def test_every_preset_renders_a_complete_stylesheet(mode, accent):
    palette = build_palette(mode, accent)
    stylesheet = build_stylesheet(palette)

    assert "{" in stylesheet
    # An unresolved f-string field would leave a literal placeholder behind.
    assert "None" not in stylesheet
    assert palette.accent in stylesheet


@pytest.mark.parametrize("accent", sorted(ACCENT_PRESETS))
@pytest.mark.parametrize("mode", [ThemeMode.light, ThemeMode.dark])
def test_accent_text_is_readable_on_its_accent(mode, accent):
    palette = build_palette(mode, accent)
    contrast = abs(relative_luminance(palette.accent) - relative_luminance(palette.accent_text))

    assert contrast > 0.25


def test_unknown_accent_falls_back_to_the_system_colour():
    palette = build_palette(ThemeMode.light, "not-a-preset")

    assert palette.accent.startswith("#")
    assert len(palette.accent) == 7


def test_mix_and_alpha_helpers():
    assert mix("#000000", "#FFFFFF", 0.5) == "#808080"
    assert mix("#123456", "#FFFFFF", 0.0) == "#123456"
    assert with_alpha("#FF0000", 0.5) == "rgba(255, 0, 0, 0.500)"
    assert readable_text_on("#FFFFFF") == "#000000"
    assert readable_text_on("#101010") == "#FFFFFF"


def test_preferences_round_trip(tmp_path):
    path = tmp_path / "ui.json"
    save_preferences(Preferences(theme_mode="dark", accent="teal", poll_interval_s=42), path)

    loaded = load_preferences(path)

    assert loaded.mode is ThemeMode.dark
    assert loaded.accent == "teal"
    assert loaded.poll_interval_s == 42


def test_preferences_reject_unknown_values(tmp_path):
    path = tmp_path / "ui.json"
    path.write_text(
        json.dumps({"theme_mode": "neon", "accent": "chartreuse", "poll_interval_s": 0}),
        encoding="utf-8",
    )

    loaded = load_preferences(path)

    assert loaded.mode is ThemeMode.system
    assert loaded.accent == "system"
    assert loaded.poll_interval_s == 10  # 0 is meaningless, so the default stands


def test_preferences_clamp_an_out_of_range_interval(tmp_path):
    path = tmp_path / "ui.json"
    path.write_text(json.dumps({"poll_interval_s": 1}), encoding="utf-8")

    assert load_preferences(path).poll_interval_s == 3

    path.write_text(json.dumps({"poll_interval_s": 9999}), encoding="utf-8")

    assert load_preferences(path).poll_interval_s == 300


def test_missing_preferences_file_yields_defaults(tmp_path):
    loaded = load_preferences(tmp_path / "absent.json")

    assert loaded.mode is ThemeMode.system
    assert loaded.accent == "system"
