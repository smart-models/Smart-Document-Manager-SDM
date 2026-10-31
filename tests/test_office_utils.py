"""Verify Office format detection and isolated PDF conversion guards."""

import io
import subprocess
import zipfile
from pathlib import Path

import pytest

from ocrserver import office_utils
from ocrserver.office_utils import OfficeError, convert_to_pdf, office_format

OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
PDF = b"%PDF-1.7\n%%EOF\n"


def make_zip(entries: dict[str, bytes], stored_first: str | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        if stored_first:
            archive.writestr(
                stored_first, entries.pop(stored_first), zipfile.ZIP_STORED
            )
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def ooxml(marker: str) -> bytes:
    return make_zip({"[Content_Types].xml": b"<Types/>", marker: b"<x/>"})


def odf(suffix: str) -> bytes:
    mimetype = f"application/vnd.oasis.opendocument.{suffix}".encode()
    return make_zip(
        {"mimetype": mimetype, "content.xml": b"<x/>"}, stored_first="mimetype"
    )


def ole2(stream: str) -> bytes:
    name = stream.encode("utf-16-le") + b"\x00\x00"
    return OLE2 + b"\x00" * 504 + name + b"\x00" * 64


@pytest.mark.parametrize(
    "data,kind",
    [
        (ooxml("word/document.xml"), "docx"),
        (ooxml("ppt/presentation.xml"), "pptx"),
        (ooxml("xl/workbook.xml"), "xlsx"),
        (odf("text"), "odt"),
        (odf("presentation"), "odp"),
        (odf("spreadsheet"), "ods"),
        (ole2("WordDocument"), "doc"),
        (ole2("PowerPoint Document"), "ppt"),
        (ole2("Workbook"), "xls"),
        (ole2("Book"), "xls"),
        (ole2("Unknown"), "ole2"),
    ],
)
def test_detects_office_formats(data, kind):
    assert office_format(data) == kind


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"random bytes",
        PDF,
        make_zip({"readme.txt": b"hello"}),
        make_zip({"word/document.xml": b"<x/>"}),  # no [Content_Types].xml
        make_zip({"mimetype": b"application/zip"}, stored_first="mimetype"),
        make_zip({"mimetype": b"application/vnd.oasis.opendocument.graphics"}),
        ooxml("word/document.xml")[:40],  # truncated zip
        b"PK\x03\x04" + b"\x00" * 100,
    ],
)
def test_rejects_non_office_inputs(data):
    assert office_format(data) is None


def manifest_only(suffix: str, root_first: bool = True) -> bytes:
    root = (
        "<manifest:file-entry manifest:media-type="
        f'"application/vnd.oasis.opendocument.{suffix}" manifest:full-path="/" />'
    )
    other = (
        '<manifest:file-entry manifest:full-path="Object 1/" manifest:media-type='
        '"application/vnd.oasis.opendocument.chart" />'
    )
    entries = root + other if root_first else other + root
    manifest = (
        '<?xml version="1.0"?><manifest:manifest xmlns:manifest='
        f'"urn:oasis:names:tc:opendocument:xmlns:manifest:1.0">{entries}'
        "</manifest:manifest>"
    ).encode()
    return make_zip({"content.xml": b"<x/>", "META-INF/manifest.xml": manifest})


@pytest.mark.parametrize(
    "data,kind",
    [
        (manifest_only("spreadsheet"), "ods"),
        (manifest_only("text", root_first=False), "odt"),
        (manifest_only("presentation"), "odp"),
        (manifest_only("graphics"), None),
    ],
)
def test_odf_without_mimetype_uses_manifest_root(data, kind):
    assert office_format(data) == kind


def test_odf_mimetype_read_is_capped():
    huge = b"application/vnd.oasis.opendocument.text" + b"x" * 10_000_000
    assert office_format(make_zip({"mimetype": huge})) is None


class FakePopen:
    """Stands in for soffice: records the command, writes the output PDF."""

    calls: list[list[str]] = []
    returncode = 0
    output: bytes | None = PDF
    stderr = b""
    hang = False

    def __init__(self, cmd, **kwargs):
        self.cmd = cmd
        self.kwargs = kwargs
        self.pid = 4242
        FakePopen.calls.append(cmd)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def communicate(self, timeout=None):
        if self.hang and timeout is not None:
            raise subprocess.TimeoutExpired(self.cmd, timeout)
        outdir = Path(self.cmd[self.cmd.index("--outdir") + 1])
        source = Path(self.cmd[-1])
        assert source.read_bytes() == b"doc bytes"
        if self.output is not None:
            (outdir / f"{source.stem}.pdf").write_bytes(self.output)
        return b"", self.stderr

    def kill(self):
        pass


