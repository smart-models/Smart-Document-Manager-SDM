"""Offline qualification of frozen OCR RAG manifests and retrieval runs."""

from .manifest import validate_manifest
from .evaluation import evaluate

__all__ = ["evaluate", "validate_manifest"]
