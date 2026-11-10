"""The released version is declared once and must never drift."""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

import scoreforge

ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")


def _project_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def test_package_version_matches_pyproject():
    assert scoreforge.__version__ == _project_version()


def test_version_is_semver():
    assert SEMVER.match(scoreforge.__version__), scoreforge.__version__


def test_cli_reports_the_version():
    result = subprocess.run(
        [sys.executable, "-m", "scoreforge.cli", "--version"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )

    assert result.returncode == 0
    assert scoreforge.__version__ in result.stdout


def test_publish_workflow_tags_the_project_version():
    """A push to master must publish a tag equal to pyproject's version."""
    workflow = (ROOT / ".github" / "workflows" / "publish.yml").read_text(
        encoding="utf-8"
    )

    assert "steps.version.outputs.version" in workflow
