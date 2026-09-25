from django import template

register = template.Library()

# Worst first: one modal can carry several messages, and it is headed by the
# most serious of them so a warning is never presented under a "Success" title.
_SEVERITY = ("error", "warning", "success", "info")

_COPY = {
    "error": ("Something went wrong", "Close"),
    "warning": ("Please check this", "Close"),
    "success": ("Success", "Done"),
    "info": ("Notice", "Close"),
}


@register.simple_tag
def flash_summary(items, level=""):
    """Level, title and dismiss label for the flash modal.

    `items` is either the messages framework's storage (each item has a
    level_tag) or a plain list of strings, which take `level` instead.
    Anything unrecognised — debug, a custom level — reads as info.
    """
    seen = set()
    for item in items:
        tag = getattr(item, "level_tag", None) or level or "info"
        seen.add(tag if tag in _COPY else "info")
    worst = next((tag for tag in _SEVERITY if tag in seen), "info")
    title, button = _COPY[worst]
    return {"level": worst, "title": title, "button": button}
