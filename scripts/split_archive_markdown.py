"""Split the Markdown of legacy storage archives into one object per page.

Archives written before ``archive_schema_version`` 3 hold one ``document.md``
that joins the Markdown of every parsed page with a blank line and keeps no
page boundary. This operator tool rewrites such an archive in place to the
current layout: the whole document becomes ``<stem>.md`` and every page gets
``document/<stem>_pNNNN.md`` with its crop links pointing one folder up, the
manifest inventory and schema version follow, and ``document.md`` goes last.

The archived OCR JSON carries no page Markdown when the parse ran with
``output="json"``, so the pages are told apart by alignment: the alphanumeric
text of every OCR block is searched in the Markdown, the occurrences are
chained in reading order (the heaviest strictly increasing chain, resemblance
breaking ties) within the page bounds the crop paths give, the paragraph that
holds each chained occurrence belongs to the block's page, and paragraphs
nobody anchored are dealt out by the count or the resemblance of the blocks
still unplaced around them. Headers, footers and page numbers never reach the
Markdown and are ignored. Whatever cannot be placed unambiguously fails the
archive, which is then left untouched and reported.

Run it from the repository root against a running storage profile::

    python scripts/split_archive_markdown.py            # dry run, lists the plan
    python scripts/split_archive_markdown.py --apply    # rewrite every archive
    python scripts/split_archive_markdown.py --apply --prefix <archive prefix>/

Credentials come from ``.env`` (``--env-file``): the reader lists and reads,
the writer puts, the browser deletes, as in the API. Nothing secret is printed.
"""

from __future__ import annotations

import argparse
import bisect
import difflib
import hashlib
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from botocore.exceptions import ClientError
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Direct script execution needs the repository root before local imports.
from ocrserver.archive_names import parse_prefix, safe_filename  # noqa: E402

MARKDOWN_TYPE = "text/markdown; charset=utf-8"
# Layout regions PaddleOCR-VL leaves out of its Markdown.
MARGINAL_TYPES = frozenset(
    {"header", "footer", "number", "header_image", "footer_image"}
)
# The storage browser previews and rewrites manifests up to this size.
MANIFEST_LIMIT = 1024 * 1024
# Leading alphanumerics of a block that must appear verbatim to anchor it.
ANCHOR_CHARS = 24
# Shorter blocks are placed by position between their neighbours, never searched.
MIN_CHARS = 6
MAX_OCCURRENCES = 64
_TAG = re.compile(r"<[^>]+>")
_BLANK = re.compile(r"\n\n+")
_CROP = re.compile(r"images/p(\d{4})-\d{4}\.png")
_ROOT_CROP = re.compile(r"(?<![\w/])images/(p\d{4}-\d{4}\.png)")


class SplitError(ValueError):
    """The pages of a document cannot be told apart in its joined Markdown."""


def _alnum(text: str) -> str:
    """Reduce text to its alphanumerics, dropping HTML tags first.

    Markdown formatting, table pipes, whitespace and punctuation differ between
    the OCR block content and PaddleOCR's Markdown; the letters and digits match.
    """
    return "".join(char for char in _TAG.sub(" ", text) if char.isalnum())


def _paragraphs(document: str) -> list[tuple[int, int]]:
    """Return the (start, end) offsets of every blank-line separated paragraph."""
    paragraphs, position = [], 0
    for match in _BLANK.finditer(document):
        if match.start() > position:
            paragraphs.append((position, match.start()))
        position = match.end()
    if position < len(document):
        paragraphs.append((position, len(document)))
    return paragraphs


def _occurrences(haystack: str, needle: str) -> list[int]:
    """Return up to MAX_OCCURRENCES start offsets of needle in haystack."""
    found, position = [], 0
    while len(found) < MAX_OCCURRENCES:
        position = haystack.find(needle, position)
        if position < 0:
            break
        found.append(position)
        position += 1
    return found


def _similar(first: str, second: str) -> float:
    """Score two alphanumeric strings: containment counts as identity."""
    if first in second or second in first:
        return 1.0
    if (
        not first
        or not second
        or max(len(first), len(second)) > 4 * min(len(first), len(second))
    ):
        return 0.0
    return difflib.SequenceMatcher(None, first, second).ratio()


