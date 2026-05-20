"""Unit tests for the markdown → Telegram-HTML converter.

These tests pin down the behaviour the Telegram channel relies on:
LLM-style markdown (``**bold**``, fenced code, headings, bullets, etc.)
must render into the small HTML subset Telegram accepts, and the
plain-text fallback must drop the markdown markers cleanly.
"""

from __future__ import annotations

from trevor_databricks.telegram_format import md_to_telegram_html, strip_markdown


def test_empty_string_round_trips():
    assert md_to_telegram_html("") == ""
    assert strip_markdown("") == ""


def test_plain_text_only_escapes_html_specials():
    src = "5 < 10 & 10 > 3"
    assert md_to_telegram_html(src) == "5 &lt; 10 &amp; 10 &gt; 3"


def test_double_asterisks_become_bold():
    assert md_to_telegram_html("**hello**") == "<b>hello</b>"


def test_double_underscores_become_bold():
    assert md_to_telegram_html("__hello__") == "<b>hello</b>"


def test_single_asterisk_becomes_italic():
    assert md_to_telegram_html("*hello*") == "<i>hello</i>"


def test_single_underscore_becomes_italic_at_word_boundary():
    assert md_to_telegram_html("a _word_ here") == "a <i>word</i> here"


def test_snake_case_is_not_italicised():
    # Common LLM hazard: snake_case_var must stay literal.
    assert md_to_telegram_html("snake_case_var") == "snake_case_var"


def test_arithmetic_asterisks_are_not_italicised():
    assert md_to_telegram_html("5 * 3 * 2 = 30") == "5 * 3 * 2 = 30"


def test_bold_inside_italic_combo():
    # ``***word***`` → <b><i>word</i></b> (bold claims the outer pair).
    out = md_to_telegram_html("***word***")
    assert out == "<b><i>word</i></b>"


def test_inline_code_is_preserved_and_escaped():
    out = md_to_telegram_html("use `a < b` carefully")
    assert out == "use <code>a &lt; b</code> carefully"


def test_fenced_code_block_with_language():
    src = "```python\nprint('hi')\n```"
    out = md_to_telegram_html(src)
    assert out == "<pre><code class=\"language-python\">print('hi')\n</code></pre>"


def test_fenced_code_block_without_language():
    src = "```\nplain\n```"
    out = md_to_telegram_html(src)
    assert out == "<pre>plain\n</pre>"


def test_fenced_code_escapes_html_and_ignores_markdown_inside():
    src = "```\n<script>**bold**</script>\n```"
    out = md_to_telegram_html(src)
    assert out == "<pre>&lt;script&gt;**bold**&lt;/script&gt;\n</pre>"


def test_headings_become_bold_lines():
    src = "# Heading 1\nbody\n## Heading 2"
    out = md_to_telegram_html(src)
    assert "<b>Heading 1</b>" in out
    assert "<b>Heading 2</b>" in out
    assert "#" not in out


def test_unordered_bullets_become_bullet_dots():
    src = "- one\n- two\n  - nested"
    out = md_to_telegram_html(src)
    assert out == "• one\n• two\n  • nested"


def test_link_rendered_as_html_anchor():
    src = "see [docs](https://example.com/x)"
    out = md_to_telegram_html(src)
    assert out == 'see <a href="https://example.com/x">docs</a>'


def test_link_with_query_string_escapes_ampersand():
    src = "[q](https://example.com/?a=1&b=2)"
    out = md_to_telegram_html(src)
    assert out == '<a href="https://example.com/?a=1&amp;b=2">q</a>'


def test_link_with_non_http_scheme_left_alone():
    src = "[weird](javascript:alert(1))"
    out = md_to_telegram_html(src)
    # Non-http(s)/tg/mailto URLs aren't wrapped; the raw markdown is
    # HTML-escaped instead so the user sees the literal text rather
    # than an unsafe anchor.
    assert "javascript:" in out
    assert "<a href=" not in out


def test_link_label_can_contain_bold():
    src = "[**bold label**](https://x.test/)"
    out = md_to_telegram_html(src)
    assert out == '<a href="https://x.test/"><b>bold label</b></a>'


