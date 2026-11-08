"""Package the result of a run into a single downloadable zip.

The archive contains everything a user needs to keep or edit the score:

    scoreforge-<slug>.zip
    ├── score.pdf          engraved sheet music
    ├── score.musicxml     editable notation (MuseScore, Finale, Sibelius)
    ├── score.mscz         MuseScore project
    ├── score.mid          performance MIDI
    ├── preview.jpg        cover image
    ├── pages/page-1.png … rendered page images
    └── README.txt         what this is, and the licensing notes
"""

from __future__ import annotations

import re
import shutil
import zipfile
from pathlib import Path

from .errors import ScoreForgeError

README_TEMPLATE = """ScoreForge - {title}
{underline}

This archive was produced by ScoreForge, an open-source tool that turns a
piano recording into sheet music.

  score.pdf       engraved sheet music (print / read)
  score.musicxml  editable notation - open it in MuseScore 4
  score.mscz      MuseScore 4 project file
  score.mid       MIDI of the transcription
  preview.jpg     cover image
  pages/          PNG images of every page of the score

Source video
------------
  {source}

Licensing
---------
  The transcription code is MIT licensed.
  The notation model (DuET) and the audio frontend (Transkun) are
  distributed under non-commercial terms (CC BY-NC 4.0 / MIT).
  This output is therefore for personal, non-commercial use only.

  This is a machine-generated first draft. Please review it: note
  durations, hand assignment and pedalling are inferred from the audio
  and are often imperfect.
"""


def slugify(title: str, *, max_length: int = 60) -> str:
    """A filesystem- and URL-safe version of a title."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", title).strip("-").lower()
    slug = re.sub(r"-{2,}", "-", slug)
    return (slug[:max_length].strip("-")) or "score"


def make_preview(
    pages: list[Path],
    destination: Path,
    *,
    size: tuple[int, int] = (1200, 1200),
) -> Path | None:
    """Build a JPEG cover from the first rendered page."""
    if not pages:
        return None
    try:
        from PIL import Image
    except ImportError:  # Pillow is optional
        return None
    Image.MAX_IMAGE_PIXELS = None
    try:
        with Image.open(pages[0]) as img:
            img = img.convert("RGB")
            img.thumbnail(size)
            destination.parent.mkdir(parents=True, exist_ok=True)
            img.save(destination, "JPEG", quality=88)
    except Exception:
        return None
    return destination


#: Page images are rendered at print resolution; that is far more than a
#: screen needs. We downscale them to keep the archive a sane size.
PAGE_MAX_WIDTH = 1700


def make_page_images(pages: list[Path], out_dir: Path) -> list[Path]:
    """Downscale the rendered pages so the archive stays small."""
    if not pages:
        return []
    try:
        from PIL import Image
    except ImportError:
        return list(pages)

    Image.MAX_IMAGE_PIXELS = None
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[Path] = []
    for index, page in enumerate(pages, start=1):
        target = out_dir / f"page-{index}.png"
        try:
            with Image.open(page) as img:
                img = img.convert("RGB")
                if img.width > PAGE_MAX_WIDTH:
                    height = round(img.height * PAGE_MAX_WIDTH / img.width)
                    img = img.resize((PAGE_MAX_WIDTH, height), Image.LANCZOS)
                img.save(target, "PNG", optimize=True)
            results.append(target)
        except Exception:
            results.append(page)
    return results


def build_package(
    score_dir: Path,
    out_dir: Path,
    *,
    title: str,
    source: str,
    pages: list[Path] | None = None,
) -> Path:
    """Zip up the artifacts of a run. Returns the path of the archive."""
    score_dir = Path(score_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pages = pages or sorted(score_dir.glob("page-*.png"))

    archive = out_dir / f"scoreforge-{slugify(title)}.zip"
    readme = out_dir / "README.txt"
    readme.write_text(
        README_TEMPLATE.format(
            title=title, underline="=" * len(title), source=source
        ),
        encoding="utf-8",
    )

    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(readme, "README.txt")
        for name in ("score.pdf", "score.musicxml", "score.mscz", "score.mid"):
            path = score_dir / name
            if path.exists():
                zf.write(path, name)

        page_names = []
        scaled = make_page_images(pages, out_dir / "pages")
        for index, page in enumerate(scaled, start=1):
            arcname = f"pages/page-{index}.png"
            zf.write(page, arcname)
            page_names.append(arcname)

        preview = make_preview(pages, out_dir / "preview.jpg")
        if preview and preview.exists():
            zf.write(preview, "preview.jpg")

        # A small manifest, handy for scripts and for debugging.
        import json

        manifest = {
            "title": title,
            "source": source,
            "pages": len(page_names),
            "files": sorted(zf.namelist()),
        }
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
    return archive


def cleanup_package(out_dir: Path) -> None:
    for name in ("preview.jpg", "README.txt"):
        (Path(out_dir) / name).unlink(missing_ok=True)


__all__ = ["build_package", "slugify", "make_preview", "cleanup_package",
           "ScoreForgeError", "shutil"]
