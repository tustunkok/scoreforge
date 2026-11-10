"""The seam: "a submitted link can only pull a short, public video".

The download stage talks to yt-dlp and ffmpeg; both are stubbed here so the
guards around untrusted metadata can be tested without a network.
"""

from __future__ import annotations

import sys
import types

import pytest

from scoreforge import errors
from scoreforge.pipeline import (
    DOWNLOAD_TIMEOUT,
    MAX_DOWNLOAD_BYTES,
    ingest,
    ingest_url,
)

VALID = "https://youtu.be/ZOaFiTStPt4"


class FakeYDL:
    """Stands in for ``yt_dlp.YoutubeDL``; records the options it was given."""

    info: dict = {}
    options: dict = {}

    def __init__(self, options):
        FakeYDL.options = options

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        return FakeYDL.info


@pytest.fixture
def fake_ytdlp(monkeypatch):
    module = types.ModuleType("yt_dlp")
    module.YoutubeDL = FakeYDL
    monkeypatch.setitem(sys.modules, "yt_dlp", module)
    FakeYDL.info = {
        "id": "ZOaFiTStPt4",
        "title": "A test video",
        "duration": 90,
        "_type": "video",
    }
    return FakeYDL


@pytest.fixture
def commands(monkeypatch):
    """Capture subprocess commands instead of running them."""
    from scoreforge import pipeline

    seen: list[list[str]] = []

    def fake_run(cmd, *, timeout=None):
        from pathlib import Path

        seen.append(cmd)
        if cmd[0] == "yt-dlp":
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"fake audio")
        elif cmd[0] == "ffmpeg":
            Path(cmd[-1]).write_bytes(b"fake wav")
        return None

    monkeypatch.setattr(pipeline, "_run", fake_run)
    return seen


def test_a_local_path_is_never_used_as_a_source(tmp_path, fake_ytdlp, commands):
    secret = tmp_path / "secret.wav"
    secret.write_bytes(b"not for the public")

    with pytest.raises(errors.InvalidSourceError):
        ingest(str(secret), tmp_path / "work")

    assert not any("ffmpeg" in cmd[0] for cmd in commands)


def test_a_local_path_is_allowed_only_when_asked(tmp_path, commands):
    local = tmp_path / "song.m4a"
    local.write_bytes(b"fake audio")
    (tmp_path / "work").mkdir()

    wav, title = ingest(str(local), tmp_path / "work", allow_local=True)

    assert title == "song"
    assert wav.exists()


@pytest.mark.parametrize(
    "metadata",
    [
        {"id": "ZOaFiTStPt4", "duration": 60, "is_live": True},
        {"id": "ZOaFiTStPt4", "duration": 60, "live_status": "is_upcoming"},
    ],
)
def test_live_streams_are_rejected(tmp_path, fake_ytdlp, metadata):
    FakeYDL.info = {"title": "live", "_type": "video", **metadata}

    with pytest.raises(errors.InvalidSourceError):
        ingest_url(VALID, tmp_path / "work")


@pytest.mark.parametrize("duration", [None, 0, "90"])
def test_a_video_without_a_known_length_is_rejected(
    tmp_path, fake_ytdlp, duration
):
    FakeYDL.info = {"id": "ZOaFiTStPt4", "title": "x", "duration": duration}

    with pytest.raises(errors.InvalidSourceError):
        ingest_url(VALID, tmp_path / "work")


def test_an_overlong_video_is_rejected(tmp_path, fake_ytdlp):
    FakeYDL.info = {"id": "ZOaFiTStPt4", "title": "x", "duration": 4 * 3600}

    with pytest.raises(errors.SourceTooLongError):
        ingest_url(VALID, tmp_path / "work")


@pytest.mark.parametrize("video_id", ["../../etc/passwd", "short", "", "a" * 12])
def test_a_metadata_id_that_is_not_a_video_id_is_rejected(
    tmp_path, fake_ytdlp, video_id
):
    FakeYDL.info = {"id": video_id, "title": "x", "duration": 60}

    with pytest.raises(errors.InvalidSourceError):
        ingest_url(VALID, tmp_path / "work")


def test_the_download_runs_with_hard_limits(tmp_path, fake_ytdlp, commands):
    ingest_url(VALID, tmp_path / "work")

    download = next(cmd for cmd in commands if cmd[0] == "yt-dlp")
    assert "--ignore-config" in download
    assert "--no-playlist" in download
    assert "--max-filesize" in download
    assert str(MAX_DOWNLOAD_BYTES) in download


def test_the_metadata_call_loads_no_config(tmp_path, fake_ytdlp, commands):
    ingest_url(VALID, tmp_path / "work")

    assert FakeYDL.options["ignoreconfig"] is True
    assert FakeYDL.options["noplaylist"] is True


def test_the_download_has_a_timeout(tmp_path, fake_ytdlp, monkeypatch):
    import scoreforge.pipeline as pipeline

    timeouts: list[int | None] = []

    def fake_run(cmd, *, timeout=None):
        from pathlib import Path

        timeouts.append(timeout)
        if cmd[0] == "yt-dlp":
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"fake audio")
        else:
            Path(cmd[-1]).write_bytes(b"fake wav")

    monkeypatch.setattr(pipeline, "_run", fake_run)
    ingest_url(VALID, tmp_path / "work")

    assert timeouts[0] == DOWNLOAD_TIMEOUT
    assert all(timeout is not None for timeout in timeouts)
