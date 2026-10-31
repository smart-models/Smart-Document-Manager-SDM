"""Verify Markdown table rendering, merged cells and malformed HTML."""

import pytest

from ocrserver.table_md import tables_to_markdown


def test_implicit_cell_and_row_closure_preserve_text():
    source = "<table><tr><th>A<th>B<tr><td>a<td>b</td></table>"
    converted, expanded = tables_to_markdown(source)
    assert expanded == 0
    assert converted == "| A | B |\n| --- | --- |\n| a | b |"


def test_breaks_blocks_entities_and_pipe_backslash():
    source = (
        "<table><tr><th>A</th></tr><tr><td>a<br>b<p>c</p>"
        "<div>d &amp; e</div>\\|</td></tr></table>"
    )
    converted, expanded = tables_to_markdown(source)
    assert expanded == 0
    assert "a b c d & e \\\\|" in converted


@pytest.mark.parametrize(
    ("source", "expected", "expanded"),
    [
        (
            "<table><tr><td>no header</td></tr></table>",
            "| no header |\n| --- |",
            0,
        ),
        (
            "<table><tr><th>A</th></tr><tr><td colspan='2'>a</td></tr></table>",
            "| A |  |\n| --- | --- |\n| a | a |",
            1,
        ),
        (
            "<table><tr><th>A</th></tr>"
            "<tr><td>x<table><tr><td>inner</td></tr></table>y</td></tr></table>",
            "| A |\n| --- |\n| x inner y |",
            0,
        ),
        (
            "<table><caption>Important</caption><tr><th>A</th></tr></table>",
            "Important\n\n| A |\n| --- |",
            0,
        ),
        (
            "<table><tr><th>A</th></tr><tr><td><img alt='chart'></td></tr></table>",
            "| A |\n| --- |\n| chart |",
            0,
        ),
        (
            "<table><tr><th>A</th><th>B</th></tr><tr><td>a</td></tr></table>",
            "| A | B |\n| --- | --- |\n| a |  |",
            0,
        ),
        (
            "<table><tr><th>A</th><th>B</th></tr><tr><td>a</td>",
            "| A | B |\n| --- | --- |\n| a |  |",
            0,
        ),
        (
            "<table><thead><tr><td>A</td><td>B</td></tr>"
            "<tr><td>a</td><td>b</td></tr></thead>"
            "<tr><td>1</td><td>2</td></tr></table>",
            "| A a | B b |\n| --- | --- |\n| 1 | 2 |",
            0,
        ),
    ],
)
def test_every_table_becomes_pipes(source, expected, expanded):
    assert tables_to_markdown(source) == (expected, expanded)


def test_user_transformer_table_2():
    source = (
        '<table border=1><tr><td rowspan="2">Model</td>'
        '<td colspan="2">BLEU</td><td colspan="2">Training Cost (FLOPs)</td>'
        "</tr><tr><td>EN-DE</td><td>EN-FR</td><td>EN-DE</td><td>EN-FR</td></tr>"
        "<tr><td>ByteNet [18]</td><td>23.75</td><td></td><td></td><td></td></tr>"
        "<tr><td>GNMT + RL [38]</td><td>24.6</td><td>39.92</td>"
        "<td>$ 2.3 \\cdot 10^{19} $</td><td>$ 1.4 \\cdot 10^{20} $</td></tr>"
        "</table>"
    )
    expected = (
        "| Model | BLEU EN-DE | BLEU EN-FR | Training Cost (FLOPs) EN-DE | "
        "Training Cost (FLOPs) EN-FR |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| ByteNet [18] | 23.75 |  |  |  |\n"
        "| GNMT + RL [38] | 24.6 | 39.92 | $ 2.3 \\cdot 10^{19} $ | "
        "$ 1.4 \\cdot 10^{20} $ |"
    )
    converted, expanded = tables_to_markdown(source)
    assert (converted, expanded) == (expected, 1)
    assert "| $ 2.3 \\cdot 10^{19} $ |" in converted


def test_literal_html_in_cells_remains_text():
    converted, expanded = tables_to_markdown(
        "<table><tr><th>H</th></tr><tr><td>&lt;script&gt;literal&lt;/script&gt;</td></tr></table>"
    )
    assert expanded == 0
    assert "&lt;script&gt;literal&lt;/script&gt;" in converted


@pytest.mark.parametrize("markup", ["<![custom]>", "<![>"])
def test_malformed_markup_outside_tables_preserves_all_ocr_text(markup):
    source = f"OCR text {markup} without any table"
    assert tables_to_markdown(source) == (source, 0)


def test_malformed_markup_fallback_returns_source_unchanged(monkeypatch):
    from ocrserver import table_md

    def malformed_marked_section(*args):
        raise AssertionError("unknown status keyword 'custom' in marked section")

    monkeypatch.setattr(table_md._TableRanges, "feed", malformed_marked_section)
    source = "<![custom]><table><tr><th>H</th></tr><tr><td>x</td></tr></table>"
    assert tables_to_markdown(source) == (source, 0)


def test_unrelated_parser_assertions_are_not_hidden(monkeypatch):
    from ocrserver import table_md

    def broken_parser(*args):
        raise AssertionError("unrelated implementation bug")

    monkeypatch.setattr(table_md._TableRanges, "feed", broken_parser)
    with pytest.raises(AssertionError, match="unrelated implementation bug"):
        tables_to_markdown("text")


@pytest.mark.asyncio
async def test_api_converts_tables_and_reports_expanded_spans():
    import httpx
    from test_mcp_schema import FakePipeline, FakeRecognizer
    from test_mcp_tools import PNG

    from ocr_server import create_app
    from ocrserver.config import Settings

    source = (
        "<table><tr><td>A</td><td>B</td></tr><tr><td colspan='2'>a</td></tr></table>"
    )

    class Pipeline(FakePipeline):
        async def parse_document(self, *args, **kwargs):
            result = await super().parse_document(*args, **kwargs)
            result[0]["markdown"] = source
            return result

    app = create_app(Settings(), Pipeline(), FakeRecognizer())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/parse", json={"image_base64": PNG, "table_format": "markdown"}
        )
    assert response.status_code == 200
    payload = response.json()
    markdown = payload["pages"][0]["markdown"]
    assert markdown.startswith("| ")
    assert "<table" not in markdown
    warning = next(
        w for w in payload["warnings"] if w["code"] == "tables_spans_expanded"
    )
    assert warning["severity"] == "info"
    assert "1 table(s)" in warning["message"]


@pytest.mark.asyncio
async def test_api_plain_table_produces_no_span_warning():
    import httpx
    from test_mcp_schema import FakePipeline, FakeRecognizer
    from test_mcp_tools import PNG

    from ocr_server import create_app
    from ocrserver.config import Settings

    source = "<table><tr><td>A</td><td>B</td></tr><tr><td>1</td><td>2</td></tr></table>"

    class Pipeline(FakePipeline):
        async def parse_document(self, *args, **kwargs):
            result = await super().parse_document(*args, **kwargs)
            result[0]["markdown"] = source
            return result

    app = create_app(Settings(), Pipeline(), FakeRecognizer())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/parse", json={"image_base64": PNG, "table_format": "markdown"}
        )
    assert response.status_code == 200
    payload = response.json()
    markdown = payload["pages"][0]["markdown"]
    assert markdown == "| A | B |\n| --- | --- |\n| 1 | 2 |"
    assert "tables_spans_expanded" not in [w["code"] for w in payload["warnings"]]
