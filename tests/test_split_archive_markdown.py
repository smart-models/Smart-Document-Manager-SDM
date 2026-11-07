"""Legacy archive Markdown is split into one object per page, in place."""

import hashlib
import json

import pytest
from test_storage_browser import MemoryS3

from scripts.split_archive_markdown import (
    SplitError,
    iter_archives,
    migrate_archive,
    page_objects,
    split_markdown,
)

PAGE_ONE = (
    '<img src="images/p0001-0001.png" alt="Image" width="12%" />\n\n\n'
    "# DELLA REPUBBLICA ITALIANA\n\n"
    "La Gazzetta Ufficiale pubblica cinque Serie speciali, ciascuna "
    "contraddistinta da autonoma numerazione."
)
PAGE_TWO = (
    "| Materia prima | Quantità |\n| --- | --- |\n| Granturco | fino al 65% |\n"
    "| Orzo | fino al 55% |\n\n"
    "Il presente decreto entra in vigore il giorno successivo alla pubblicazione."
)
PAGE_FOUR = (
    "La Gazzetta Ufficiale pubblica cinque Serie speciali, ciascuna "
    "contraddistinta da autonoma numerazione.\n\n"
    "€ 1,00\n\n"
    '<img src="images/p0004-0001.png" alt="Image" width="38%" />'
)
# PaddleOCR ends a page's Markdown with a newline; the joiner adds a blank line.
# Blank lines around a page are separators, never part of its file.
DOCUMENT = "\n\n".join([PAGE_ONE + "\n", PAGE_TWO, PAGE_FOUR])


def block(kind, content):
    return {"type": kind, "content": content}


def pages():
    """Four OCR pages: text, a table, a failed page and a duplicate-text page."""
    return [
        {
            "page": 1,
            "error": None,
            "blocks": [
                block("header", "GAZZETTA UFFICIALE DELLA REPUBBLICA ITALIANA"),
                block("image", ""),
                block("doc_title", "DELLA REPUBBLICA ITALIANA"),
                block(
                    "text",
                    "La Gazzetta Ufficiale pubblica cinque Serie speciali, "
                    "ciascuna contraddistinta da autonoma numerazione.",
                ),
                block("number", "I"),
            ],
        },
        {
            "page": 2,
            "error": None,
            "blocks": [
                block("header", "GAZZETTA UFFICIALE DELLA REPUBBLICA ITALIANA"),
                block(
                    "table",
                    "<table><tr><td>Materia prima</td><td>Quantità</td></tr>"
                    "<tr><td>Granturco</td><td>fino al 65%</td></tr>"
                    "<tr><td>Orzo</td><td>fino al 55%</td></tr></table>",
                ),
                block(
                    "text",
                    "Il presente decreto entra in vigore il giorno successivo "
                    "alla pubblicazione.",
                ),
            ],
        },
        {"page": 3, "error": "fixture page failed", "blocks": None},
        {
            "page": 4,
            "error": None,
            "blocks": [
                block("header", "GAZZETTA UFFICIALE DELLA REPUBBLICA ITALIANA"),
                block(
                    "text",
                    "La Gazzetta Ufficiale pubblica cinque Serie speciali, "
                    "ciascuna contraddistinta da autonoma numerazione.",
                ),
                block("text", "€ 1,00"),
                block("image", ""),
            ],
        },
    ]


def test_split_markdown_places_every_paragraph_on_its_page():
    """Blocks anchor paragraphs; tables, short text and duplicates follow suit.

    Marginal blocks never appear in the Markdown, the failed page has no text,
    the table is HTML in the JSON but a pipe table in the Markdown, the short
    price is placed by position and the repeated sentence lands on both pages.
    """
    split = split_markdown(DOCUMENT, pages())

    assert split == {1: PAGE_ONE, 2: PAGE_TWO, 4: PAGE_FOUR}


def test_split_markdown_refuses_text_it_cannot_place():
    """A page whose text is missing or out of order fails instead of guessing."""
    missing = pages()
    missing[1]["blocks"].append(block("text", "Un comma che il Markdown non ha."))
    swapped = "\n\n".join([PAGE_TWO, PAGE_ONE, PAGE_FOUR])

    with pytest.raises(SplitError):
        split_markdown("\n\n".join([PAGE_ONE, PAGE_FOUR]), missing)
    with pytest.raises(SplitError):
        split_markdown(swapped, pages())


