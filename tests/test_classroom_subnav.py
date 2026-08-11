from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATES = Path(__file__).parents[1] / "gamgui" / "web" / "templates"


def _render_subnav(section: str) -> str:
    environment = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(("html",)),
    )
    return environment.get_template("_classroom_subnav.html").render(
        classroom_section=section
    )


def test_classroom_subnav_has_native_wrapping_links_and_visible_focus():
    rendered = _render_subnav("courses")

    assert '<nav class="classroom-subnav mb-6 w-full min-w-0 max-w-full border-b' in rendered
    assert 'aria-label="Classroom sections"' in rendered
    assert 'class="flex w-full flex-wrap items-center gap-1"' in rendered
    assert 'href="/classroom"' in rendered
    assert 'href="/classroom/access"' in rendered
    assert 'href="/classroom/imports"' in rendered
    assert 'href="/classroom/courses/manage"' in rendered
    assert 'href="/classroom/monitoring"' in rendered
    assert 'href="/classroom/recovery"' in rendered
    assert rendered.count("<a ") == 6
    assert rendered.count("focus-visible:ring-2") == 6


def test_classroom_subnav_marks_only_the_active_section_as_current():
    expected_labels = {
        "dashboard": "Dashboard",
        "courses": "Courses",
        "access": "Teacher access",
        "imports": "Guided import",
        "monitoring": "Monitoring",
        "recovery": "Recovery",
    }

    for section, label in expected_labels.items():
        rendered = _render_subnav(section)
        assert rendered.count('aria-current="page"') == 1
        current_link = rendered.split('aria-current="page"', maxsplit=1)[1].split(
            "</a>", maxsplit=1
        )[0]
        assert label in current_link


def test_classroom_pages_include_the_shared_section_navigation():
    expected_sections = {
        "classroom_dashboard.html": "dashboard",
        "classroom.html": "courses",
        "classroom_access.html": "access",
        "oneroster.html": "imports",
        "classroom_monitoring.html": "monitoring",
        "classroom_recovery.html": "recovery",
    }

    for template_name, section in expected_sections.items():
        template = (TEMPLATES / template_name).read_text(encoding="utf-8")
        assert f'{{% set classroom_section = "{section}" %}}' in template
        assert '{% include "_classroom_subnav.html" %}' in template


def test_components_show_classroom_navigation_only_for_oneroster_deep_link():
    template = (TEMPLATES / "components.html").read_text(encoding="utf-8")
    deep_link_start = template.index("{% if deep_link %}")
    deep_link_end = template.index("{% endif %}", deep_link_start)
    include = template.index('{% include "_classroom_subnav.html" %}')

    assert deep_link_start < include < deep_link_end
    assert template.count('{% include "_classroom_subnav.html" %}') == 1


def test_teacher_access_is_not_a_global_or_groups_promotional_link():
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    groups = (TEMPLATES / "groups.html").read_text(encoding="utf-8")

    assert 'href="/classroom/access"' not in base
    assert 'href="/classroom/access"' not in groups
    assert 'href="/classroom">Classroom</a>' in base
