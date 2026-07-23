from __future__ import annotations

from pathlib import Path

TEMPLATES = Path(__file__).parents[1] / "gamgui" / "web" / "templates"


def _luminance(color: str) -> float:
    channels = []
    for offset in (1, 3, 5):
        value = int(color[offset : offset + 2], 16) / 255
        channels.append(
            value / 12.92
            if value <= 0.04045
            else ((value + 0.055) / 1.055) ** 2.4
        )
    return (
        0.2126 * channels[0]
        + 0.7152 * channels[1]
        + 0.0722 * channels[2]
    )


def _contrast(foreground: str, background: str) -> float:
    first, second = _luminance(foreground), _luminance(background)
    return (max(first, second) + 0.05) / (min(first, second) + 0.05)


def test_error_code_text_pairs_meet_wcag_aa():
    pairs = (
        ("#92400E", "#FFFBEB"),  # amber-800 on amber-50
        ("#78350F", "#FFFBEB"),  # amber-900 on amber-50
        ("#78350F", "#FEF3C7"),  # amber-900 on amber-100
    )
    assert all(
        _contrast(foreground, background) >= 4.5
        for foreground, background in pairs
    )


def test_error_page_has_live_region_semantics():
    error = (TEMPLATES / "error.html").read_text(encoding="utf-8")

    assert 'role="alert"' in error
    assert "bg-amber-50" in error
    assert "text-amber-800" in error
    assert "bg-amber-100/70" in error
    assert "text-amber-900" in error
