"""Public, server-rendered (crawlable) Wiki pages.

The in-app Wiki (``ui/main-panel/wiki/``) is a single-page surface: the browser
fetches article JSON from ``/api/v1/wiki`` and renders the Markdown body
client-side, so a search crawler that doesn't run JavaScript only sees an empty
app shell. This module adds a SECOND, read-only doorway to the SAME data — real
HTML pages at ``/wiki`` (index) and ``/wiki/<slug>`` (one article), drawn live
from ``data/wiki.db`` on every request. There is NO build/regeneration step:
edit or publish an article in the app and the next visit (or crawl) reflects it,
because the page IS the database rendered on the spot.

Only PUBLISHED articles are ever rendered here — drafts stay members-only. The
visibility gate is simply the store's ``include_drafts=False`` read path (a
public/anonymous caller, which is exactly what a crawler is). Wiring lives in
``app/main.py`` (the ``/wiki`` + ``/wiki/<slug>`` routes and the sitemap), which
passes in the SEO ``<head>`` block — its single source of truth — and the
configured origin, so this module never imports the app shell (no circular
import). Deleting those routes leaves the in-app Wiki and its API untouched, so
this whole surface is drop-in.

Markdown is converted by a small, dependency-free, **escape-FIRST** renderer
(``_render_markdown``): the body is HTML-escaped before any tags are introduced,
and only a known, self-generated tag set is emitted — so article text can never
inject markup (no server-side ``marked``/``DOMPurify`` needed). It covers the
common subset authors use (headings, bold/italic, inline + fenced code, links,
``[[wiki-links]]``, lists, blockquotes, rules, images) — close to the in-app
reader, deliberately not a full CommonMark parser.
"""
from __future__ import annotations

import re
from datetime import datetime
from html import escape as _html_escape
from typing import Dict, List, Optional


def _esc(s) -> str:
    """HTML-escape for both text content and attribute values (quote=True)."""
    return _html_escape("" if s is None else str(s), quote=True)


# ── Markdown rendering (escape-first; safe by construction) ──────────────────

_SAFE_URL_SCHEME = re.compile(r"^(https?:|mailto:|tel:)", re.IGNORECASE)


def _safe_url(url: str) -> str:
    """Return ``url`` only if its scheme is safe, else ``#``. Relative paths,
    anchors and protocol-relative urls pass; ``javascript:``/``data:`` and other
    schemes are rejected. The url has already been HTML-escaped by the caller."""
    u = (url or "").strip()
    if not u:
        return "#"
    if u.startswith("/") or u.startswith("#") or u.startswith("//"):
        return u
    if _SAFE_URL_SCHEME.match(u):
        return u
    # A bare "word:" before the first slash is an unknown/unsafe scheme.
    if ":" in u.split("/", 1)[0]:
        return "#"
    return u  # no scheme → treat as relative


def _md_inline(text: str, link_index: Dict[str, str]) -> str:
    """Render inline Markdown for one already-RAW text run. Escapes first, then
    stashes code-spans / links / images as placeholders (so emphasis can't bleed
    into them), applies bold + italic, and restores the placeholders."""
    s = _esc(text)
    stash: List[str] = []

    def _stash(html: str) -> str:
        stash.append(html)
        return f"\x00P{len(stash) - 1}\x00"

    # `inline code` (content already escaped)
    s = re.sub(r"`([^`]+)`", lambda m: _stash(f"<code>{m.group(1)}</code>"), s)

    # ![alt](url) images — before links, since the syntax overlaps
    def _img(m):
        return _stash(
            f'<img src="{_safe_url(m.group(2))}" alt="{m.group(1)}" loading="lazy">'
        )

    s = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)\)", _img, s)

    # [[Target]] / [[Target|label]] wiki-links → other public pages
    def _wiki(m):
        inner = m.group(1)
        bar = inner.find("|")
        target = (inner[:bar] if bar >= 0 else inner).strip()
        label = (inner[bar + 1:] if bar >= 0 else inner).strip()
        if not target:
            return m.group(0)
        slug = link_index.get(target.lower())
        if slug:
            return _stash(f'<a href="/wiki/{_esc(slug)}">{label}</a>')
        # Unknown / unpublished target — no public page to link to.
        return _stash(f'<span class="pw-wikilink-missing">{label}</span>')

    s = re.sub(r"\[\[([^\]]+)\]\]", _wiki, s)

    # [label](url) standard links
    def _link(m):
        url = _safe_url(m.group(2))
        ext = url.startswith("http")
        attrs = ' target="_blank" rel="noopener noreferrer nofollow"' if ext else ""
        return _stash(f'<a href="{url}"{attrs}>{m.group(1)}</a>')

    s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", _link, s)

    # Emphasis (bold before italic).
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"__([^_]+)__", r"<strong>\1</strong>", s)
    s = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", s)
    s = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"<em>\1</em>", s)

    s = re.sub(r"\x00P(\d+)\x00", lambda m: stash[int(m.group(1))], s)
    return s