@pytest.fixture
def popen(monkeypatch):
    FakePopen.calls = []
    monkeypatch.setattr(office_utils.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(office_utils, "_kill", lambda proc: None)
    yield FakePopen
    FakePopen.returncode, FakePopen.output = 0, PDF
    FakePopen.stderr, FakePopen.hang = b"", False


def test_convert_success_uses_isolated_profile(popen):
    assert convert_to_pdf(b"doc bytes", "docx", soffice="soffice", timeout=5) == PDF
    cmd = popen.calls[0]
    assert cmd[0] == "soffice"
    assert "--headless" in cmd and "--norestore" in cmd and "--nolockcheck" in cmd
    assert cmd[cmd.index("--convert-to") + 1] == "pdf"
    assert Path(cmd[-1]).name == "input.docx"
    profile = next(arg for arg in cmd if arg.startswith("-env:UserInstallation="))
    assert profile.startswith("-env:UserInstallation=file://")
    # The temporary directory, including the profile, is gone afterwards.
    assert not Path(cmd[-1]).parent.exists()


def test_convert_nonzero_exit(popen):
    popen.returncode = 1
    popen.stderr = b"x" * 2000
    with pytest.raises(OfficeError) as info:
        convert_to_pdf(b"doc bytes", "doc", soffice="soffice", timeout=5)
    assert info.value.code == "office_conversion_failed"
    assert len(str(info.value)) <= 600


@pytest.mark.parametrize("output", [None, b""])
def test_convert_missing_or_empty_output(popen, output):
    popen.output = output
    with pytest.raises(OfficeError) as info:
        convert_to_pdf(b"doc bytes", "pptx", soffice="soffice", timeout=5)
    assert info.value.code == "office_conversion_failed"


def test_convert_timeout(popen):
    popen.hang = True
    with pytest.raises(OfficeError) as info:
        convert_to_pdf(b"doc bytes", "xlsx", soffice="soffice", timeout=0.1)
    assert info.value.code == "office_conversion_timeout"


def test_convert_missing_binary(monkeypatch):
    monkeypatch.setattr(office_utils.shutil, "which", lambda name: None)
    with pytest.raises(OfficeError) as info:
        convert_to_pdf(b"doc bytes", "odt", soffice="", timeout=5)
    assert info.value.code == "office_converter_unavailable"


def test_convert_configured_binary_not_found():
    with pytest.raises(OfficeError) as info:
        convert_to_pdf(
            b"doc bytes", "odt", soffice="/nonexistent/soffice-binary", timeout=5
        )
    assert info.value.code == "office_converter_unavailable"


def test_office_settings_defaults_and_environment():
    from ocrserver.config import Settings, load_settings

    defaults = load_settings({}, None)
    assert defaults.office_enabled is True
    assert defaults.office_soffice_path == ""
    assert defaults.office_timeout_seconds == 120.0
    custom = load_settings(
        {
            "OCR_OFFICE_ENABLED": "false",
            "OCR_SOFFICE_PATH": "/opt/lo/soffice",
            "OCR_OFFICE_TIMEOUT_SECONDS": "30.5",
        },
        None,
    )
    assert custom.office_enabled is False
    assert custom.office_soffice_path == "/opt/lo/soffice"
    assert custom.office_timeout_seconds == 30.5
    for changes in (
        {"office_timeout_seconds": 0},
        {"office_timeout_seconds": float("nan")},
        {"office_enabled": "yes"},
    ):
        with pytest.raises(ValueError):
            Settings(**changes)
    with pytest.raises(TypeError):
        Settings(office_soffice_path=None)
    with pytest.raises(ValueError):
        load_settings({"OCR_OFFICE_ENABLED": "maybe"}, None)


def test_conversions_are_serialized(popen, monkeypatch):
    import threading
    import time

    active, peak = [0], [0]
    original = FakePopen.communicate

    def slow(self, timeout=None):
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        time.sleep(0.05)
        active[0] -= 1
        return original(self, timeout)

    monkeypatch.setattr(FakePopen, "communicate", slow)
    threads = [
        threading.Thread(
            target=convert_to_pdf,
            args=(b"doc bytes", "docx"),
            kwargs={"soffice": "soffice", "timeout": 5},
        )
        for _ in range(3)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(popen.calls) == 3
    assert peak[0] == 1


def test_failure_excerpt_hides_temporary_paths(popen, monkeypatch):
    def failing(self, timeout=None):
        outdir = self.cmd[self.cmd.index("--outdir") + 1]
        self.returncode = 1
        return (
            b"",
            f"Error: source file {outdir}/input.docx could not be loaded".encode(),
        )

    monkeypatch.setattr(FakePopen, "communicate", failing)
    with pytest.raises(OfficeError) as info:
        convert_to_pdf(b"doc bytes", "docx", soffice="soffice", timeout=5)
    message = str(info.value)
    assert "ocr-office-" not in message
    assert "<tmp>/input.docx could not be loaded" in message


@pytest.mark.parametrize("suffix", ["text", "presentation", "spreadsheet"])
def test_manifest_accepts_single_quotes(suffix):
    manifest = (
        "<manifest:manifest "
        "xmlns:manifest='urn:oasis:names:tc:opendocument:xmlns:manifest:1.0'>"
        "<manifest:file-entry manifest:full-path='/' "
        f"manifest:media-type='application/vnd.oasis.opendocument.{suffix}'/>"
        "</manifest:manifest>"
    ).encode()
    assert (
        office_format(make_zip({"META-INF/manifest.xml": manifest}))
        == {
            "text": "odt",
            "presentation": "odp",
            "spreadsheet": "ods",
        }[suffix]
    )


def test_expired_conversion_never_starts_process(popen):
    import time

    with pytest.raises(OfficeError, match="deadline exceeded"):
        convert_to_pdf(
            b"doc bytes",
            "docx",
            soffice="soffice",
            timeout=5,
            deadline=time.monotonic() - 1,
        )
    assert not popen.calls


def test_conversion_lock_wait_obeys_deadline(popen):
    import time

    office_utils._SOFFICE_LOCK.acquire()
    try:
        started = time.monotonic()
        with pytest.raises(OfficeError, match="deadline exceeded"):
            convert_to_pdf(
                b"doc bytes",
                "docx",
                soffice="soffice",
                timeout=5,
                deadline=started + 0.02,
            )
        assert time.monotonic() - started < 1
        assert not popen.calls
    finally:
        office_utils._SOFFICE_LOCK.release()
