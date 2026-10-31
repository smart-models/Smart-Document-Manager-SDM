"""Keep workflow wiring, dependency installs and release evidence consistent."""

import json
import re
import shlex
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
FRONTEND = Path("frontend")
LOCK = FRONTEND / "package-lock.json"


def _load_workflow(path):
    """Read scalar strings without YAML 1.1 interpreting the key on as True."""
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


@pytest.fixture(params=sorted(WORKFLOWS.glob("*.yml")), ids=lambda path: path.name)
def workflow(request):
    """Provide every workflow, including new files added to the directory."""
    return request.param.name, _load_workflow(request.param)


def test_aggregate_requires_every_job_and_rejects_incomplete_results():
    jobs = _load_workflow(WORKFLOWS / "ci.yml")["jobs"]
    aggregate = jobs["ci"]
    assert aggregate["name"] == "CI"
    assert set(aggregate["needs"]) == set(jobs) - {"ci"}
    assert aggregate["if"] == "always()"
    assert aggregate.get("continue-on-error", "false") == "false"
    failures = [step for step in aggregate["steps"] if step.get("run") == "exit 1"]
    assert len(failures) == 1
    assert " ".join(failures[0]["if"].split()) == " || ".join(
        f"contains(needs.*.result, '{result}')"
        for result in ("failure", "cancelled", "skipped")
    )
    assert failures[0].get("continue-on-error", "false") == "false"


def test_actions_are_pinned_and_checkout_does_not_persist_credentials(workflow):
    _, config = workflow
    actions = []
    for job in config["jobs"].values():
        actions.extend(step for step in job.get("steps", []) if "uses" in step)
        if "uses" in job:
            actions.append(job)
    assert actions
    for action in actions:
        reference = action["uses"]
        assert reference.startswith("./.github/workflows/") or re.fullmatch(
            r"[\w./-]+@[0-9a-f]{40}", reference
        ), reference
        if reference.startswith("actions/checkout@"):
            assert action["with"]["persist-credentials"] == "false"


def test_only_dependency_audit_can_write_issues(workflow):
    name, config = workflow
    expected = {"contents": "read"}
    if name == "dependency-audit.yml":
        expected["issues"] = "write"
    assert config["permissions"] == expected
    for job in config["jobs"].values():
        permissions = job.get("permissions", expected)
        assert isinstance(permissions, dict)
        assert permissions.items() <= expected.items()


def test_frontend_installation_uses_committed_lock_and_matching_cache(workflow):
    _, config = workflow
    for job in config["jobs"].values():
        steps = job.get("steps", [])
        node_steps = [
            step
            for step in steps
            if step.get("uses", "").startswith("actions/setup-node@")
        ]
        if not node_steps:
            continue
        assert len(node_steps) == 1
        assert node_steps[0]["with"]["node-version"] == "24"
        assert node_steps[0]["with"]["cache"] == "npm"
        assert node_steps[0]["with"]["cache-dependency-path"] == LOCK.as_posix()
        installs = [
            step
            for step in steps
            if re.search(r"\bnpm (?:install|ci)\b", step.get("run", ""))
        ]
        assert len(installs) == 1
        assert installs[0]["run"] == "npm ci --ignore-scripts"
        assert installs[0]["working-directory"] == FRONTEND.as_posix()
        assert steps.index(node_steps[0]) < steps.index(installs[0])
    if not (ROOT / FRONTEND / "package.json").exists():
        pytest.skip("the frontend is not part of this checkout")
    package = json.loads((ROOT / FRONTEND / "package.json").read_text())
    lock = json.loads((ROOT / LOCK).read_text())
    assert lock["packages"][""]["devDependencies"] == package["devDependencies"]


def test_release_requires_one_sbom_per_committed_python_lock():
    config = _load_workflow(WORKFLOWS / "release-check.yml")
    steps = config["jobs"]["offline-gate"]["steps"]
    manifests = [
        step for step in steps if "scripts/release_evidence.py" in step.get("run", "")
    ]
    assert len(manifests) == 1
    assert manifests[0]["if"] == "always()"
    # comments=True reads the command the way bash will: a "# ..." line folded
    # into this scalar silently drops every --require after it.
    args = shlex.split(manifests[0]["run"], comments=True)
    required = [args[index + 1] for index, arg in enumerate(args) if arg == "--require"]
    assert len(required) == len(set(required))
    locks = list((ROOT / "requirements" / "locks").glob("*.txt"))
    if not locks:
        pytest.fail("dependency inventories must exist")
    assert {name for name in required if name.startswith("sbom/")} == {
        f"sbom/{lock.stem}.cdx.json" for lock in locks
    }
    producers = [
        shlex.split(step["run"])
        for step in steps
        if "scripts/verify_dependencies.py" in step.get("run", "")
    ]
    assert len(producers) == 1
    evidence_dir = args[2]
    assert producers[0][producers[0].index("--sbom") + 1] == f"{evidence_dir}/sbom"
    uploads = [
        step
        for step in steps
        if step.get("uses", "").startswith("actions/upload-artifact@")
    ]
    assert len(uploads) == 1
    assert uploads[0]["if"] == "always()"
    assert uploads[0]["with"]["path"].rstrip("/") == evidence_dir
    assert uploads[0]["with"]["if-no-files-found"] == "error"


