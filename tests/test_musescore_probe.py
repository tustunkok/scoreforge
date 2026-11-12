"""The seam: "doctor and the pipeline actually run MuseScore".

A file that exists and is marked executable can still be unreadable by the
service user - an AppImage extracted as root leaves `squashfs-root` at mode
700, so every render dies with "AppRun: Permission denied" while a naive
presence check reports OK. These tests pin down the behaviour that caught it.

The fake binary is driven through a stubbed ``subprocess.run`` because a shell
script is not a runnable executable on Windows.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scoreforge import cli
from scoreforge.errors import MuseScoreNotFoundError, MuseScoreUnusableError
from scoreforge.musescore import find_musescore, probe_musescore


def _fake_musescore(tmp_path: Path) -> Path:
    exe = tmp_path / "mscore"
    exe.write_text("not a real binary\n", encoding="utf-8")
    exe.chmod(0o755)
    return exe


def _stub_run(
    monkeypatch, *, stdout: str = "", stderr: str = "", code: int = 0, boom=None
):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if boom is not None:
            raise boom
        return subprocess.CompletedProcess(cmd, code, stdout, stderr)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_probe_executes_the_binary_and_reports_its_version(tmp_path, monkeypatch):
    exe = _fake_musescore(tmp_path)
    calls = _stub_run(monkeypatch, stdout="MuseScore4 4.7.6\n")

    assert "4.7.6" in probe_musescore(exe)
    assert calls == [[str(exe), "--version"]]


def test_probe_finds_the_version_on_stderr(tmp_path, monkeypatch):
    """MuseScore prints the banner to stderr when a display is missing."""
    exe = _fake_musescore(tmp_path)
    _stub_run(monkeypatch, stderr="Using fallback for library\nMuseScore4 4.7.6\n")

    assert "4.7.6" in probe_musescore(exe)


def test_probe_rejects_a_non_executable_file(tmp_path):
    exe = _fake_musescore(tmp_path)
    exe.chmod(0o644)

    with pytest.raises(MuseScoreUnusableError) as caught:
        probe_musescore(exe, timeout=5)

    assert caught.value.code == "musescore_unusable"


def test_probe_explains_a_permission_failure(tmp_path, monkeypatch):
    exe = _fake_musescore(tmp_path)
    _stub_run(monkeypatch, boom=PermissionError(13, "Permission denied"))

    with pytest.raises(MuseScoreUnusableError) as caught:
        probe_musescore(exe)

    detail = (caught.value.detail or "").lower()
    assert "permission denied" in detail
    assert "not readable by this user" in detail


def test_probe_explains_an_unreadable_directory(tmp_path, monkeypatch):
    """The exact production bug: mode 700 on the extracted AppImage."""
    exe = _fake_musescore(tmp_path)
    _stub_run(monkeypatch, boom=PermissionError(13, "Permission denied"))

    with pytest.raises(MuseScoreUnusableError):
        probe_musescore(exe)


def test_probe_reports_a_non_zero_exit_with_a_hint(tmp_path, monkeypatch):
    exe = _fake_musescore(tmp_path)
    _stub_run(monkeypatch, stderr="cannot open display\n", code=1)

    with pytest.raises(MuseScoreUnusableError) as caught:
        probe_musescore(exe)

    assert "xvfb-run" in (caught.value.detail or "")


def test_probe_times_out_instead_of_hanging(tmp_path, monkeypatch):
    exe = _fake_musescore(tmp_path)
    _stub_run(monkeypatch, boom=subprocess.TimeoutExpired(str(exe), 1))

    with pytest.raises(MuseScoreUnusableError) as caught:
        probe_musescore(exe, timeout=1)

    assert "did not answer in time" in caught.value.message


def test_probe_passes_a_timeout_to_the_subprocess(tmp_path, monkeypatch):
    exe = _fake_musescore(tmp_path)
    seen = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, "MuseScore4 4.7.6", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    probe_musescore(exe, timeout=42)

    assert seen["timeout"] == 42


def test_find_prefers_the_environment_variable(tmp_path, monkeypatch):
    exe = _fake_musescore(tmp_path)
    monkeypatch.setenv("SCOREFORGE_MUSESCORE", str(exe))

    assert find_musescore(None) == exe


def test_find_raises_when_nothing_is_readable(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOREFORGE_MUSESCORE", str(tmp_path / "nope"))
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr("scoreforge.musescore.LINUX_CANDIDATES", ())
    monkeypatch.setattr("scoreforge.musescore.WINDOWS_CANDIDATES", ())

    with pytest.raises(MuseScoreNotFoundError):
        find_musescore(None)


def test_doctor_reports_a_broken_install_as_broken(tmp_path, monkeypatch, capsys):
    exe = _fake_musescore(tmp_path)
    monkeypatch.setenv("SCOREFORGE_MUSESCORE", str(exe))
    _stub_run(monkeypatch, stderr="AppRun: Permission denied\n", code=126)

    exit_code = cli.main(["doctor"])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "BROKEN" in output
    assert "MuseScore" in output
    assert "not readable by this user" in output


def test_doctor_reports_a_working_install_with_its_version(tmp_path, monkeypatch, capsys):
    exe = _fake_musescore(tmp_path)
    monkeypatch.setenv("SCOREFORGE_MUSESCORE", str(exe))
    _stub_run(monkeypatch, stdout="MuseScore4 4.7.6\n")

    cli.main(["doctor"])

    output = capsys.readouterr().out
    assert "OK" in output
    assert "4.7.6" in output
    assert "BROKEN" not in output
