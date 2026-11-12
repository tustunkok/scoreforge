"""The seam: "the shipped container does not run privileged".

These are cheap assertions on the deployment files - the failure mode they
guard against (a public service running as root with a writable filesystem)
is invisible at runtime until it is exploited.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def dockerfile() -> str:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    # Collapse line continuations so assertions can look at whole commands.
    return text.replace("\\\n", " ").replace("\\\r\n", " ")


@pytest.fixture(scope="module")
def compose() -> dict:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load((ROOT / "docker-compose.yaml").read_text(encoding="utf-8"))


def test_the_image_switches_to_a_non_root_user(dockerfile):
    users = [
        line for line in dockerfile.splitlines() if line.strip().startswith("USER ")
    ]

    assert users, "the image never drops privileges"
    assert users[-1].strip() != "USER root"
    assert "useradd" in dockerfile


def test_the_service_home_is_writable_by_that_user(dockerfile):
    assert "HOME=/data/home" in dockerfile


def test_the_extracted_musescore_is_readable_by_the_service_user(dockerfile):
    """The AppImage is extracted as root, so squashfs-root lands at mode 700.

    Without a recursive widening chmod the service user cannot even traverse
    into it, and every render fails with "AppRun: Permission denied" while a
    mere presence check still reports OK.
    """
    assert "chmod -R a+rX /opt/musescore/squashfs-root" in dockerfile


def test_the_image_checks_musescore_actually_runs(dockerfile):
    """`doctor` must execute the binary, not just look at it."""
    cli = (ROOT / "src" / "scoreforge" / "cli.py").read_text(encoding="utf-8")

    assert "probe_musescore" in cli
    assert "BROKEN" in cli


def test_compose_runs_the_container_unprivileged(compose):
    service = compose["services"]["scoreforge"]

    assert service["user"] != "0" and not service["user"].startswith("0:")
    assert service["read_only"] is True
    assert "no-new-privileges:true" in service["security_opt"]
    assert service["cap_drop"] == ["ALL"]
    assert service["pids_limit"] > 0


def test_compose_keeps_the_input_limits(compose):
    env = compose["services"]["scoreforge"]["environment"]

    assert int(env["SCOREFORGE_RATE_LIMIT"]) <= 10
    assert int(env["SCOREFORGE_MAX_QUEUE_DEPTH"]) <= 50
    assert int(env["SCOREFORGE_RESULT_TTL_HOURS"]) <= 24 * 7
    assert int(env["SCOREFORGE_MIN_FREE_MB"]) >= 256
