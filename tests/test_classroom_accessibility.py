from __future__ import annotations

import re
from pathlib import Path

TEMPLATES = Path(__file__).parents[1] / "gamgui" / "web" / "templates"
CLASSROOM_TEMPLATES = [
    path
    for path in TEMPLATES.glob("*classroom*.html")
]


def _luminance(color: str) -> float:
    channels = []
    for offset in (1, 3, 5):
        value = int(color[offset : offset + 2], 16) / 255
        channels.append(
            value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
        )
    return (
        0.2126 * channels[0]
        + 0.7152 * channels[1]
        + 0.0722 * channels[2]
    )


def _contrast(foreground: str, background: str) -> float:
    first, second = _luminance(foreground), _luminance(background)
    return (max(first, second) + 0.05) / (min(first, second) + 0.05)


def test_classroom_text_and_button_pairs_meet_wcag_aa():
    pairs = (
        ("#52647B", "#FFFFFF"),  # brand-blueink text on cards
        ("#52647B", "#FAF9F6"),  # brand-blueink text on paper
        ("#FFFFFF", "#52647B"),  # primary button text
        ("#78350F", "#FFFBEB"),  # amber-900 on amber-50
        ("#991B1B", "#FEF2F2"),  # red-800 on red-50
        ("#065F46", "#ECFDF5"),  # emerald-800 on emerald-50
        ("#52647B", "#DBE3EB"),  # monitoring phase progress graphic
    )
    assert all(_contrast(foreground, background) >= 4.5 for foreground, background in pairs)


def test_classroom_controls_have_visible_keyboard_focus():
    for path in CLASSROOM_TEMPLATES:
        text = path.read_text(encoding="utf-8")
        for tag in re.findall(r"<(?:button|a|summary)\b[^>]*>", text, flags=re.I | re.S):
            assert "focus-visible:" in tag, f"{path.name}: missing focus-visible style in {tag}"


def test_classroom_detail_never_eager_loads_rosters():
    detail = (TEMPLATES / "_classroom_detail.html").read_text(encoding="utf-8")
    assert 'hx-trigger="load"' not in detail
    assert "/roster?role=teachers" in detail
    assert "/roster?role=students" in detail


def test_classroom_templates_do_not_use_low_contrast_brand_gray_for_live_text():
    for path in CLASSROOM_TEMPLATES:
        text = path.read_text(encoding="utf-8")
        live_text_uses = [
            token
            for token in re.findall(r"(?:^|\s)(text-brand-gray)(?:\s|\"|')", text)
        ]
        if path.name == "_classroom_detail.html":
            # Disabled controls are exempt from WCAG text contrast.
            assert text.count("text-brand-gray") == text.count("disabled:text-brand-gray")
        else:
            assert not live_text_uses, path.name


def test_monitoring_keeps_pacing_details_optional_and_read_only():
    monitoring = (TEMPLATES / "classroom_monitoring.html").read_text(encoding="utf-8")
    live = (TEMPLATES / "_classroom_monitoring_live.html").read_text(encoding="utf-8")
    receipt = (TEMPLATES / "_classroom_monitoring_receipt.html").read_text(
        encoding="utf-8"
    )
    assert "Working normally" not in monitoring + live
    assert "Technical receipt" in live
    assert "local page checks about every three seconds" in live
    assert "Open Recovery to pause" in monitoring
    assert "worker_count" not in monitoring
    assert "worker_count" not in live
    assert "worker_count" in receipt
    assert "raw GAM output" in receipt