def _is_block_start(line: str) -> bool:
    """A line that begins a new block (so paragraph gathering must stop)."""
    return bool(
        re.match(r"^\s*```", line)
        or re.match(r"^#{1,6}\s+", line)
        or re.match(r"^\s*([-*_])\1\1+\s*$", line)
        or re.match(r"^\s*>", line)
        or re.match(r"^\s*[-*+]\s+", line)
        or re.match(r"^\s*\d+\.\s+", line)
    )


def _render_markdown(body: str, link_index: Optional[Dict[str, str]] = None) -> str:
    """Convert an article body (Markdown + ``[[links]]``) to safe HTML."""
    link_index = link_index or {}
    lines = (body or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: List[str] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]

        # Fenced code block ``` … ```
        m = re.match(r"^\s*```(.*)$", line)
        if m:
            lang = m.group(1).strip()
            i += 1
            buf: List[str] = []
            while i < n and not re.match(r"^\s*```\s*$", lines[i]):
                buf.append(lines[i])
                i += 1
            i += 1  # consume closing fence (or fall off the end)
            cls = f' class="language-{_esc(lang)}"' if lang else ""
            out.append(f"<pre><code{cls}>{_esc(chr(10).join(buf))}</code></pre>")
            continue

        if not line.strip():
            i += 1
            continue

        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{_md_inline(m.group(2).strip(), link_index)}</h{lvl}>")
            i += 1
            continue

        if re.match(r"^\s*([-*_])\1\1+\s*$", line):
            out.append("<hr>")
            i += 1
            continue

        if re.match(r"^\s*>", line):
            buf = []
            while i < n and re.match(r"^\s*>", lines[i]):
                buf.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            inner = "<br>".join(_md_inline(b, link_index) for b in buf)
            out.append(f"<blockquote>{inner}</blockquote>")
            continue

        if re.match(r"^\s*[-*+]\s+", line):
            buf = []
            while i < n and re.match(r"^\s*[-*+]\s+", lines[i]):
                item = re.sub(r"^\s*[-*+]\s+", "", lines[i])
                buf.append(f"<li>{_md_inline(item, link_index)}</li>")
                i += 1
            out.append("<ul>" + "".join(buf) + "</ul>")
            continue

        if re.match(r"^\s*\d+\.\s+", line):
            buf = []
            while i < n and re.match(r"^\s*\d+\.\s+", lines[i]):
                item = re.sub(r"^\s*\d+\.\s+", "", lines[i])
                buf.append(f"<li>{_md_inline(item, link_index)}</li>")
                i += 1
            out.append("<ol>" + "".join(buf) + "</ol>")
            continue

        # Paragraph — gather consecutive lines until a blank / new block.
        buf = []
        while i < n and lines[i].strip() and not _is_block_start(lines[i]):
            buf.append(lines[i])
            i += 1
        out.append("<p>" + "<br>".join(_md_inline(b, link_index) for b in buf) + "</p>")

    return "\n".join(out)


# ── Small derived helpers (description, reading time, dates) ─────────────────