def _best_chain(candidates: list[list[tuple[int, float]]]) -> list[int | None]:
    """Pick at most one candidate per block so the picks strictly increase.

    Args:
        candidates: Per block, in reading order, the (paragraph, weight) pairs it
            may take. A weight is 1 plus the block's resemblance to that
            paragraph, so the chain keeps as many blocks as possible first and,
            among chains of one length, the better matching ones: two blocks
            of different pages competing for one paragraph (a table continued
            overleaf under the same header) leave it to the closer one.

    Returns:
        The paragraph of each block in the heaviest chain, None for the rest.

    A Fenwick tree over paragraph indices holds the heaviest chain ending below
    each position; every block queries before it updates, so it never chains to
    itself.
    """
    size = 1 + max((index for options in candidates for index, _ in options), default=0)
    tree: list[tuple[float, tuple | None]] = [(0.0, None)] * (size + 1)

    def heaviest(below):
        """Return the heaviest (total, node) over paragraphs before ``below``."""
        best, position = (0.0, None), below
        while position > 0:
            if tree[position][0] > best[0]:
                best = tree[position]
            position -= position & -position
        return best

    for block, options in enumerate(candidates):
        grown = []
        for index, weight in options:
            total, previous = heaviest(index)
            grown.append((index, (total + weight, (block, index, previous))))
        for index, item in grown:
            position = index + 1
            while position <= size:
                if item[0] > tree[position][0]:
                    tree[position] = item
                position += position & -position
    chosen: list[int | None] = [None] * len(candidates)
    node = heaviest(size)[1]
    while node is not None:
        block, index, node = node
        chosen[block] = index
    return chosen


