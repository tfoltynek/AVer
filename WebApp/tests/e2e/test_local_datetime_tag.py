from datetime import UTC, datetime

from django.template import Context, Template


def _render(value):
    return Template(
        "{% load datetime_extras %}{% local_datetime value %}"
    ).render(Context({"value": value}))


def test_renders_time_element_with_iso_and_fallback():
    dt = datetime(2026, 6, 1, 14, 30, tzinfo=UTC)
    html = _render(dt)
    assert 'class="js-localdt"' in html
    assert 'datetime="2026-06-01T14:30:00+00:00"' in html
    assert "01.06.2026 14:30" in html


def test_renders_empty_for_none():
    assert _render(None).strip() == ""
