"""Every file SDM ships under another licence travels with its licence text.

The console fonts and vendored libraries under ``frontend/dist/assets/`` reach
users inside the ``ocr-api`` image, the guides reach them inside the MCP wheel,
the skills under ``ocr-skill/`` are MIT, and both images carry SDM's own
``LICENSE``. ``NOTICE`` is the index: it names each component and the file that
holds its licence. These checks keep the licence texts, the index and the
packaging in step.
"""

from __future__ import annotations

import fnmatch
import json
import re
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ("smart-document-manager", "smart-document-reader")
FRONTEND = ROOT / "frontend"
KATEX_FONTS = "dist/assets/vendor/katex/fonts/"
LICENCE_PATH = re.compile(r"[\w./-]+/[\w.-]*(?:LICEN[CS]E|OFL)[\w.-]*")


def read(relative: str) -> str:
    """Read a repository text file with normalised line endings."""
    return (ROOT / relative).read_text(encoding="utf-8")


def manifest_assets() -> list[dict[str, str]]:
    """Return the vendored console assets recorded in the vendor manifest."""
    return json.loads(read("frontend/vendor-manifest.json"))["assets"]


def dockerfile_stage(stage: str) -> str:
    """Return the body of one target of the multi-stage Dockerfile."""
    stages = re.split(r"(?m)^FROM\s", read("Dockerfile"))
    return next(part for part in stages if re.search(rf"\bAS {stage}\s*$", part, re.M))


def copy_sources(body: str) -> set[str]:
    """Return every build-context source that a stage's COPY instructions name."""
    sources = set()
    for line in body.replace("\\\n", " ").splitlines():
        if line.startswith("COPY "):
            words = [word for word in line.split()[1:] if not word.startswith("--")]
            sources.update(words[:-1])
    return sources


def test_console_fonts_ship_the_guide_font_notices():
    notices = read("frontend/dist/assets/fonts/FONT-LICENSES.txt")
    assert notices == read("guide/FONT-LICENSES.txt")
    lines = notices.splitlines()
    noticed = {
        heading.lower().replace(" ", "-")
        for heading, following in zip(lines, lines[1:])
        if re.match(r"Copyright \d{4} ", following)
    }
    shipped = {
        re.sub(r"-(\d+|var)$", "", font.stem)
        for font in (FRONTEND / "dist/assets/fonts").glob("*.woff2")
    }
    assert shipped and shipped <= noticed
    assert notices.count("SIL OPEN FONT LICENSE Version 1.1") == len(noticed)


def test_katex_fonts_are_declared_ofl_and_name_every_reserved_font():
    fonts = [
        asset
        for asset in manifest_assets()
        if asset["path"].startswith(KATEX_FONTS) and "KaTeX_" in asset["path"]
    ]
    assert len(fonts) == 60
    assert {asset["license"] for asset in fonts} == {"OFL-1.1"}
    assert {asset["license_file"] for asset in fonts} == {KATEX_FONTS + "OFL.txt"}
    notice = (FRONTEND / KATEX_FONTS / "OFL.txt").read_text(encoding="utf-8")
    assert "SIL OPEN FONT LICENSE Version 1.1" in notice
    for asset in fonts:
        family = Path(asset["path"]).name.split("-")[0]
        assert re.search(rf"\b{family}\b", notice), family


def test_notice_names_every_vendored_component_and_licence_file():
    notice = read("NOTICE")
    assets = manifest_assets()
    for name, version in {(asset["name"], asset["version"]) for asset in assets}:
        assert f"{name} {version}" in notice, name
    named = {path.rstrip(".") for path in LICENCE_PATH.findall(notice)}
    for path in named:
        assert (ROOT / path).is_file(), path
    for asset in assets:
        assert f"frontend/{asset['license_file']}" in named, asset["path"]


def test_both_images_carry_the_repository_licence():
    ignored = [
        line.strip().lstrip("/")
        for line in read(".dockerignore").splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "!"))
    ]
    for name in ("LICENSE", "NOTICE", "ocr-mcp-server/LICENSE"):
        assert not any(fnmatch.fnmatch(name, pattern) for pattern in ignored), name
    api = dockerfile_stage("ocr-api")
    mcp = dockerfile_stage("ocr-mcp")
    assert {"LICENSE", "NOTICE"} <= copy_sources(api)
    assert "ocr-mcp-server/LICENSE" in copy_sources(mcp)
    for body in (api, mcp):
        assert 'org.opencontainers.image.licenses="AGPL-3.0-only"' in body


def test_mcp_wheel_declares_and_bundles_the_repository_licence():
    project = tomllib.loads(read("ocr-mcp-server/pyproject.toml"))["project"]
    assert project["license"] == "AGPL-3.0-only"
    assert project["license-files"] == ["LICENSE"]
    assert read("ocr-mcp-server/LICENSE") == read("LICENSE")


def test_skills_are_mit_with_the_copyright_filled_in():
    licences = {read(f"ocr-skill/{skill}/LICENSE.txt") for skill in SKILLS}
    assert len(licences) == 1
    (licence,) = licences
    assert licence.startswith(
        "MIT License\n\nCopyright (c) 2026 Federico Palma and the contributors to "
        "Smart Document\nManager (SDM)\n\nPermission is hereby granted, free of charge"
    )
    assert 'THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND' in licence
    notice = read("NOTICE")
    for skill in SKILLS:
        frontmatter = read(f"ocr-skill/{skill}/SKILL.md").split("---", 2)[1]
        assert yaml.safe_load(frontmatter)["license"].startswith("MIT."), skill
        assert f"ocr-skill/{skill}/LICENSE.txt" in notice
