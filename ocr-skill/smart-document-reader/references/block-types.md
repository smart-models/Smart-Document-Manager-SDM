# Block types (PP-DocLayoutV3 via PaddleOCR-VL 1.6)

`order`: `yes` = numbered in reading order, `no` = always `null` for this tag (the label
decides, not the position). Reading order for every block is still `block_id`.
`markdown`: whether the page `markdown` contains the block. Blocks marked `no` exist only in
`blocks`. `—` = never present in output.

| Tag | Role | order | content | markdown | Treatment and observed variants |
|---|---|---|---|---|---|
| `abstract` | Abstract | yes | text | yes | Typed section before the body (papers). |
| `algorithm` | Code or algorithm | yes | plain code, multi-line | yes | Present as a code block; markdown has no fences. Pseudocode can instead arrive as `display_formula` with `\text{for}`. |
| `aside_text` | Side or margin text | no | text | no | Page metadata, never body: arXiv identifier (paper), side navigation tabs (magazine), exam form fields ("Name: ___") and instructions, publisher "Downloaded by …" stamps. The chunker (`ocr_create_rag_chunks`) keeps it in body chunks: set it apart when reading chunk text. |
| `chart` | Chart | no | empty | yes | Mention presence, caption and source; never state values. Markdown shows `<img src="imgs/img_in_chart_box_…jpg">`. |
| `content` | Table of contents | yes | lines with dotted leaders and printed page numbers | yes | Page numbers are printed labels (see `number`), not OCR page indexes. |
| `display_formula` | Display formula | yes | LaTeX in `$$ … $$` | yes | Keep verbatim. Matrices and pseudocode also land here. |
| `doc_title` | Document title | yes | text | yes | Top heading. Several on one page may be several articles; a second one is often a subtitle or subject line — confirm from content. |
| `figure_title` | Figure, table or chart title (caption) | no | text | yes | Usually adjacent in `block_id` to its object, before or after. A label (Table/Figure/表/图/图表 N) confirms it when present, but most captions have none. Sub-figure labels like `(d)` and section headers tagged `figure_title` are not captions; one between two objects is ambiguous. A final caption without an adjacent object is only a possible continuation: verify matching evidence on the next page; otherwise report the association as unresolved. |
| `footer` | Page footer | no | text | no | Page metadata: copyright, venue, data sources, disclaimers, funding statements. Report repeats once. |
| `footer_image` | Footer image | no | empty | no | Logo (e.g. EU emblem). Mention with the adjacent `footer` text; never describe. |
| `footnote` | Footnote | no | text, starting with a marker or none | no | Usually linked to a marker in body text on the same page. The leading marker can be LaTeX `$ ^{x} $`, a plain number (`1. `), a circled number (`①`) or absent (newspaper contributor line); search the body for it as a superscript, in LaTeX and in plain form. |
| `formula_number` | Equation number | yes | text such as `(1)` | no | Usually belongs to the `display_formula` just before it in `block_id`; after a `text` block it numbers an equation inside that prose. |
| `header` | Page header | no | text | no | Page metadata: running title, date, publication name; a mislabeled notice is possible (copyright notices usually come as `footer`). |
| `header_image` | Header image | no | empty | no | Logo, usually in the top band; a cover logo elsewhere is possible. Never describe. |
| `image` | Image | no | empty | yes | Mention presence and caption; never describe. Markdown shows `<img src="imgs/img_in_image_box_…jpg">`. |
| `inline_formula` | Inline formula | yes | LaTeX in `$$ … $$` | yes | Mostly standalone (formula sheets, also textbook and exam pages); inside prose, inline math is in `text` as `$ … $`. Adjacent fragments can be one split formula: flag it. |
| `number` | Printed page number | no | text: "12", "C7", "九〇", "Page 1 of 1" | no | Printed page label, not the page index. |
| `paragraph_title` | Section heading | yes | text | yes | Heading; level only from explicit numbering. Also receives "References" and slide titles; bylines ("By …") are usually `text`, sometimes here. |
| `reference` | References region | — | — | — | Never in output: dropped by the pipeline before recognition. Bibliography arrives as `paragraph_title` + `reference_content`. |
| `reference_content` | Bibliography entry | yes | text, often starting "[n]" or "n." | yes | One entry per block; often numbered, otherwise author-year: keep numbering when present, never invent it. |
| `seal` | Seal or stamp | yes | empty | yes | Mention presence only; the stamp text is not read (few samples observed). Markdown shows `<img src="imgs/img_in_seal_box_…jpg">`. |
| `table` | Table | no | HTML `<table>` with `rowspan`/`colspan`, sometimes `<img>` in cells | yes | Parse spans; leave empty cells empty; `<img>` in a cell = unread image. Flag tables that continue on the next page. |
| `text` | Body text | yes | text with inline math `$ … $` | yes | Body. Empty `content` and shares its `group_id` with an earlier non-empty block = its text is in that block (`group_id` is not a `block_id`); such empty blocks still carry an `order`, they are not unplaced. |
| `vertical_text` | Vertical text | yes | text | yes | CJK vertical columns; `block_id` runs right to left on the few pages observed: flag any inversion. |
| `vision_footnote` | Note under a figure or table | no | text | yes | Usually the source, photo credit, legend or URL of the object just before it; it can follow another `vision_footnote`. When its content is a closing sentence or unrelated, follow the content. |
