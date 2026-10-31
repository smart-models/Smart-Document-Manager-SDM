# Document types

The reading rules never change with the genre. The genre changes what counts as important
metadata and which labels are likely wrong. Recognise it from content, not from one tag.

## Scientific paper
- Keep: title, authors (merged groups: text sits in the first block of a `group_id`), abstract,
  numbered sections, equation numbers, footnote markers, bibliography (`reference_content`).
- Traps: copyright notice as `header` (possible; usually `footer`); arXiv id as `aside_text`;
  "References" as `paragraph_title`; plain-numbered footnotes ("1. ") linked to superscripts.
- Flag: `markdown: null` when parsed with `output="json"` — read `blocks` in `block_id` order.

## Book and textbook
- Keep: chapter and section numbering, printed page `number`, running `header`.
- Traps: tables mixing text and pictures (`<img>` inside cells); exercise numbering read as headings.

## Magazine
- Keep: article heading, body, sidebars, footnotes with markers.
- Traps: vertical side tabs as `aside_text` (navigation, not content); report copyright in `footer`.

## Newspaper
- Keep: publication name, date and page label from `header`/`number` — often the only place they appear.
- Traps: several articles per page, often with a single `doc_title` (other articles start at a
  `paragraph_title`); bylines ("By …") usually as `text`, sometimes `paragraph_title`;
  section headers ("CAPE ANN") tagged `figure_title`; dozens of small tables (scores,
  listings); legends and credits as `vision_footnote`; contributor lines as `footnote`
  without marker.

## Financial or research report
- Keep: chart and table titles ("图表 N"), data sources (`vision_footnote` "数据来源…",
  `footer` "资料来源…"), disclaimers.
- Traps: charts have no values (`chart` is empty); a caption at page bottom whose object is on
  the next page; broker logo as `header_image`.

## Slides
- Keep: slide title (`paragraph_title`), bullets, sources in `footer`.
- Traps: little text and many `image` blocks; do not describe images.

## Handwritten notes
- Keep: text as read; flag low confidence when content looks garbled.
- Traps: few blocks, headings guessed from size; formulas mixed into text.

## Exam paper and worksheet
- Keep: question numbering, formulas, tables, answer spaces as empty.
- Traps: each exercise equation as its own `display_formula`; figures without captions.

## Formula sheet
- Keep: each formula verbatim with its heading.
- Traps: grid layout — `block_id` can alternate between columns, use `bbox` to regroup and say so;
  standalone `inline_formula` fragments that are one split formula; unit lines like `[v]=m/s`.

## Historical or vertical document
- Keep: `vertical_text` in `block_id` order (right to left); printed page labels.
- Traps: one image can hold two facing pages (two `header`, two `number`); traditional
  characters and OCR substitutions — report as read.

## Business document (invoice, contract, form)
- Keep: parties, identifiers, dates, amounts, table rows, printed page label ("Page 1 of 1").
- Traps: `seal` is empty (stamp text unread); the model can "correct" names ("Forniture" →
  "Furniture") — report names, codes and amounts as read and flag suspicious normalisation;
  a form or catalogue code ("430225") can arrive as a `number` block beside the printed
  page label — keep both as metadata and cite the page by the label.

## Institutional or project document
- Keep: funding statement and programme (often only in `footer`), report it once.
- Traps: logos as `header_image`/`footer_image`, also on the cover; identical footer on every page;
  `pages_truncated` on long deliverables.

## Table of contents
- Keep: entries (`content`) with their printed page numbers.
- Traps: printed page numbers are not OCR page indexes.

## Technical documentation with code
- Keep: function signatures and snippets from `algorithm` as code blocks.
- Traps: markdown shows code as plain paragraphs; pseudocode can be a `display_formula`.

## Generic document
- Use when no genre above fits: short office pages, letters, reports converted from Office
  (`office_converted`), mixed scans.
- Keep: title, body in `block_id` order, tables with their spans, closing lines, any printed
  page label.
- Traps: a closing sentence or note tagged `vision_footnote` or `footnote` — follow the content;
  tables without `<th>` — a header row is an assumption to state.
