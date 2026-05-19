r"""Markdown → Telegram conversion for outbound messages.

Telegram's ``parse_mode="Markdown"`` (legacy) only understands a tiny
subset of formatting: single-asterisk ``*bold*``, single-underscore
``_italic_``, inline backticks, fenced ``` ``` blocks, and
``[text](url)``. LLM output is almost always standard CommonMark —
``**bold**``, ``# headings``, ``- bullets`` — which Telegram renders as
raw characters under legacy Markdown mode.

We therefore send with ``parse_mode="HTML"`` and pre-render LLM
markdown into the small subset of HTML Telegram accepts:

    <b>, <i>, <u>, <s>, <a href="…">, <code>, <pre>,
    <pre><code class="language-…">, <blockquote>, <tg-spoiler>

``md_to_telegram_html`` performs that conversion. ``strip_markdown``
gives a clean plain-text fallback for the rare case Telegram still
rejects the rendered HTML (e.g. exotic content nested inside code).

Both helpers are deliberately regex-based and dependency-free — the App
container already pulls in ``httpx``; we don't want to add a markdown
library just for chat output.
"""

from __future__ import annotations

import html
import re

__all__ = ["md_to_telegram_html", "strip_markdown"]


_FENCE_RE = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+?)`")
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
_HEADING_RE = re.compile(r"^[ \t]*(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)
_BULLET_RE = re.compile(r"^([ \t]*)[-*+][ \t]+", re.MULTILINE)
# Bold+italic combo: ``***word***`` / ``___word___`` — must run BEFORE bold,
# otherwise the bold regex eats one ``*`` and leaves an orphaned marker.
_BOLD_ITALIC_STAR_RE = re.compile(r"\*\*\*(?!\s)([^\n]+?)(?<!\s)\*\*\*")
_BOLD_ITALIC_UNDER_RE = re.compile(r"___(?!\s)([^\n]+?)(?<!\s)___")
_BOLD_RE = re.compile(
    r"(?<!\w)(?:\*\*|__)(\S(?:.*?\S)?)(?:\*\*|__)(?!\w)",
    re.DOTALL,
)
# Italic with * — allows mid-word per CommonMark, but disallows ** runs and
# refuses to span across HTML tags introduced by earlier passes.
_ITALIC_STAR_RE = re.compile(r"(?<!\*)\*(?!\s|\*)([^*\n<>]+?)(?<!\s)\*(?!\*)")
# Italic with _ — strict word boundaries so ``snake_case_var`` stays literal.
_ITALIC_UNDER_RE = re.compile(r"(?<![\w_])_(?!\s|_)([^_\n<>]+?)(?<!\s)_(?![\w_])")
_STRIKE_RE = re.compile(r"~~(?!\s)([^~\n]+?)(?<!\s)~~")

_PLACEHOLDER_RE = re.compile(r"\x00PH(\d+)\x00")


def md_to_telegram_html(text: str) -> str:
    """Convert LLM-style markdown to Telegram's HTML subset.

    The conversion is best-effort: anything we don't recognise is
    passed through after HTML-escaping ``&``, ``<``, ``>``. Code
    blocks and inline code are preserved verbatim (HTML-escaped, never
    re-interpreted as markdown).
    """
    if not text:
        return ""

    placeholders: list[str] = []

    def stash(snippet: str) -> str:
        placeholders.append(snippet)
        return f"\x00PH{len(placeholders) - 1}\x00"

    def fence_sub(m: re.Match[str]) -> str:
        lang = (m.group(1) or "").strip()
        body = m.group(2)
        escaped = html.escape(body, quote=False)
        if lang:
            return stash(
                f'<pre><code class="language-{html.escape(lang, quote=True)}">'
                f"{escaped}</code></pre>"
            )
        return stash(f"<pre>{escaped}</pre>")

    # 1. Stash fenced code blocks (must run before inline code).
    text = _FENCE_RE.sub(fence_sub, text)

    # 2. Stash inline code.
    text = _INLINE_CODE_RE.sub(
        lambda m: stash(f"<code>{html.escape(m.group(1), quote=False)}</code>"),
        text,
    )

    # 3. HTML-escape the remaining body. Code placeholders survive
    #    because they contain only ASCII control + digits.
    text = html.escape(text, quote=False)

    # 4. Headings → bold line.
    text = _HEADING_RE.sub(lambda m: f"<b>{m.group(2).strip()}</b>", text)

    # 5. Unordered list bullets ("-", "*", "+") → "•".
    #    Must run BEFORE italic, otherwise a leading "* item" would
    #    be eaten as a half-open emphasis marker.
    text = _BULLET_RE.sub(lambda m: f"{m.group(1)}• ", text)

    # 6a. Bold+italic combo first so ``***word***`` becomes <b><i>…</i></b>.
    text = _BOLD_ITALIC_STAR_RE.sub(lambda m: f"<b><i>{m.group(1)}</i></b>", text)
    text = _BOLD_ITALIC_UNDER_RE.sub(lambda m: f"<b><i>{m.group(1)}</i></b>", text)

    # 6b. Bold (** or __).
    text = _BOLD_RE.sub(lambda m: f"<b>{m.group(1)}</b>", text)

    # 7. Italic.
    text = _ITALIC_STAR_RE.sub(lambda m: f"<i>{m.group(1)}</i>", text)
    text = _ITALIC_UNDER_RE.sub(lambda m: f"<i>{m.group(1)}</i>", text)

    # 8. Strikethrough.
    text = _STRIKE_RE.sub(lambda m: f"<s>{m.group(1)}</s>", text)

    # 9. Links — the URL is stashed so bold/italic above can never see
    #    asterisks/underscores inside an http(s) URL. The visible label
    #    has already gone through bold/italic conversion. ``&``, ``<``,
    #    ``>`` in the URL were already escaped by the global pass above,
    #    so we only need to neutralise quotes inside the href attribute.
    def link_sub(m: re.Match[str]) -> str:
        label = m.group(1)
        url = m.group(2)
        if not _looks_like_url(url):
            return m.group(0)
        safe_url = url.replace('"', "&quot;").replace("'", "&#x27;")
        url_token = stash(safe_url)
        return f'<a href="{url_token}">{label}</a>'

    text = _LINK_RE.sub(link_sub, text)

    # 10. Restore stashed snippets. Run in a loop because a link
    #     placeholder embeds a URL placeholder.
    while _PLACEHOLDER_RE.search(text):
        text = _PLACEHOLDER_RE.sub(lambda m: placeholders[int(m.group(1))], text)

    return text


def strip_markdown(text: str) -> str:
    """Best-effort markdown → plain text for the parse-mode-less fallback."""
    if not text:
        return ""
    # Fenced code blocks → keep body, drop fences.
    text = _FENCE_RE.sub(lambda m: m.group(2), text)
    # Inline code → keep contents.
    text = _INLINE_CODE_RE.sub(r"\1", text)

    # Links → "label (url)" when the URL looks real, else just label.
    def _link(m: re.Match[str]) -> str:
        label, url = m.group(1), m.group(2)
        return f"{label} ({url})" if _looks_like_url(url) else label

    text = _LINK_RE.sub(_link, text)
    # Headings → plain line.
    text = _HEADING_RE.sub(lambda m: m.group(2).strip(), text)
    # Bullets → "• ".
    text = _BULLET_RE.sub(lambda m: f"{m.group(1)}• ", text)
    # Bold markers.
    text = re.sub(r"\*\*|__", "", text)
    # Italic markers (same boundary rules as the HTML path).
    text = _ITALIC_STAR_RE.sub(r"\1", text)
    text = _ITALIC_UNDER_RE.sub(r"\1", text)
    # Strikethrough markers.
    text = _STRIKE_RE.sub(r"\1", text)
    return text


def _looks_like_url(s: str) -> bool:
    return s.startswith(("http://", "https://", "tg://", "mailto:"))
