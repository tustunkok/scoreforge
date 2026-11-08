"""The seam: "the user downloads one zip and finds everything in it"."""

from __future__ import annotations

import zipfile

import pytest

from scoreforge.package import build_package, slugify


@pytest.fixture
def score_dir(tmp_path):
    """A fake result directory, as the pipeline would leave it."""
    d = tmp_path / "score"
    d.mkdir()
    (d / "score.pdf").write_bytes(b"%PDF-1.7 fake")
    (d / "score.musicxml").write_text(
        '<?xml version="1.0"?><score-partwise version="4.0"><work>'
        "<work-title>Autumn Leaves</work-title></work></score-partwise>",
        encoding="utf-8",
    )
    (d / "score.mscz").write_bytes(b"PK fake mscz")
    (d / "score.mid").write_bytes(b"MThd fake midi")
    for index in (1, 2, 3):
        (d / f"page-{index}.png").write_bytes(b"\x89PNG fake")
    return d


def test_archive_contains_every_artifact(tmp_path, score_dir):
    archive = build_package(
        score_dir, tmp_path / "out", title="Autumn Leaves",
        source="https://youtu.be/abc",
    )

    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())

    assert "score.pdf" in names
    assert "score.musicxml" in names
    assert "score.mscz" in names
    assert "score.mid" in names
    assert "README.txt" in names
    assert {"pages/page-1.png", "pages/page-2.png", "pages/page-3.png"} <= names


def test_archive_is_named_after_the_title(tmp_path, score_dir):
    archive = build_package(
        score_dir, tmp_path / "out", title="Autumn Leaves (Live)",
        source="https://youtu.be/abc",
    )

    assert archive.name == "scoreforge-autumn-leaves-live.zip"


def test_readme_records_the_source_and_the_licence(tmp_path, score_dir):
    archive = build_package(
        score_dir, tmp_path / "out", title="Autumn Leaves",
        source="https://youtu.be/abc",
    )

    with zipfile.ZipFile(archive) as zf:
        readme = zf.read("README.txt").decode("utf-8")

    assert "Autumn Leaves" in readme
    assert "https://youtu.be/abc" in readme
    assert "non-commercial" in readme


def test_missing_files_are_simply_skipped(tmp_path, score_dir):
    (score_dir / "score.mscz").unlink()

    archive = build_package(
        score_dir, tmp_path / "out", title="Autumn Leaves",
        source="https://youtu.be/abc",
    )

    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())

    assert "score.mscz" not in names
    assert "score.pdf" in names


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Autumn Leaves", "autumn-leaves"),
        ("  Jazz  Standards!! ", "jazz-standards"),
        ("Someday / When?", "someday-when"),
        ("", "score"),
    ],
)
def test_slugs_are_safe_for_the_filesystem(title, expected):
    assert slugify(title) == expected