def test_split_markdown_keeps_repeated_text_behind_the_crops_of_its_page():
    """A sentence repeated overleaf cannot be pulled before the earlier page's crop.

    Page 2 repeats a line of page 1 and its Markdown lists the blocks in another
    order than the OCR JSON, so the longest chain alone could anchor the repeat
    to the first occurrence; the crop of page 1 that sits between forbids it.
    """
    repeated = "Art. 1 - Oggetto del decreto"
    first = f"Visto il decreto.\n\n{repeated}\n\n![fig](images/p0001-0001.png)"
    second = (
        f"Disposizioni finali.\n\n{repeated}\n\nIl presente decreto entra in vigore."
    )
    split = split_markdown(
        first + "\n\n" + second,
        [
            {
                "page": 1,
                "blocks": [block("text", "Visto il decreto."), block("text", repeated)],
            },
            {
                "page": 2,
                "blocks": [
                    block("text", repeated),
                    block("text", "Disposizioni finali."),
                    block("text", "Il presente decreto entra in vigore."),
                ],
            },
        ],
    )

    assert split == {1: first, 2: second}


def test_split_markdown_deals_identical_lines_out_in_page_order():
    """Lines no block anchors are dealt to the pages around them in order.

    Both pages list the same products, the Markdown spells a digit as a letter
    so no prefix anchors, and page 2 has a dash block the Markdown dropped, so
    the counts do not add up: the lines are dealt out by similarity, ties
    following the share of blocks each page still has to place.
    """
    lines = "Prodotto Alfa 1O compresse.\n\nProdotto Alfa 2O compresse."
    first = "# Elenco\n\n" + lines
    second = lines + "\n\nFine dell'elenco dei prodotti."
    products = [
        block("text", "Prodotto Alfa 10 compresse."),
        block("text", "Prodotto Alfa 20 compresse."),
    ]
    split = split_markdown(
        first + "\n\n" + second,
        [
            {"page": 1, "blocks": [block("doc_title", "Elenco"), *products]},
            {
                "page": 2,
                "blocks": [
                    *products,
                    block("text", "—"),
                    block("text", "Fine dell'elenco dei prodotti."),
                ],
            },
        ],
    )

    assert split == {1: first, 2: second}


def test_split_markdown_leaves_a_shared_table_header_to_the_closer_block():
    """Two pages' tables share a header; the paragraph goes to the table it is.

    Only page 1's pipe table starts like both HTML blocks, so both compete for
    that paragraph; the chain keeps the block whose whole text matches, and
    page 2's table is then placed between its neighbours.
    """
    first = "| N | METODO | VINCOLI |\n| --- | --- | --- |\n| 3 | A | DIRETTO 10 |"
    second = (
        "| Nr. | METODO | VINCOLI |\n| --- | --- | --- |\n| 3 | B | INDIRETTO 30 |"
        "\n\nFine della tabella riportata."
    )
    split = split_markdown(
        first + "\n\n" + second,
        [
            {
                "page": 1,
                "blocks": [
                    block(
                        "table",
                        "<table><tr><td>N</td><td>METODO</td><td>VINCOLI</td></tr>"
                        "<tr><td>3</td><td>A</td><td>DIRETTO 10</td></tr></table>",
                    )
                ],
            },
            {
                "page": 2,
                "blocks": [
                    block(
                        "table",
                        "<table><tr><td>N</td><td>METODO</td><td>VINCOLI</td></tr>"
                        "<tr><td>3</td><td>B</td><td>INDIRETTO 30</td></tr></table>",
                    ),
                    block("text", "Fine della tabella riportata."),
                ],
            },
        ],
    )

    assert split == {1: first, 2: second}


def test_split_markdown_accepts_a_table_that_clearly_beats_the_other_pages():
    """An expanded table resembles its block only in part, but nothing else."""
    first = (
        "Premessa generale del documento.\n\n"
        "| Totale | entrate | complessive | 1234 | 5678 | 9012 | 3456 |"
    )
    second = "Disposizioni transitorie e finali."
    split = split_markdown(
        first + "\n\n" + second,
        [
            {
                "page": 1,
                "blocks": [
                    block("text", "Premessa generale del documento."),
                    block(
                        "table",
                        "<table><tr><td>Totale complessivo entrate</td></tr></table>",
                    ),
                ],
            },
            {
                "page": 2,
                "blocks": [block("text", "—"), block("text", second)],
            },
        ],
    )

    assert split == {1: first, 2: second}


def test_page_objects_name_files_after_the_stem_and_link_crops_one_folder_up():
    document = DOCUMENT.encode("utf-8")

    objects = page_objects("report", document, {1: PAGE_ONE, 4: PAGE_FOUR})

    names = [name for name, _, _ in objects]
    assert names == [
        "document/report_p0001.md",
        "document/report_p0004.md",
        "report.md",
    ]
    assert objects[0][1] == PAGE_ONE.replace("images/", "../images/").encode("utf-8")
    assert objects[2][1] == document
    assert {content_type for _, _, content_type in objects} == {
        "text/markdown; charset=utf-8"
    }


