from django import template

register = template.Library()


@register.inclusion_tag("core/partials/local-datetime.html")
def local_datetime(value):
    """Render a datetime as a <time> element to be localized in the browser.

    The element carries the UTC value in its `datetime` attribute (machine
    readable) and the server-formatted UTC text as fallback content. The
    `localtime.js` script rewrites the visible text to the viewer's timezone.
    """
    return {"value": value}
