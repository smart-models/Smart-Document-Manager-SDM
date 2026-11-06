"""Versioned temporal index; entries contain only verified archive addresses."""

from datetime import UTC, datetime, timedelta

from ocrserver.archive_names import STAMP_FORMAT, parse_prefix, prefix_from_key

INDEX_ROOT = "_archive_index/v1/"
INDEX_ENTRIES = INDEX_ROOT + "entries/"
INDEX_READY = INDEX_ROOT + "ready.json"
# Below this age an archive without a complete manifest may still be being
# written, so it is neither listed as abandoned, deleted nor unindexed.
INCOMPLETE_AFTER = timedelta(minutes=10)
_REVERSE_DIGITS = str.maketrans("0123456789", "9876543210")


def sort_stamp(prefix):
    """Return a sortable UTC stamp from a validated current or legacy address.

    Raise ValueError when the prefix is not a recognized archive address.
    """
    match = parse_prefix(prefix)
    if match is None:
        raise ValueError("Invalid archive address")
    return match["stamp"]


def stale(stamp):
    """Return whether an archive stamp is older than INCOMPLETE_AFTER."""
    written = datetime.strptime(stamp, STAMP_FORMAT).replace(tzinfo=UTC)
    return datetime.now(UTC) - written >= INCOMPLETE_AFTER


def index_key(prefix):
    """Encode an archive address for newest-first lexical S3 listing.

    Reverse timestamp digits and ASCII filename bytes so ordinary ascending
    listing yields descending archive order. Split long encoded path components
    without changing their lexical order to satisfy SeaweedFS key limits.
    """
    stamp = sort_stamp(prefix).translate(_REVERSE_DIGITS)
    reverse_name = bytes(255 - b for b in prefix.encode("ascii")).hex()
    # SeaweedFS limits each path component; fixed-width splits preserve sorting.
    encoded = "/".join(
        reverse_name[i : i + 120] for i in range(0, len(reverse_name), 120)
    )
    return INDEX_ENTRIES + stamp + "__" + encoded


def index_archive(key):
    """Decode an index entry and reject noncanonical or invalid archive keys."""
    if not key.startswith(INDEX_ENTRIES):
        raise ValueError("Invalid index key")
    stamp, name = key[len(INDEX_ENTRIES) :].split("__", 1)
    prefix = bytes(255 - b for b in bytes.fromhex(name.replace("/", ""))).decode(
        "ascii"
    )
    if index_key(prefix) != key:
        raise ValueError("Invalid index address")
    return prefix


def index_start(date_to):
    """Return a lexical listing start for an inclusive date upper bound.

    An absent bound starts at the beginning of the index. The caller validates
    date syntax before passing a nonempty value.
    """
    if not date_to:
        return ""
    stamp = date_to.replace("-", "") + "T235959999999Z"
    return INDEX_ENTRIES + stamp.translate(_REVERSE_DIGITS)


def write_entry(client, bucket, prefix):
    """Write an empty index marker using the supplied S3 client and bucket."""
    client.put_object(
        Bucket=bucket,
        Key=index_key(prefix),
        Body=b"",
        ContentType="application/octet-stream",
    )


def touch_index(writer, bucket, prefix, check, *, reader=None):
    """Index an archive and renew an existing readiness marker.

    Args:
        writer: S3 client authorized to write index entries and readiness state.
        bucket: Archive bucket shared by the index and document objects.
        prefix: Validated archive address with a trailing slash.
        check: Cancellation or deadline guard called around each S3 operation.
        reader: Optional listing client; defaults to the writer.

    A missing readiness marker stays absent until a complete backfill publishes
    it. Guard and S3 failures propagate to the caller without further writes.
    """
    check()
    write_entry(writer, bucket, prefix)
    check()
    result = (reader or writer).list_objects_v2(
        Bucket=bucket, Prefix=INDEX_READY, MaxKeys=1
    )
    check()
    if any(item["Key"] == INDEX_READY for item in result.get("Contents", [])):
        # Readiness expires safely with an idle bucket; active writes renew it.
        writer.put_object(
            Bucket=bucket, Key=INDEX_READY, Body=b"1", ContentType="text/plain"
        )
        check()


def _keys(client, bucket, prefix):
    """Yield every key under a prefix, in listing order.

    Args:
        client: S3 client allowed to list the bucket.
        bucket: Bucket to list.
        prefix: Key prefix to list; empty for the whole bucket.

    Raises:
        ValueError: A listing left the prefix or did not advance. Callers delete
            from these keys, so a malformed listing must stop them.
    """
    after = ""
    while True:
        result = client.list_objects_v2(
            Bucket=bucket,
            Prefix=prefix,
            MaxKeys=1000,
            **({"StartAfter": after} if after else {}),
        )
        records = result.get("Contents", [])
        for item in records:
            if not item["Key"].startswith(prefix) or item["Key"] <= after:
                raise ValueError("Storage listing left its prefix or did not advance")
            after = item["Key"]
            yield after
        if not result.get("IsTruncated"):
            return
        if not records:
            raise ValueError("Storage listing did not advance")


def _orphan(key, live):
    """Return whether an index entry should be pruned.

    Args:
        key: Index entry key.
        live: Addresses of the archives that have objects in the bucket.

    A noncanonical key is always an orphan, for example a stamp misread by an
    earlier release. Otherwise the entry must name no live archive and be too old
    for a writer to still be reserving that archive.
    """
    try:
        prefix = index_archive(key)
    except (ValueError, UnicodeError):
        return True
    return prefix not in live and stale(sort_stamp(prefix))


def rebuild_index(client, bucket):
    """Reconcile the index with the bucket; publish readiness after a full scan.

    New writers index before reserving an archive, so concurrent new writes do
    not fall through a scan whose lexical position has already advanced, and
    an entry younger than INCOMPLETE_AFTER survives without its objects.
    Existing entries are not rewritten, so retention expires them with their
    archive. Orphans (see _orphan) are pruned best effort: they only cost listings
    a manifest read, so a failed prune neither blocks startup nor readiness, and
    the next rebuild retries it. Every index key and archive address is held in
    memory during the scan.

    Args:
        client: S3 client allowed to list, write and delete in the bucket.
        bucket: Archive bucket shared by the index and document objects.

    Returns:
        The number of archives found in the bucket.

    Raises:
        ValueError: A listing was malformed; nothing is pruned or published.
    """
    indexed = set(_keys(client, bucket, INDEX_ENTRIES))
    live = set()
    for key in _keys(client, bucket, ""):
        prefix = prefix_from_key(key)
        if prefix and prefix not in live:
            live.add(prefix)
            if index_key(prefix) not in indexed:
                write_entry(client, bucket, prefix)
    orphans = [key for key in indexed if _orphan(key, live)]
    try:
        for offset in range(0, len(orphans), 1000):
            batch = orphans[offset : offset + 1000]
            client.delete_objects(
                Bucket=bucket,
                Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True},
            )
    except Exception:  # noqa: BLE001, S110 - SDK errors may contain secrets
        pass
    client.put_object(
        Bucket=bucket, Key=INDEX_READY, Body=b"1", ContentType="text/plain"
    )
    return len(live)