def make_description(body: str, limit: int = 160) -> str:
    """A plain-text meta-description from a Markdown body: strip markup, collapse
    whitespace, truncate on a word boundary. Used for <meta>/Open Graph."""
    text = body or ""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"\[\[([^\]|]+)(\|[^\]]+)?\]\]", r"\1", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[#*_`>]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip() + "…"
    return text or "A WebAgent wiki article."


def _reading_time(body: str) -> int:
    return max(1, round(len((body or "").split()) / 200))


def _fmt_date(iso: str) -> str:
    """ISO timestamp → "Jun 23, 2026" (cross-platform; no %-d)."""
    s = (iso or "").strip()
    if not s:
        return ""
    try:
        dt = datetime.strptime(s[:10], "%Y-%m-%d")
        return f"{dt.strftime('%b')} {dt.day}, {dt.year}"
    except Exception:
        return s[:10]


# ── Page shell (standalone, themed like the app) ─────────────────────────────

# Applied as the FIRST child of <body> so it runs before the content paints: read
# the visitor's saved theme (same key the app uses, webagent_theme) and flip to
# light if chosen. Default is dark (the app's :root theme). Mirrors how the
# server-rendered landing page is themed.
_THEME_BOOT = (
    "<script>(function(){try{var t=localStorage.getItem('webagent_theme')||'dark';"
    "var light=t==='light'||(t==='system'&&window.matchMedia&&"
    "window.matchMedia('(prefers-color-scheme: light)').matches);"
    "if(light&&document.body)document.body.classList.add('light-mode');}"
    "catch(e){}})();</script>\n"
)

# Compact, theme-driven stylesheet for the public pages. Every colour is a
# design-system variable (never a raw hex) so it tracks dark + light + the admin
# palette exactly like the rest of the app.
_PAGE_CSS = """
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{background:var(--bg-0);color:var(--fg-1);
  font-family:var(--font-sans,'Inter',system-ui,-apple-system,sans-serif);
  line-height:1.65;-webkit-font-smoothing:antialiased}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.pw-wrap{max-width:760px;margin:0 auto;padding:24px 20px 80px}
.pw-top{display:flex;align-items:center;justify-content:space-between;gap:16px;
  padding:8px 0 20px;margin-bottom:28px;
  border-bottom:var(--border-width) solid var(--border)}
.pw-brand{font-weight:700;font-size:1.05rem;color:var(--fg-1)}
.pw-top-links{display:flex;gap:18px;font-size:.9rem}
.pw-top-links a{color:var(--fg-3)}
.pw-cat{display:inline-block;font-size:.76rem;font-weight:600;letter-spacing:.05em;
  text-transform:uppercase;color:var(--accent);margin-bottom:8px}
.pw-article h1,.pw-index-head h1{font-size:2rem;line-height:1.2;margin:0 0 12px}
.pw-meta{color:var(--fg-3);font-size:.88rem;margin:0 0 28px}
.pw-lead{color:var(--fg-2);font-size:1.05rem;margin:0 0 28px}
.pw-article h2{font-size:1.4rem;margin:34px 0 12px;padding-bottom:6px;
  border-bottom:var(--border-width) solid var(--border-soft)}
.pw-article h3{font-size:1.16rem;margin:24px 0 10px}
.pw-article h4,.pw-article h5,.pw-article h6{margin:20px 0 8px}
.pw-article p{margin:0 0 16px}
.pw-article ul,.pw-article ol{margin:0 0 16px;padding-left:24px}
.pw-article li{margin:5px 0}
.pw-article code{font-family:var(--font-mono,'JetBrains Mono',ui-monospace,monospace);
  font-size:.88em;background:var(--bg-2);
  border:var(--border-width) solid var(--border-soft);
  border-radius:var(--r-xs);padding:.12em .35em}
.pw-article pre{background:var(--bg-2);border:var(--border-width) solid var(--border);
  border-radius:var(--r-md);padding:14px 16px;overflow:auto;margin:0 0 16px}
.pw-article pre code{background:none;border:none;padding:0;font-size:.85rem}
.pw-article blockquote{margin:0 0 16px;padding:4px 16px;color:var(--fg-2);
  border-left:3px solid var(--accent-line)}
.pw-article img{max-width:100%;height:auto;border-radius:var(--r-md)}
.pw-article hr{border:none;border-top:var(--border-width) solid var(--border);margin:28px 0}
.pw-article a.pw-wikilink-missing,.pw-wikilink-missing{color:var(--fg-2)}
.pw-tags{margin:30px 0 0;display:flex;flex-wrap:wrap;gap:8px}
.pw-tag{font-size:.8rem;color:var(--fg-3);background:var(--bg-2);
  border:var(--border-width) solid var(--border-soft);
  border-radius:var(--r-pill);padding:3px 11px}
.pw-related{margin-top:34px;padding-top:18px;
  border-top:var(--border-width) solid var(--border-soft)}
.pw-related-title{font-size:.78rem;text-transform:uppercase;letter-spacing:.05em;
  color:var(--fg-3);margin-bottom:8px}
.pw-related-list{display:flex;flex-direction:column;gap:6px}
.pw-foot{margin-top:50px;padding-top:18px;color:var(--fg-3);font-size:.85rem;
  border-top:var(--border-width) solid var(--border)}
.pw-index{list-style:none;padding:0;margin:0 0 8px}
.pw-index li{padding:14px 0;border-bottom:var(--border-width) solid var(--border-soft)}
.pw-index a{font-size:1.05rem;font-weight:600}
.pw-snip{color:var(--fg-3);font-size:.9rem;margin:5px 0 0}
.pw-group-title{font-size:1.2rem;margin:30px 0 2px}
.pw-empty{color:var(--fg-3);margin:24px 0}
"""


def _page_shell(*, title: str, head_html: str, body_html: str) -> str:
    """Wrap page content in a complete, themed HTML document — same asset set as
    the server-rendered landing page (design-system tokens + live appearance
    overrides), plus the public-page stylesheet."""
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">\n'
        f"<title>{_esc(title)}</title>\n"
        + head_html
        + '<meta name="theme-color" content="#0d0d1a">\n'
        '<link rel="icon" href="/ui/favicon.svg" type="image/svg+xml">\n'
        '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap">\n'
        '<link rel="stylesheet" href="/ui/shared/css/design-system.css">\n'
        '<script src="/ui/shared/js/appearance.js"></script>\n'
        f"<style>{_PAGE_CSS}</style>\n"
        "</head>\n<body>\n"
        + _THEME_BOOT
        + body_html
        + "\n</body>\n</html>\n"
    )


