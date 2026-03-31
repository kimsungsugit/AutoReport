"""Tests for scripts/design_system.py — CSS generation and SVG helpers."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Add scripts/ to path so we can import design_system directly
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from design_system import (
    DESIGN_CSS,
    SVG_PALETTE,
    SVG_TEXT_LIGHT,
    SVG_TEXT_DARK,
    svg_text_color_for,
    css_tag,
    full_head,
)


# ---------------------------------------------------------------------------
# CSS constants
# ---------------------------------------------------------------------------
class TestDesignCSS:
    def test_design_css_is_nonempty_string(self):
        assert isinstance(DESIGN_CSS, str)
        assert len(DESIGN_CSS) > 100

    def test_contains_root_variables(self):
        assert ":root" in DESIGN_CSS
        assert "--bg:" in DESIGN_CSS
        assert "--ink:" in DESIGN_CSS
        assert "--accent:" in DESIGN_CSS

    def test_contains_dark_mode(self):
        assert "prefers-color-scheme: dark" in DESIGN_CSS

    def test_svg_palette_has_entries(self):
        assert isinstance(SVG_PALETTE, list)
        assert len(SVG_PALETTE) >= 4
        for color in SVG_PALETTE:
            assert color.startswith("#")


# ---------------------------------------------------------------------------
# svg_text_color_for
# ---------------------------------------------------------------------------
class TestSvgTextColor:
    def test_dark_fill_returns_light_text(self):
        assert svg_text_color_for("#264653") == SVG_TEXT_LIGHT

    def test_light_fill_returns_dark_text(self):
        assert svg_text_color_for("#e9c46a") == SVG_TEXT_DARK

    def test_unknown_fill_returns_dark_text(self):
        assert svg_text_color_for("#ffffff") == SVG_TEXT_DARK


# ---------------------------------------------------------------------------
# css_tag
# ---------------------------------------------------------------------------
class TestCssTag:
    def test_wraps_in_style(self):
        tag = css_tag()
        assert tag.startswith("<style>")
        assert tag.endswith("</style>")

    def test_contains_design_css(self):
        tag = css_tag()
        assert DESIGN_CSS in tag


# ---------------------------------------------------------------------------
# full_head
# ---------------------------------------------------------------------------
class TestFullHead:
    def test_contains_doctype(self):
        head = full_head("Test Page")
        assert "<!doctype html>" in head

    def test_title_embedded(self):
        head = full_head("My Report")
        assert "<title>My Report</title>" in head

    def test_charset_meta(self):
        head = full_head("X")
        assert 'charset="utf-8"' in head

    def test_viewport_meta(self):
        head = full_head("X")
        assert "viewport" in head

    def test_extra_css_included(self):
        head = full_head("X", extra_css=".custom { color: red; }")
        assert ".custom { color: red; }" in head

    def test_contains_design_css(self):
        head = full_head("X")
        assert "--bg:" in head  # design tokens present