REPORT = "report.pdf__20260926T120000000000Z__Abcd123_/"


def legacy_archive(s3, kind="parse", prefix=REPORT):
    """Write a schema-2 archive whose Markdown is one joined document.md."""
    s3.objects[prefix + "document.md"] = DOCUMENT.encode("utf-8")
    s3.objects[prefix + "images/p0001-0001.png"] = b"\x89PNG"
    artifacts = [
        {"name": "document.md", "size": len(DOCUMENT), "content_type": "text/plain"},
        {"name": "images/p0001-0001.png", "size": 4, "content_type": "image/png"},
    ]
    if kind == "parse":
        s3.objects[prefix + "json_ocr.json"] = json.dumps({"pages": pages()}).encode()
        artifacts.append({"name": "json_ocr.json", "size": 1, "content_type": "a/json"})
    manifest = {
        "archive_schema_version": 2,
        "complete": True,
        "kind": kind,
        "job_id": "Abcd123_",
        "created_at": "2026-09-26T12:00:00+00:00",
        "source": {"original_name": "report.pdf", "extension": "pdf"},
        "artifacts": [{**item, "key": prefix + item["name"]} for item in artifacts],
    }
    s3.objects[prefix + "manifest.json"] = json.dumps(manifest).encode()
    return prefix


def test_migrate_archive_rewrites_manifest_last_and_removes_document_md():
    s3 = MemoryS3()
    prefix = legacy_archive(s3)

    planned = migrate_archive(s3, s3, s3, "bucket", prefix)
    assert planned["status"] == "planned" and planned["pages"] == 3
    assert prefix + "document.md" in s3.objects and not any(
        call[0] in {"put", "delete"} for call in s3.calls
    )

    record = migrate_archive(s3, s3, s3, "bucket", prefix, apply=True)

    assert record["status"] == "split" and record["pages"] == 3
    assert prefix + "document.md" not in s3.objects
    assert s3.objects[prefix + "report.md"] == DOCUMENT.encode("utf-8")
    assert s3.objects[prefix + "document/report_p0002.md"] == PAGE_TWO.encode("utf-8")
    assert s3.objects[prefix + "document/report_p0004.md"] == PAGE_FOUR.replace(
        "images/", "../images/"
    ).encode("utf-8")
    manifest = json.loads(s3.objects[prefix + "manifest.json"])
    assert manifest["archive_schema_version"] == 3
    assert manifest["migration"]["from_schema"] == 2
    names = [item["name"] for item in manifest["artifacts"]]
    assert names == [
        "document/report_p0001.md",
        "document/report_p0002.md",
        "document/report_p0004.md",
        "report.md",
        "images/p0001-0001.png",
        "json_ocr.json",
    ]
    entry = manifest["artifacts"][3]
    assert entry["key"] == prefix + "report.md"
    assert entry["size"] == len(DOCUMENT.encode("utf-8"))
    assert entry["sha256"] == hashlib.sha256(DOCUMENT.encode("utf-8")).hexdigest()
    puts = [call[1]["Key"] for call in s3.calls if call[0] == "put"]
    assert puts[-1] == prefix + "manifest.json", "the manifest commits the new layout"
    assert s3.calls[-1][0] == "delete"

    again = migrate_archive(s3, s3, s3, "bucket", prefix, apply=True)
    assert again["status"] == "skipped"


def test_migrate_archive_renames_only_for_ocr_and_fails_without_page_source():
    s3 = MemoryS3()
    scan = "scan.png__20260926T120000000000Z__Zyxw987_/"
    ocr = legacy_archive(s3, kind="ocr", prefix=scan)
    broken = legacy_archive(s3, prefix="rep.pdf__20260926T130000000000Z__Qwer456_/")
    del s3.objects[broken + "json_ocr.json"]

    record = migrate_archive(s3, s3, s3, "bucket", ocr, apply=True)
    failed = migrate_archive(s3, s3, s3, "bucket", broken, apply=True)

    assert record["status"] == "split" and record["pages"] == 0
    assert s3.objects[ocr + "scan.md"] == DOCUMENT.encode("utf-8")
    assert not any(key.startswith(ocr + "document/") for key in s3.objects)
    assert failed["status"] == "failed" and "json_ocr.json" in failed["detail"]
    assert broken + "document.md" in s3.objects


def test_iter_archives_yields_every_manifest_prefix_once():
    s3 = MemoryS3()
    first = legacy_archive(s3)
    second = s3.archive(7)
    s3.objects["_archive_index/v1/entries/x"] = b""

    assert sorted(iter_archives(s3, "bucket")) == sorted([first, second])
