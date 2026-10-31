"""The release workflow publishes one version everywhere; these checks catch a mismatch before a tag does."""

import json
import re
import tomllib
from pathlib import Path

import openring

ROOT = Path(__file__).resolve().parent.parent


def test_every_version_number_matches():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"]
    assert pyproject == package == openring.__version__


def test_launcher_installs_its_own_version_from_pypi():
    launcher = (ROOT / "bin" / "openring.js").read_text(encoding="utf-8")
    assert 'require("../package.json").version' in launcher
    assert re.search(r"openring\[kokoro,whisper\]==\$\{VERSION\}", launcher)
    assert "OPENRING_SOURCE" in launcher  # a local checkout can still be run


def test_third_party_lists_every_direct_dependency():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    notices = (ROOT / "THIRD_PARTY.md").read_text(encoding="utf-8").lower()
    extras = project["optional-dependencies"]
    deps = project["dependencies"] + extras["kokoro"] + extras["whisper"]
    names = {re.match(r"[A-Za-z0-9_.-]+", d)[0].lower() for d in deps}
    assert {n for n in names if f"[{n}]" not in notices} == set()


def test_release_workflow_uses_trusted_publishing():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "id-token: write" in workflow
    assert "pypa/gh-action-pypi-publish" in workflow and "npm stage publish . --provenance" in workflow
    assert "secrets." not in workflow  # no stored tokens