def split_markdown(document: str, pages: list[dict]) -> dict[int, str]:
    """Return the Markdown of every page with content, keyed by page number.

    Args:
        document: The joined Markdown of the archive (``document.md``).
        pages: The ``pages`` list of the archived OCR JSON, in order; a page in
            error has no Markdown and gets no entry.

    Raises:
        SplitError: Anchors fall out of page order, a paragraph matches no block
            of the pages around it, a page with text owns no paragraph, or a
            page's paragraphs are not contiguous. Nothing is guessed.
    """
    paragraphs = _paragraphs(document)
    starts = [start for start, _ in paragraphs]
    texts = [_alnum(document[start:end]) for start, end in paragraphs]

    def paragraph_at(position):
        index = bisect.bisect_right(starts, position) - 1
        return index if index >= 0 and position < paragraphs[index][1] else None

    # Map every alphanumeric of the document back to its offset.
    letters, origin = [], []
    for position, char in enumerate(document):
        if char.isalnum():
            letters.append(char)
            origin.append(position)
    haystack = "".join(letters)
    content_pages = [
        (page.get("page") or index, page)
        for index, page in enumerate(pages, 1)
        if not page.get("error")
    ]
    order = [number for number, _ in content_pages]
    blocks: list[tuple[int, str]] = []
    for number, page in content_pages:
        for block in page.get("blocks") or []:
            if block.get("type") in MARGINAL_TYPES:
                continue
            text = _alnum(block.get("content") or "")
            if block.get("type") == "image" and not text:
                continue  # placed by its crop path below
            blocks.append((number, text))
    # Crop paths carry their page number, so they fix the page of their own
    # paragraph and bound the pages of every paragraph before and after them.
    exact: dict[int, int] = {}
    for match in _CROP.finditer(document):
        index = paragraph_at(match.start())
        if index is not None:
            exact.setdefault(index, int(match.group(1)))
    marks = sorted(exact.items())
    if any(first[1] > second[1] for first, second in zip(marks, marks[1:])):
        raise SplitError("crop paths fall out of page order")
    mark_indices = [index for index, _ in marks]
    mark_pages = [number for _, number in marks]

    def allowed(index, number):
        """Return whether a block of page ``number`` may own paragraph ``index``."""
        if index in exact:
            return exact[index] == number
        rank = bisect.bisect_left(mark_indices, index)
        low = mark_pages[rank - 1] if rank else None
        high = mark_pages[rank] if rank < len(marks) else None
        return (low is None or number >= low) and (high is None or number <= high)

    # Candidate paragraphs per block: those holding the block's leading letters
    # where the crops allow that page, preferring the ones whose whole text
    # resembles the block (quotations of the same sentence elsewhere in the
    # document are thereby set aside).
    candidates = []
    for number, text in blocks:
        if len(text) < MIN_CHARS:
            candidates.append([])
            continue
        found = {
            paragraph_at(origin[offset])
            for offset in _occurrences(haystack, text[:ANCHOR_CHARS])
        }
        weighed = sorted(
            (index, _similar(text, texts[index]))
            for index in found
            if index is not None and allowed(index, number)
        )
        close = [(index, weight) for index, weight in weighed if weight >= 0.8]
        candidates.append([(index, 1 + weight) for index, weight in (close or weighed)])
    chosen = _best_chain(candidates)
    owner: dict[int, int] = dict(exact)
    body: dict[int, list[tuple[str, bool]]] = {number: [] for number in order}
    for (number, text), index in zip(blocks, chosen):
        if index is not None:
            owner.setdefault(index, number)
        body[number].append((text, index is not None))
    anchored = sorted(owner)
    if any(owner[a] > owner[b] for a, b in zip(anchored, anchored[1:])):
        raise SplitError("anchored paragraphs fall out of page order")
    if set(owner.values()) - set(order):
        raise SplitError("a crop path names a page without content")

    def edge(number, trailing):
        """Unanchored block texts after the last (or before the first) anchor."""
        items = body[number]
        flags = [anchored_flag for _, anchored_flag in items]
        if True not in flags:
            return [text for text, _ in items]
        if trailing:
            last = len(flags) - 1 - flags[::-1].index(True)
            return [text for text, _ in items[last + 1 :]]
        return [text for text, _ in items[: flags.index(True)]]

    placed = dict(owner)
    groups: dict[tuple, list[int]] = {}
    for index in range(len(paragraphs)):
        if index in owner:
            continue
        rank = bisect.bisect_left(anchored, index)
        before = owner[anchored[rank - 1]] if rank else None
        after = owner[anchored[rank]] if rank < len(anchored) else None
        groups.setdefault((before, after), []).append(index)
    for (before, after), indices in groups.items():
        if before is not None and before == after:
            for index in indices:
                placed[index] = before
            continue
        between = [
            number
            for number in order
            if (before is None or number > before) and (after is None or number < after)
        ]
        expected = [(before, t) for t in (edge(before, True) if before else [])]
        expected += [(n, t) for n in between for t, _ in body[n]]
        expected += [(after, t) for t in (edge(after, False) if after else [])]
        if len(indices) == len(expected):
            # One Markdown paragraph per unplaced block, in reading order.
            for index, (number, _) in zip(indices, expected):
                placed[index] = number
            continue
        # Otherwise the paragraphs are dealt out to the surrounding pages in page
        # order so that each resembles a block of its page as much as possible
        # (dynamic programming over a monotone assignment). Text repeated on two
        # pages, such as a product list continued overleaf, is thereby placed by
        # its neighbours rather than by a count that cannot add up. A tie, two
        # pages holding the very same line, follows the share of blocks each
        # page still has to place: a small bonus no real difference can lose to.
        around = [*([before] if before else []), *between, *([after] if after else [])]
        shares = [sum(1 for number, _ in expected if number == page) for page in around]
        scores = []
        for position, index in enumerate(indices):
            row = [
                max(
                    (_similar(texts[index], text) for text, _ in body[number] if text),
                    default=0.0,
                )
                for number in around
            ]
            if sum(shares):
                quota = (position + 0.5) / len(indices) * sum(shares)
                column, reached = 0, shares[0]
                while reached < quota and column + 1 < len(around):
                    column += 1
                    reached += shares[column]
                row[column] += 0.01
            scores.append(row)
        totals, back = list(scores[0]), []
        for row in scores[1:]:
            current, pointers, running, argument = [], [], -1.0, 0
            for column, score in enumerate(row):
                if totals[column] > running:
                    running, argument = totals[column], column
                current.append(score + running)
                pointers.append(argument)
            back.append(pointers)
            totals = current
        column = max(range(len(totals)), key=totals.__getitem__)
        assignment = [column]
        for pointers in reversed(back):
            column = pointers[column]
            assignment.append(column)
        assignment.reverse()
        # A paragraph must resemble a block of its page, or at least resemble
        # one twice as much as anything on the other pages: a table whose
        # merged cells the Markdown repeats scores low against its HTML block
        # yet far above the blocks of the pages around.
        for index, row, column in zip(indices, scores, assignment):
            others = max(
                (score for other, score in enumerate(row) if other != column),
                default=0.0,
            )
            if row[column] < 0.6 and not (
                row[column] >= 0.3 and others * 2 <= row[column]
            ):
                raise SplitError(
                    f"a paragraph between pages {before} and {after} matches no block"
                )
            placed[index] = around[column]

    by_page: dict[int, list[int]] = {}
    for index in sorted(placed):
        by_page.setdefault(placed[index], []).append(index)
    split = {}
    for number in order:
        indices = by_page.get(number)
        if not indices:
            if any(len(text) >= MIN_CHARS for text, _ in body[number]):
                raise SplitError(f"page {number} has text but owns no paragraph")
            continue  # a page whose Markdown was empty has no file, as when parsed
        if indices != list(range(indices[0], indices[-1] + 1)):
            raise SplitError(f"page {number} is not contiguous in the Markdown")
        split[number] = document[paragraphs[indices[0]][0] : paragraphs[indices[-1]][1]]
    return split


