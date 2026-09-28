"""Seed content for the default `home` page rendered into a new user's
Gen UI tab iframe on first visit.

The actual template lives next to this file as `home.html` — a plain
self-contained HTML document so it can be opened in a browser, edited
with HTML tooling, and reviewed without Python-string escape gymnastics.
We read it once at module load.

Theme: dark by default. Parent posts {type:'theme', value:'light'|'dark'}
via window.postMessage and the page toggles `<html class="light">` to
swap palettes. Falls back to `prefers-color-scheme` when opened
standalone."""

import os

_TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "home.html")

with open(_TEMPLATE_PATH, "r", encoding="utf-8") as _f:
    _HOME_HTML = _f.read()


def home_genui_html() -> str:
    """Return the seed HTML for a new user's home genui."""
    return _HOME_HTML
