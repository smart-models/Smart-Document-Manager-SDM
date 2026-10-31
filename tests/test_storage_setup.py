"""Local storage activation preserves all existing application credentials."""

from scripts.configure_storage import configure


def test_setup_preserves_secrets_and_is_repeatable(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "# keep this comment\nOCR_SERVER_API_TOKENS=existing-token:console\n"
        "OCR_S3_SECRET_KEY=existing-storage-secret\nCOMPOSE_PROFILES=debug\n",
        encoding="utf-8",
    )
    configure(path)
    first = path.read_text(encoding="utf-8")
    configure(path)
    assert path.read_text(encoding="utf-8") == first
    assert "OCR_SERVER_API_TOKENS=existing-token:console" in first
    assert "OCR_S3_SECRET_KEY=existing-storage-secret" in first
    assert "COMPOSE_PROFILES=debug,storage" in first
    assert "OCR_STORAGE_ENABLED=true" in first
    assert "OCR_S3_RETENTION_DAYS=30" in first