def page_objects(
    stem: str, document: bytes, split: dict[int, str]
) -> list[tuple[str, bytes, str]]:
    """Return (name, body, content type) for the page files and the whole file.

    Page files sit under ``document/`` and link their crops as ``../images/``;
    the whole file keeps the original bytes and its root-relative links.
    """
    objects = [
        (
            f"document/{stem}_p{number:04d}.md",
            _ROOT_CROP.sub(r"../images/\1", markdown).encode("utf-8"),
            MARKDOWN_TYPE,
        )
        for number, markdown in sorted(split.items())
    ]
    objects.append((f"{stem}.md", document, MARKDOWN_TYPE))
    return objects


def _read(client, bucket, key):
    """Return an object's bytes and ETag, or (None, None) when it is absent."""
    try:
        response = client.get_object(Bucket=bucket, Key=key)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
            return None, None
        raise
    body = response["Body"]
    try:
        return body.read(), response.get("ETag")
    finally:
        body.close()


def _stem(prefix: str, manifest: dict) -> str:
    """Return the Markdown stem: the archive folder's filename without extension.

    Legacy ``kind/date/job/`` addresses carry no filename, so the stem is derived
    from the manifest's source name exactly as a new archive would derive it.
    """
    source = manifest.get("source") or {}
    filename = (parse_prefix(prefix) or {}).get("filename") or safe_filename(
        source.get("original_name"), source.get("extension", "bin")
    )
    return PurePosixPath(filename).stem


def _encode(value) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")


