"""Local report CLI; no inference, network, embeddings or generated QA."""

import argparse
import json
import os
from pathlib import Path
import tempfile

from . import evaluate, validate_manifest


def main(argv=None):
    """Validate a local manifest or evaluate runs and atomically save a report."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "evaluate"):
        command = commands.add_parser(name)
        command.add_argument("manifest", type=Path)
        if name == "evaluate":
            command.add_argument("runs", type=Path)
        command.add_argument("--output", type=Path)
    arguments = parser.parse_args(argv)
    try:
        report = (
            validate_manifest(arguments.manifest)
            if arguments.command == "validate"
            else evaluate(arguments.manifest, arguments.runs)
        )
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        report = {
            "valid": False,
            "qualified": False,
            "errors": [{"code": "invalid_or_unreadable_input"}],
        }
    rendered = (
        json.dumps(
            report,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
        )
        + "\n"
    )
    if arguments.output:
        protected = {arguments.manifest.resolve()}
        if arguments.command == "evaluate":
            protected.add(arguments.runs.resolve())
        if arguments.output.resolve() in protected:
            parser.error("Output cannot replace an input artifact")
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=arguments.output.parent,
                prefix="rag-report-",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                stream.write(rendered)
            os.replace(temporary, arguments.output)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
    else:
        print(rendered, end="")
    if not report["valid"]:
        return 2
    success = (
        report.get("ready_for_evaluation", False)
        if arguments.command == "validate"
        else report["qualified"]
    )
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