def test_strikethrough_renders_as_s_tag():
    assert md_to_telegram_html("~~gone~~") == "<s>gone</s>"


def test_complex_llm_response_round_trip():
    src = (
        "# Summary\n"
        "Here is what I found about **bold** and *italic*:\n"
        "- item with `inline_code`\n"
        "- item with [link](https://example.com)\n"
        "\n"
        "```python\n"
        "x = 1 < 2\n"
        "```\n"
    )
    out = md_to_telegram_html(src)
    # Sanity-check: no raw markdown markers and no unsafe HTML.
    assert "<b>Summary</b>" in out
    assert "<b>bold</b>" in out
    assert "<i>italic</i>" in out
    assert "• item with <code>inline_code</code>" in out
    assert '<a href="https://example.com">link</a>' in out
    assert '<pre><code class="language-python">x = 1 &lt; 2\n</code></pre>' in out


def test_strip_markdown_drops_emphasis_markers():
    src = "**bold** and *italic* and _under_"
    assert strip_markdown(src) == "bold and italic and under"


def test_strip_markdown_keeps_link_text_and_url():
    src = "see [docs](https://example.com)"
    assert strip_markdown(src) == "see docs (https://example.com)"


def test_strip_markdown_preserves_code_block_body():
    src = "```python\nprint(1)\n```"
    assert "print(1)" in strip_markdown(src)
    assert "```" not in strip_markdown(src)


def test_strip_markdown_converts_bullets_to_dot():
    assert strip_markdown("- one\n- two") == "• one\n• two"


def test_two_column_table_renders_as_bullet_list():
    src = "| name | description |\n|------|-------------|\n| foo | first |\n| bar | second |"
    out = md_to_telegram_html(src)
    assert out == "• <b>foo</b>: first\n• <b>bar</b>: second"


def test_two_column_table_keeps_inline_code_in_cell():
    src = "| key | value |\n|-----|-------|\n| `id` | 42 |"
    out = md_to_telegram_html(src)
    # Inline code in the key cell already produces a tag, so the
    # bullet renderer must not add another <b> around it.
    assert out == "• <code>id</code>: 42"


def test_two_column_table_keeps_bold_in_cell_without_double_wrapping():
    src = "| name | description |\n|------|-------------|\n| **default** | auto-created |"
    out = md_to_telegram_html(src)
    assert out == "• <b>default</b>: auto-created"
    assert "<b><b>" not in out


def test_three_column_table_renders_as_padded_pre_block():
    src = "| name | type | count |\n|------|------|-------|\n| a | int | 5 |\n| bb | str | 10 |"
    out = md_to_telegram_html(src)
    assert out.startswith("<pre>")
    assert out.endswith("</pre>")
    # Header row appears verbatim with " | " column joiner.
    assert "name | type | count" in out
    # Separator row uses dashes + plus signs.
    assert "----+------+-----" in out
    # Column widths are computed from the widest cell (bb is 2 chars,
    # 10 is 2 chars), so the narrowest column is padded to 4 ("name").
    assert "a    | int  | 5" in out
    assert "bb   | str  | 10" in out


def test_three_column_table_escapes_html_inside_cells():
    src = "| col | a | b |\n|-----|---|---|\n| <x> | 1 | 2 |"
    out = md_to_telegram_html(src)
    assert "&lt;x&gt;" in out
    assert "<x>" not in out.replace("<pre>", "").replace("</pre>", "")


def test_plain_pipes_in_prose_are_not_detected_as_a_table():
    src = "Available options: foo | bar | baz."
    out = md_to_telegram_html(src)
    assert out == "Available options: foo | bar | baz."


def test_table_immediately_after_paragraph_still_detected():
    src = "Here are the schemas:\n| name | description |\n|------|-------------|\n| foo | bar |"
    out = md_to_telegram_html(src)
    assert out == "Here are the schemas:\n• <b>foo</b>: bar"


def test_strip_markdown_flattens_two_column_table_to_plain_bullets():
    src = "| name | description |\n|------|-------------|\n| foo | first |\n| bar | second |"
    out = strip_markdown(src)
    assert out == "• foo: first\n• bar: second"
    assert "|" not in out
    assert "---" not in out