def migrate_archive(reader, writer, browser, bucket, prefix, *, apply=False):
    """Split one archive's ``document.md`` in place; return a status record.

    Args:
        reader: S3 client that lists and reads the bucket.
        writer: S3 client that writes objects and the manifest.
        browser: S3 client that deletes objects.
        bucket: Archive bucket.
        prefix: Archive address with its trailing slash.
        apply: Write and delete; a dry run only reports the plan.

    Returns:
        ``{"prefix", "status", "detail", "pages"}`` with status ``planned``,
        ``split``, ``skipped`` (nothing to do), ``failed`` (archive untouched)
        or ``partial`` (new layout written, ``document.md`` still present).

    New objects are written first, the manifest commits them under an ETag
    guard, and ``document.md`` is deleted last, so an interruption leaves the
    archive readable and a rerun finishes the job.
    """
    record = {"prefix": prefix, "status": "failed", "detail": "", "pages": 0}
    raw, etag = _read(reader, bucket, prefix + "manifest.json")
    if raw is None:
        return {**record, "detail": "manifest missing"}
    manifest = json.loads(raw)
    artifacts = manifest.get("artifacts") or []
    legacy = next(
        (item for item in artifacts if item.get("name") == "document.md"), None
    )
    if manifest.get("complete") is not True:
        return {**record, "status": "skipped", "detail": "incomplete manifest"}
    if legacy is None:
        # Nothing to split; remove a document.md left behind by an interrupted run.
        stray, _ = _read(reader, bucket, prefix + "document.md")
        if stray is not None and apply:
            browser.delete_objects(
                Bucket=bucket,
                Delete={"Objects": [{"Key": prefix + "document.md"}], "Quiet": True},
            )
        detail = "already split" + (", stray document.md removed" if stray else "")
        return {**record, "status": "skipped", "detail": detail}
    document, _ = _read(reader, bucket, prefix + "document.md")
    if document is None:
        return {**record, "detail": "document.md listed but missing"}
    kind = manifest.get("kind")
    split: dict[int, str] = {}
    if kind != "ocr":
        source = "json_ocr.json" if kind == "parse" else "parsed.json"
        payload, _ = _read(reader, bucket, prefix + source)
        if payload is None:
            return {**record, "detail": f"{source} missing, pages unknown"}
        try:
            pages = json.loads(payload).get("pages") or []
            split = split_markdown(document.decode("utf-8"), pages)
        except (SplitError, ValueError, UnicodeError) as error:
            return {**record, "detail": str(error) or type(error).__name__}
    objects = page_objects(_stem(prefix, manifest), document, split)
    entries = [
        {
            "name": name,
            "key": prefix + name,
            "size": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
            "content_type": content_type,
        }
        for name, body, content_type in objects
    ]
    position = artifacts.index(legacy)
    updated = _encode(
        {
            **manifest,
            "archive_schema_version": 3,
            "migration": {
                "from_schema": manifest.get("archive_schema_version", 1),
                "at": datetime.now(UTC).isoformat(),
                "page_markdown": "aligned" if split else "none",
            },
            "artifacts": artifacts[:position] + entries + artifacts[position + 1 :],
        }
    )
    if len(updated) > MANIFEST_LIMIT:
        return {**record, "detail": "updated manifest exceeds the browser limit"}
    record["pages"] = len(split)
    if not apply:
        return {**record, "status": "planned", "detail": f"{len(objects)} objects"}
    for name, body, content_type in objects:
        writer.put_object(
            Bucket=bucket, Key=prefix + name, Body=body, ContentType=content_type
        )
    try:
        writer.put_object(
            Bucket=bucket,
            Key=prefix + "manifest.json",
            Body=updated,
            ContentType="application/json",
            **({"IfMatch": etag} if etag else {}),
        )
    except ClientError as error:
        return {**record, "detail": "manifest changed meanwhile: " + str(error)}
    result = browser.delete_objects(
        Bucket=bucket,
        Delete={"Objects": [{"Key": prefix + "document.md"}], "Quiet": False},
    )
    if result.get("Errors"):
        return {**record, "status": "partial", "detail": "document.md not deleted"}
    return {**record, "status": "split", "detail": f"{len(objects)} objects"}


def iter_archives(reader, bucket):
    """Yield the address of every archive that has a manifest, in key order."""
    after = ""
    while True:
        result = reader.list_objects_v2(
            Bucket=bucket,
            Prefix="",
            MaxKeys=1000,
            **({"StartAfter": after} if after else {}),
        )
        contents = result.get("Contents", [])
        for item in contents:
            after = item["Key"]
            if after.endswith("/manifest.json"):
                prefix = after[: -len("manifest.json")]
                if parse_prefix(prefix) is not None:
                    yield prefix
        if not result.get("IsTruncated") or not contents:
            return


def main(argv=None) -> int:
    """Migrate every archive (or the given prefixes); exit 1 when one failed."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--env-file", default=".env", help="OCR_S3_* role file")
    parser.add_argument("--endpoint", help="S3 endpoint; default loopback OCR_S3_PORT")
    parser.add_argument("--apply", action="store_true", help="write; default dry run")
    parser.add_argument(
        "--prefix", action="append", default=[], help="only this archive (repeatable)"
    )
    args = parser.parse_args(argv)
    from storage.bootstrap import client  # boto3 construction with bounded timeouts

    env = {**dotenv_values(args.env_file), **os.environ}
    endpoint = args.endpoint or f"http://127.0.0.1:{env.get('OCR_S3_PORT', '8333')}"
    bucket = env.get("OCR_S3_BUCKET", "ocr-outputs")

    def role(name):
        return client(
            "s3", endpoint, env[f"{name}_ACCESS_KEY"], env[f"{name}_SECRET_KEY"]
        )

    reader, writer = role("OCR_S3_READER"), role("OCR_S3")
    browser = role("OCR_S3_BROWSER")
    counts: dict[str, int] = {}
    for prefix in args.prefix or iter_archives(reader, bucket):
        record = migrate_archive(
            reader, writer, browser, bucket, prefix, apply=args.apply
        )
        counts[record["status"]] = counts.get(record["status"], 0) + 1
        print(
            f"{record['status']:8}{prefix}  pages={record['pages']}", record["detail"]
        )
    print("summary:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 1 if counts.get("failed") or counts.get("partial") else 0


if __name__ == "__main__":
    sys.exit(main())