def _top_nav(app_link: Optional[str] = None) -> str:
    """Shared top bar: brand → home, plus Wiki index and (optionally) an
    "Open in app" deep link into the SPA."""
    extra = f'<a href="{_esc(app_link)}">Open in app</a>' if app_link else ""
    return (
        '<nav class="pw-top">\n'
        '  <a class="pw-brand" href="/">WebAgent</a>\n'
        f'  <span class="pw-top-links"><a href="/wiki">Wiki</a>{extra}</span>\n'
        "</nav>\n"
    )


# ── Page builders ────────────────────────────────────────────────────────────

def build_article_html(
    *,
    article: dict,
    related: List[dict],
    link_index: Dict[str, str],
    origin: str,
    head_html: str,
    title: str,
) -> str:
    """Full HTML page for one published article."""
    a = article or {}
    slug = a.get("slug") or ""
    body = a.get("body") or ""
    cat = (a.get("category") or "").strip()
    tags = a.get("tags") if isinstance(a.get("tags"), list) else []
    when = _fmt_date(a.get("updated_at") or a.get("created_at") or "")

    meta_bits = []
    if when:
        meta_bits.append(f"Updated {_esc(when)}")
    if body.strip():
        meta_bits.append(f"{_reading_time(body)} min read")
    meta = " · ".join(meta_bits)

    cat_html = f'<div class="pw-cat">{_esc(cat)}</div>\n' if cat else ""
    body_html = _render_markdown(body, link_index) if body.strip() else "<p class='pw-empty'>This article has no content yet.</p>"

    tags_html = ""
    if tags:
        chips = "".join(f'<span class="pw-tag">#{_esc(t)}</span>' for t in tags)
        tags_html = f'<div class="pw-tags">{chips}</div>\n'

    related_html = ""
    rel = [r for r in (related or []) if r.get("slug")]
    if rel:
        links = "".join(
            f'<a href="/wiki/{_esc(r["slug"])}">{_esc(r.get("title") or r["slug"])}</a>'
            for r in rel
        )
        related_html = (
            '<div class="pw-related">\n'
            '  <div class="pw-related-title">Linked from</div>\n'
            f'  <div class="pw-related-list">{links}</div>\n'
            "</div>\n"
        )

    # Raw slug here — _top_nav() escapes the whole URL exactly once.
    app_link = f"/?tab=wiki&article={slug}" if slug else "/?tab=wiki"
    inner = (
        '<div class="pw-wrap">\n'
        + _top_nav(app_link)
        + '<article class="pw-article">\n'
        + cat_html
        + f"<h1>{_esc(a.get('title') or 'Untitled')}</h1>\n"
        + (f'<div class="pw-meta">{meta}</div>\n' if meta else "")
        + body_html
        + tags_html
        + related_html
        + "</article>\n"
        + '<footer class="pw-foot">From the <a href="/wiki">WebAgent Wiki</a>.</footer>\n'
        + "</div>\n"
    )
    return _page_shell(title=title, head_html=head_html, body_html=inner)


