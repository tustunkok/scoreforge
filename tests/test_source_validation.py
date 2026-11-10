"""The seam: "given a URL the user pasted, is it acceptable?"

These are the acceptance rules from the product side - only public YouTube
videos, never playlists or channels, never another host.
"""

from __future__ import annotations

import pytest

from scoreforge import InvalidSourceError, validate_source


@pytest.mark.parametrize(
    "url",
    [
        "https://youtu.be/ZOaFiTStPt4",
        "https://www.youtube.com/watch?v=ZOaFiTStPt4",
    ],
)
def test_accepts_only_the_supported_youtube_formats(url):
    assert validate_source(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "https://youtu.be/dQw4w9WgXcQ?si=tracking",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123",
        "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com/shorts/ZOaFiTStPt4",
        "https://youtube.com/watch?v=ZOaFiTStPt4",
        "http://youtu.be/ZOaFiTStPt4",
        "https://www.youtube.com/playlist?list=PL123",
        "https://www.youtube.com/channel/UC123",
        "https://www.youtube.com/@somechannel",
        "https://example.com/watch?v=ZOaFiTStPt4",
        "https://localhost:8000/",
        "file:///etc/passwd",
        "https://youtu.be/short",
        "https://www.youtube.com/watch?v=short",
    ],
)
def test_rejects_everything_else(url):
    with pytest.raises(InvalidSourceError):
        validate_source(url)


def test_rejected_urls_are_logged(caplog):
    caplog.set_level("WARNING", logger="scoreforge.pipeline")

    with pytest.raises(InvalidSourceError):
        validate_source("https://example.com/not-youtube")

    assert "rejected invalid source URL" in caplog.text
    assert "https://example.com/not-youtube" in caplog.text
