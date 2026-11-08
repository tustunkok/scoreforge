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
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com/shorts/abc123",
        "youtube.com/watch?v=dQw4w9WgXcQ",
    ],
)
def test_accepts_single_youtube_videos(url):
    assert validate_source(url).startswith("http")


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "https://www.youtube.com/playlist?list=PL123",
        "https://www.youtube.com/channel/UC123",
        "https://www.youtube.com/@somechannel",
        "https://example.com/watch?v=dQw4w9WgXcQ",
        "https://localhost:8000/",
        "file:///etc/passwd",
    ],
)
def test_rejects_everything_else(url):
    with pytest.raises(InvalidSourceError):
        validate_source(url)