def build_index_html(
    *, articles: List[dict], origin: str, head_html: str, title: str
) -> str:
    """Full HTML page listing every published article, grouped by category."""
    arts = [a for a in (articles or []) if a.get("slug")]

    if not arts:
        listing = '<p class="pw-empty">No published articles yet.</p>\n'
    else:
        groups: Dict[str, List[dict]] = {}
        for a in arts:
            key = (a.get("category") or "").strip() or "General"
            groups.setdefault(key, []).append(a)
        # Alphabetical categories, but keep "General" last.
        cats = sorted(groups.keys(), key=lambda c: (c == "General", c.lower()))
        parts: List[str] = []
        for cat in cats:
            items = sorted(groups[cat], key=lambda x: (x.get("title") or "").lower())
            rows = []
            for a in items:
                snip = (a.get("snippet") or "").replace("\n", " ").strip()
                snip = re.sub(r"\s+", " ", snip)
                snip_html = f'<p class="pw-snip">{_esc(snip)}</p>' if snip else ""
                rows.append(
                    f'<li><a href="/wiki/{_esc(a["slug"])}">{_esc(a.get("title") or "Untitled")}</a>{snip_html}</li>'
                )
            parts.append(
                f'<h2 class="pw-group-title">{_esc(cat)}</h2>\n'
                '<ul class="pw-index">' + "".join(rows) + "</ul>\n"
            )
        listing = "".join(parts)

    inner = (
        '<div class="pw-wrap">\n'
        + _top_nav("/?tab=wiki")
        + '<header class="pw-index-head">\n'
        + "<h1>Wiki</h1>\n"
        + '<p class="pw-lead">The shared knowledge base — published guides, '
        "policies and reference.</p>\n"
        + "</header>\n"
        + listing
        + '<footer class="pw-foot">Powered by <a href="/">WebAgent</a>.</footer>\n'
        + "</div>\n"
    )
    return _page_shell(title=title, head_html=head_html, body_html=inner)


def build_notfound_html(*, origin: str, head_html: str) -> str:
    """A small, themed 404 for a missing or non-public article slug."""
    inner = (
        '<div class="pw-wrap">\n'
        + _top_nav()
        + '<article class="pw-article">\n'
        + "<h1>Not found</h1>\n"
        + '<p class="pw-lead">This wiki article doesn’t exist or isn’t public.</p>\n'
        + '<p><a href="/wiki">← Browse the wiki</a></p>\n'
        + "</article>\n"
        + "</div>\n"
    )
    return _page_shell(title="Not found — WebAgent Wiki", head_html=head_html, body_html=inner)
