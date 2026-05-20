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

# GFM pipe-table detection. The separator line is the unambiguous signal:
# ``|---|---|`` (with optional alignment colons). Without it we treat a
# pipe-bearing line as ordinary prose, which avoids false positives on
# things like ``foo | bar`` written in regular text.
_TABLE_ROW_RE = re.compile(r"^[ \t]*\|.*\|[ \t]*$|^[ \t]*\|[^\n]+$")
_TABLE_SEPARATOR_RE = re.compile(
    r"^[ \t]*\|?[ \t]*:?-{2,}:?[ \t]*"
    r"(?:\|[ \t]*:?-{2,}:?[ \t]*)+\|?[ \t]*$"
)

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

    # 10. Tables → bullet list (2-col) or padded <pre> block (3+-col).
    #     Runs last so cells already have <b>/<i>/<code>/<a> wrapped,
    #     and we stash the result so the placeholder-restore loop
    #     below treats it as opaque.
    text = _convert_tables(text, stash)

    # 11. Restore stashed snippets. Run in a loop because a link
    #     placeholder embeds a URL placeholder, and a stashed table
    #     can embed inline-code placeholders.
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
    # Tables → flatten. 2-col tables become "• key: value" lines; wider
    # tables become " | "-joined cells. The point is to drop the
    # separator row and the surrounding pipes so the user doesn't see
    # raw markdown in the fallback path.
    text = _strip_tables(text)
    # Bold markers.
    text = re.sub(r"\*\*|__", "", text)
    # Italic markers (same boundary rules as the HTML path).
    text = _ITALIC_STAR_RE.sub(r"\1", text)
    text = _ITALIC_UNDER_RE.sub(r"\1", text)
    # Strikethrough markers.
    text = _STRIKE_RE.sub(r"\1", text)
    return text


def _strip_tables(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        if (
            i + 1 < n
            and _TABLE_ROW_RE.match(lines[i])
            and "|" in lines[i]
            and _TABLE_SEPARATOR_RE.match(lines[i + 1])
        ):
            header_cells = _split_table_row(lines[i])
            sep_cells = _split_table_row(lines[i + 1])
            if len(header_cells) != len(sep_cells) or len(header_cells) < 2:
                out.append(lines[i])
                i += 1
                continue

            body: list[list[str]] = []
            j = i + 2
            while j < n and _TABLE_ROW_RE.match(lines[j]) and "|" in lines[j]:
                row = _split_table_row(lines[j])
                if len(row) < len(header_cells):
                    row += [""] * (len(header_cells) - len(row))
                elif len(row) > len(header_cells):
                    row = row[: len(header_cells)]
                body.append(row)
                j += 1

            if len(header_cells) == 2:
                if body:
                    out.extend(f"• {row[0]}: {row[1]}" for row in body)
                else:
                    out.append(f"{header_cells[0]}: {header_cells[1]}")
            else:
                out.append(" | ".join(header_cells))
                out.extend(" | ".join(r) for r in body)
            i = j
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out)


def _looks_like_url(s: str) -> bool:
    return s.startswith(("http://", "https://", "tg://", "mailto:"))


def _bold_key(cell: str) -> str:
    """Emphasise a 2-col bullet's key cell without double-wrapping.

    If the cell already contains formatting — either an inline tag
    injected by an earlier pass (``<b>``/``<i>``/``<a>``...) or a
    stashed placeholder that will expand to ``<code>``/``<pre>``/a
    URL — leave it alone. Otherwise wrap in ``<b>``.
    """
    if "<" in cell or "\x00PH" in cell:
        return cell
    return f"<b>{cell}</b>"


def _split_table_row(row: str) -> list[str]:
    """Split a pipe-delimited row into trimmed cells.

    Surrounding pipes (``| a | b |``) and trailing whitespace are
    stripped. Cells are kept verbatim otherwise — including any HTML
    tags introduced by earlier passes.
    """
    stripped = row.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _convert_tables(text: str, stash):
    """Detect GFM pipe tables and replace them with stashed renderings.

    Two-column tables → bullet list (``• <b>k</b>: v``). Three-or-more
    column tables → ``<pre>`` block with cells padded to the widest
    value in their column so columns line up under Telegram's
    monospace renderer.

    Lines that aren't part of a table are passed through unchanged.
    """
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        # A table requires a row line followed immediately by a
        # separator line. Both must contain at least one ``|``.
        if (
            i + 1 < n
            and _TABLE_ROW_RE.match(lines[i])
            and "|" in lines[i]
            and _TABLE_SEPARATOR_RE.match(lines[i + 1])
        ):
            header_cells = _split_table_row(lines[i])
            sep_cells = _split_table_row(lines[i + 1])
            # The header and separator must agree on column count for
            # the block to be a real GFM table. Otherwise we treat the
            # current line as ordinary prose and move on.
            if len(header_cells) != len(sep_cells) or len(header_cells) < 2:
                out.append(lines[i])
                i += 1
                continue

            body: list[list[str]] = []
            j = i + 2
            while j < n and _TABLE_ROW_RE.match(lines[j]) and "|" in lines[j]:
                row_cells = _split_table_row(lines[j])
                # Pad / truncate to header width so misaligned rows
                # don't crash the renderer.
                if len(row_cells) < len(header_cells):
                    row_cells += [""] * (len(header_cells) - len(row_cells))
                elif len(row_cells) > len(header_cells):
                    row_cells = row_cells[: len(header_cells)]
                body.append(row_cells)
                j += 1

            rendered = _render_table(header_cells, body)
            out.append(stash(rendered))
            i = j
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out)


def _render_table(header: list[str], body: list[list[str]]) -> str:
    """Render a parsed table into Telegram-flavoured HTML."""
    if len(header) == 2:
        # 2-column → bullet list. Drop the header; LLM-emitted 2-col
        # tables are almost always "name → description" and the values
        # are self-describing.
        if not body:
            # Header-only edge case: surface it as a single bold line.
            return f"{_bold_key(header[0])}: {header[1]}"
        lines = [f"• {_bold_key(row[0])}: {row[1]}" for row in body]
        return "\n".join(lines)

    # 3+ columns → padded monospace block. We pad on display-width
    # ignoring tags, but the cells in this codebase only contain
    # HTML if earlier passes injected ``<b>``/``<i>``/``<code>`` —
    # Telegram renders those as zero-width formatting in ``<pre>`` so
    # padding by raw character length over-aligns slightly. That's
    # acceptable for a chat client; we don't want a full HTML
    # length-stripping pass here.
    rows = [header, *body]
    col_widths = [max(len(row[c]) for row in rows) for c in range(len(header))]
    sep_row = "-+-".join("-" * w for w in col_widths)

    def fmt_row(row: list[str]) -> str:
        return " | ".join(row[c].ljust(col_widths[c]) for c in range(len(header)))

    lines = [fmt_row(header), sep_row]
    lines.extend(fmt_row(r) for r in body)
    return "<pre>" + "\n".join(lines) + "</pre>"
