"""Verify HTML, code and formula normalization in OCR Markdown."""

from ocrserver.markdown_normalizer import (
    MarkdownNormalizer,
    normalize_code_blocks,
    normalize_formulas,
    normalize_generic_html,
)


def test_normalizer_converts_centered_text_and_table():
    md = (
        '<p align="center">Title</p>\n'
        "<table><tr><th>A</th></tr><tr><td>1</td></tr></table>"
    )
    out, spans = MarkdownNormalizer.normalize(md, table_format="markdown")
    assert "align" not in out
    assert "| A |" in out
    assert spans == 0


def test_normalize_code_block_with_language():
    md = '<pre><code class="python">x = 1</code></pre>'
    assert "```python" in normalize_code_blocks(md)
    assert "<pre>" not in normalize_code_blocks(md)


def test_normalize_generic_html_preserves_img():
    md = '<b>bold</b> and <img src="fig.png"> and <br>line'
    out = normalize_generic_html(md)
    assert "**bold**" in out
    assert '<img src="fig.png">' in out
    assert "<br>" not in out


def test_normalize_formulas_does_not_touch_plain_text():
    md = "The price is $5 and not $10."
    assert normalize_formulas(md) == md


def test_normalize_formulas_wraps_math_tag():
    md = "<math>x^2</math>"
    out = normalize_formulas(md)
    assert "$x^2$" in out


def test_malformed_html_fallback():
    md = "<pre><code>unclosed"
    out, _ = MarkdownNormalizer.normalize(md, table_format="markdown")
    assert "unclosed" in out


def test_code_content_with_backtick_uses_four_backtick_fence():
    out = normalize_code_blocks("<pre><code>print(`x`)</code></pre>")
    assert out.startswith("````\n")
    assert out.endswith("\n````")


def test_normalizer_preserves_html_tables_when_requested():
    out, spans = MarkdownNormalizer.normalize("<table><tr><td>A</td></tr></table>")
    assert "<table>" in out
    assert spans == 0


def test_code_block_decodes_html_entities_once():
    md = "<pre><code>&amp;amp;</code></pre>"
    out = normalize_code_blocks(md)
    assert "&amp;" in out
    assert "&" not in out.replace("&amp;", "")


def test_generic_html_preserves_entities_in_img_tag():
    md = '<img src="fig.png?a=1&amp;b=2">'
    out = normalize_generic_html(md)
    assert '<img src="fig.png?a=1&amp;b=2">' == out


def test_code_block_decodes_lt_gt_entities():
    md = "<pre><code>&lt;script&gt;</code></pre>"
    out = normalize_code_blocks(md)
    assert "<script>" in out
    assert "&lt;" not in out


def test_code_block_preserves_nested_html_literal():
    md = "<pre><code>&lt;div&gt;nested&lt;/div&gt;</code></pre>"
    out = normalize_code_blocks(md)
    assert "<div>nested</div>" in out
    assert "&lt;div&gt;" not in out
    assert "```" in out


def test_already_delimited_formula_is_not_doubled():
    md = "<math>$x^2$</math>"
    out = normalize_formulas(md)
    assert out == "$x^2$"

    md_display = "<math>$$x^2$$</math>"
    out_display = normalize_formulas(md_display)
    assert out_display == "$$x^2$$"


def test_empty_and_whitespace_input_are_identity():
    assert MarkdownNormalizer.normalize("") == ("", 0)
    assert MarkdownNormalizer.normalize("   \n\t  ") == ("   \n\t  ", 0)


def test_plain_markdown_identity():
    md = "# Heading\n\nSome **bold** text and a `code` span."
    out, spans = MarkdownNormalizer.normalize(md)
    assert out == md
    assert spans == 0


def test_literal_angle_brackets_are_preserved():
    md = "The value x < y and z > w"
    out, _ = MarkdownNormalizer.normalize(md)
    assert "x < y" in out
    assert "z > w" in out
    assert out == md


def test_duplicate_formula_blocks_wrapped_once():
    blocks = [
        {"type": "inline_formula", "content": "x"},
        {"type": "inline_formula", "content": "x"},
    ]
    md = "x and x"
    out = normalize_formulas(md, blocks)
    assert out.count("$x$") == 2
    assert "$$" not in out


def test_formula_content_substring_of_word_not_wrapped():
    blocks = [{"type": "inline_formula", "content": "x"}]
    md = "The example shows x and y"
    out = normalize_formulas(md, blocks)
    assert "exam$x$le" not in out
    assert "$x$" in out


def test_formula_with_leading_backslash_is_wrapped():
    blocks = [{"type": "inline_formula", "content": r"\frac{a}{b}"}]
    md = r"The equation \frac{a}{b} is shown"
    out = normalize_formulas(md, blocks)
    assert out == r"The equation $\frac{a}{b}$ is shown"


def test_formula_with_trailing_backslash_word_is_wrapped():
    blocks = [{"type": "inline_formula", "content": r"\alpha + \beta"}]
    md = r"The rule \alpha + \beta applies"
    out = normalize_formulas(md, blocks)
    assert out == r"The rule $\alpha + \beta$ applies"


def test_formula_already_dollar_wrapped_is_not_doubled():
    blocks = [{"type": "inline_formula", "content": r"\frac{a}{b}"}]
    md = r"The equation $\frac{a}{b}$ is shown"
    out = normalize_formulas(md, blocks)
    assert out == md
    assert out.count(r"$\frac{a}{b}$") == 1


def test_latex_substring_inside_word_not_wrapped():
    blocks = [{"type": "inline_formula", "content": r"\alpha"}]
    md = r"The word \alphabet contains the substring"
    out = normalize_formulas(md, blocks)
    assert r"$\alpha$bet" not in out
    assert r"\alphabet" in out
