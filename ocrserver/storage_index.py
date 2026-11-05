"""Versioned temporal index; entries contain only verified archive addresses."""

from datetime import UTC, datetime, timedelta

from ocrserver.archive_names import parse_prefix, prefix_from_key

INDEX_ROOT = "_archive_index/v1/"
INDEX_ENTRIES = INDEX_ROOT + "entries/"
INDEX_READY = INDEX_ROOT + "ready.json"
# A writer may still be reserving an archive this young: keep its entry.
INCOMPLETE_AFTER = timedelta(minutes=10)
_REVERSE_DIGITS = str.maketrans("0123456789", "9876543210")


def sort_stamp(prefix):
    """Return a sortable UTC stamp from a validated current or legacy address.

    Legacy archives carry only a date, so their time is represented as midnight.
    Raise ValueError when the prefix is not a recognized archive address.
    """
    match = parse_prefix(prefix)
    if match is None:
        raise ValueError("Invalid archive address")
    if match["kind"] is None:
        # The eight-character code may itself contain "__"; slice from the end.
        return prefix[-33:-11]
    return match["date"].replace("-", "") + "T000000000000Z"


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
    """Yield every key under a prefix, rejecting a listing that does not advance."""
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
            if item["Key"] <= after:
                raise ValueError("Storage listing did not advance")
            after = item["Key"]
            yield after
        if not result.get("IsTruncated"):
            return
        if not records:
            raise ValueError("Storage listing did not advance")


def _orphan(key, live):
    """Whether an entry names no archive and no writer can still be reserving it."""
    try:
        prefix = index_archive(key)
    except (ValueError, UnicodeError):
        return True  # noncanonical, e.g. a stamp misread by an earlier release
    if prefix in live:
        return False
    written = datetime.strptime(sort_stamp(prefix), "%Y%m%dT%H%M%S%fZ")
    return datetime.now(UTC) - written.replace(tzinfo=UTC) >= INCOMPLETE_AFTER


def rebuild_index(client, bucket):
    """Reconcile the index with the bucket; publish readiness after a full scan.

    New writers index before reserving an archive, so concurrent new writes do
    not fall through a scan whose lexical position has already advanced, and
    an entry younger than INCOMPLETE_AFTER survives without its objects.
    Existing entries are not rewritten, so retention expires them with their
    archive; entries of deleted or expired archives are removed.
    Return the number of archives found.
    """
    indexed = set(_keys(client, bucket, INDEX_ENTRIES))
    live = set()
    for key in _keys(client, bucket, ""):
        prefix = prefix_from_key(key)
        if prefix and prefix not in live:
            live.add(prefix)
            if index_key(prefix) not in indexed:
                write_entry(client, bucket, prefix)
    orphans = [key for key in sorted(indexed) if _orphan(key, live)]
    for offset in range(0, len(orphans), 1000):
        client.delete_objects(
            Bucket=bucket,
            Delete={
                "Objects": [{"Key": key} for key in orphans[offset : offset + 1000]],
                "Quiet": True,
            },
        )
    client.put_object(
        Bucket=bucket, Key=INDEX_READY, Body=b"1", ContentType="text/plain"
    )
    return len(live)