def test_no_comment_is_folded_into_a_single_line_command(workflow):
    """A ``# ...`` line inside a ``run: >-`` scalar is not a YAML comment.

    Folding joins it into the command, and bash then ignores everything after
    it: arguments vanish while the step stays green. A literal ``|`` block keeps
    its newlines, so a comment line there is harmless and is not flagged.
    """
    _, config = workflow
    for job_name, job in config["jobs"].items():
        for step in job.get("steps", []):
            run = step.get("run", "").strip()
            if not run or "\n" in run:
                continue
            assert shlex.split(run, comments=True) == shlex.split(run), (
                f"{job_name}: a '#' token in {step.get('name', run[:40])!r} "
                "drops the rest of the command; move the note above 'run:'"
            )


def test_workflow_syntax_validation_is_required_by_ci():
    config = _load_workflow(WORKFLOWS / "ci.yml")
    steps = config["jobs"]["lint"]["steps"]
    validators = [
        step for step in steps if '"$RUNNER_TEMP/actionlint"' in step.get("run", "")
    ]
    assert len(validators) == 1
    assert validators[0].get("continue-on-error", "false") == "false"


def test_release_reuses_identical_ci_and_only_current_attempt_artifacts():
    release = _load_workflow(WORKFLOWS / "release-check.yml")
    assert release["jobs"]["checks"]["uses"] == "./.github/workflows/ci.yml"
    assert release["jobs"]["offline-gate"]["needs"] == "checks"
    steps = release["jobs"]["offline-gate"]["steps"]
    downloads = [
        step
        for step in steps
        if step.get("uses", "").startswith("actions/download-artifact@")
    ]
    assert len(downloads) == 1
    assert downloads[0]["with"]["pattern"] == "ci-*-${{ github.run_attempt }}"
    ci = _load_workflow(WORKFLOWS / "ci.yml")
    for job in ci["jobs"].values():
        for step in job.get("steps", []):
            if step.get("uses", "").startswith("actions/upload-artifact@"):
                assert step["with"]["name"].endswith("-${{ github.run_attempt }}")
    guards = [
        step for step in steps if step.get("run") == 'test "$CHECK_RESULT" = success'
    ]
    assert len(guards) == 1 and guards[0]["if"] == "always()"


def test_only_offline_checks_run_by_default_and_shell_presence_is_required():
    ci = _load_workflow(WORKFLOWS / "ci.yml")
    assert set(ci["on"]) >= {"push", "pull_request", "workflow_call"}
    assert ci["env"]["HF_HUB_OFFLINE"] == "1"
    assert ci["env"]["PYTHONUTF8"] == "1"
    shell_checks = "\n".join(
        step.get("run", "") for step in ci["jobs"]["launchers"]["steps"]
    )
    assert "command -v bash" in shell_checks and "command -v pwsh" in shell_checks
    serialized = json.dumps(ci)
    assert "--project=mock" in serialized and "--project=live" not in serialized
    assert "docker compose up" not in serialized


def test_audit_workflows_use_the_same_explicit_review_policy():
    for workflow in ("dependency-audit.yml", "release-check.yml"):
        config = _load_workflow(WORKFLOWS / workflow)
        audits = [
            step["run"]
            for job in config["jobs"].values()
            for step in job.get("steps", [])
            if "scripts/verify_dependencies.py" in step.get("run", "")
        ]
        assert len(audits) == 1
        assert "--triage requirements/audit-triage.json" in audits[0]
    ci = _load_workflow(WORKFLOWS / "ci.yml")
    serialized = json.dumps(ci)
    assert "docker build --check --target ocr-api ." in serialized
    assert "docker build --check --target ocr-mcp ." in serialized
    assert "docker build --check -f Dockerfile.vllm ." in serialized


def test_offline_guides_are_required_and_supply_release_evidence():
    ci = _load_workflow(WORKFLOWS / "ci.yml")
    assert "guides" in ci["jobs"]["ci"]["needs"]
    job = ci["jobs"]["guides"]
    commands = "\n".join(step.get("run", "") for step in job["steps"])
    assert "pytest guide/tests" in commands
    assert "--junitxml=ci-evidence/guides.xml" in commands
    assert "--config playwright.guides.config.js" in commands
    assert "tee ci-evidence/guides.log" in commands
    assert "npm ci --ignore-scripts" in commands
    assert "docker compose up" not in commands
    release = _load_workflow(WORKFLOWS / "release-check.yml")
    required = "\n".join(
        step.get("run", "") for step in release["jobs"]["offline-gate"]["steps"]
    )
    assert "--require guides.log" in required
    assert "--require guides.xml" in required
