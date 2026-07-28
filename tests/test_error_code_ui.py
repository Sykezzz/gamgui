from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

from jinja2 import Environment

TEMPLATE_DIR = Path(__file__).parents[1] / "gamgui" / "web" / "templates"
STATIC_DIR = Path(__file__).parents[1] / "gamgui" / "web" / "static"


class _Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        self.tags.append((tag, dict(attrs)))


def _parse(html: str) -> _Tags:
    parsed = _Tags()
    parsed.feed(html)
    return parsed


def _attrs_for_id(parsed: _Tags, element_id: str) -> dict[str, str | None]:
    matches = [
        attrs
        for _, attrs in parsed.tags
        if attrs.get("id") == element_id
    ]
    assert len(matches) == 1
    return matches[0]


def _render_partial(**context: str) -> str:
    source = (TEMPLATE_DIR / "_error.html").read_text(encoding="utf-8")
    return Environment(autoescape=True).from_string(source).render(**context)


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
    error = (TEMPLATE_DIR / "error.html").read_text(encoding="utf-8")

    assert 'role="alert"' in error
    assert "bg-amber-50" in error
    assert "text-amber-800" in error
    assert "bg-amber-100/70" in error
    assert "text-amber-900" in error


def test_base_keeps_an_accessible_global_error_surface_outside_htmx_swaps():
    base = (TEMPLATE_DIR / "base.html").read_text(encoding="utf-8")
    parsed = _parse(base)

    alert = _attrs_for_id(parsed, "global-error")
    panel = _attrs_for_id(parsed, "global-error-panel")
    assert alert["role"] == "alert"
    assert alert["aria-atomic"] == "true"
    assert "hidden" not in alert
    assert "hidden" in panel
    assert base.index('id="global-error"') < base.index('id="app-main"')


def test_global_error_positioning_is_present_in_source_and_built_css():
    source = (STATIC_DIR / "app.source.css").read_text(encoding="utf-8")
    built = (STATIC_DIR / "app.css").read_text(encoding="utf-8")

    assert "#global-error-panel {" in source
    assert "width: min(calc(100% - 2rem), 28rem);" in source
    assert "#global-error-panel{" in built
    assert "#global-error-panel[hidden]{display:none}" in built


def test_global_htmx_errors_use_fixed_safe_text_and_single_completion_owner():
    base = (TEMPLATE_DIR / "base.html").read_text(encoding="utf-8")

    assert "responseText" not in base
    assert "globalErrorMessage.textContent = message" in base
    assert "globalErrorCode.textContent = code" in base
    assert '"WEB-HTTP-5XX"' in base
    assert '"WEB-REQUEST-FAILED"' in base
    assert base.count("pending = Math.max(0, pending - 1)") == 1
    assert 'addEventListener("htmx:responseError", showResponseError)' in base
    assert 'addEventListener("htmx:sendError", showTransportError)' in base
    assert 'addEventListener("htmx:timeout", showTransportError)' in base

    reporting = base.split("function showResponseError", 1)[1].split(
        "// The signature-apply progress panel", 1
    )[0]
    assert "responseText" not in reporting
    assert "pending" not in reporting
    assert "isPoll" not in reporting
    assert "showBusy" not in reporting
    assert "hideBusy" not in reporting

    completion = base.split(
        'document.body.addEventListener("htmx:afterRequest"', 1
    )[1].split(
        'document.body.addEventListener("htmx:responseError"', 1
    )[0]
    assert completion.index("if (isPoll(e)) { return; }") < completion.index(
        "clearGlobalError()"
    )
    assert "e.detail && e.detail.successful" in completion


def test_partial_error_optionally_labels_stable_code_and_next_step():
    rendered = _render_partial(
        message="Credential import could not start.",
        error_code="SETUP-ACTIVITY-PATH",
        action="Choose a writable local data folder, then retry.",
    )
    parsed = _parse(rendered)
    alerts = [
        attrs
        for _, attrs in parsed.tags
        if attrs.get("role") == "alert"
    ]

    assert len(alerts) == 1
    assert alerts[0]["aria-atomic"] == "true"
    assert "Error code" in rendered
    assert "SETUP-ACTIVITY-PATH" in rendered
    assert "Next step:" in rendered
    assert "Choose a writable local data folder, then retry." in rendered

    message_only = _render_partial(message="Try again.")
    assert "Error code" not in message_only
    assert "Next step:" not in message_only
