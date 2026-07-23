from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "gamgui" / "web" / "static"


def test_static_css_is_built_and_runtime_compiler_is_removed():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert "tailwindcss v4.3.3" in css
    assert ".bg-brand-blue" in css
    assert ".font-serif" in css
    assert not (STATIC / "vendor" / "tailwind-play.js").exists()


def test_fonts_and_licenses_are_vendored():
    fonts = STATIC / "fonts"
    expected = {
        "source-sans-3-latin-300-normal.woff2",
        "source-sans-3-latin-400-normal.woff2",
        "source-sans-3-latin-500-normal.woff2",
        "source-sans-3-latin-600-normal.woff2",
        "source-sans-3-latin-400-italic.woff2",
        "source-serif-4-latin-400-normal.woff2",
        "source-serif-4-latin-600-normal.woff2",
        "source-serif-4-latin-400-italic.woff2",
        "OFL-source-sans-3.txt",
        "OFL-source-serif-4.txt",
    }
    assert expected <= {path.name for path in fonts.iterdir()}


def test_navigation_uses_stable_main_shell_with_full_page_fallback():
    base = (ROOT / "gamgui" / "web" / "templates" / "base.html").read_text(encoding="utf-8")
    assert 'hx-boost="true"' in base
    assert 'hx-target="#app-main"' in base
    assert 'hx-select="#app-main"' in base
    assert '<main id="app-main"' in base


def test_base_declares_a_local_favicon_without_a_network_probe():
    base = (ROOT / "gamgui" / "web" / "templates" / "base.html").read_text(encoding="utf-8")
    assert '<link rel="icon" href="data:," />' in base
