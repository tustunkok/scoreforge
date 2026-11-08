"""YouTube/audio -> performance MIDI -> MusicXML -> PDF sheet music.

Stages
------
1. ingest     : yt-dlp (URL) or a local file -> 44.1 kHz mono WAV
2. transcribe : Transkun V2 (neural semi-CRF) -> performance MIDI
3. score      : DuET (note+frame dual encoder) -> MusicXML + score MIDI
4. engrave    : MuseScore 4 CLI -> PDF, MSCZ and PNG pages

The transcription models are loaded in-process (no subprocesses) so the
model weights are loaded once per worker and reused across jobs.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .errors import (
    DownloadError,
    EngravingError,
    InvalidSourceError,
    SourceTooLongError,
    SourceUnavailableError,
    TranscriptionError,
)
from .logging_setup import get_logger
from .musescore import find_musescore

log = get_logger("pipeline")

AUDIO_SR = 44100
DEFAULT_MODEL = "Wayne42/DuET"

#: Videos longer than this are rejected (seconds).
MAX_DURATION_SECONDS = 30 * 60

ProgressFn = Callable[[str, int], None]


@dataclass
class StageResult:
    name: str
    ok: bool
    detail: str = ""
    outputs: list[Path] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "outputs": [str(p) for p in self.outputs],
        }


# --------------------------------------------------------------------------- #
# model cache
# --------------------------------------------------------------------------- #
_model_lock = threading.Lock()
_duet_model = None
_audio_model = None


def get_duet_model(device: str = "cpu", model: str = DEFAULT_MODEL):
    """Load (and cache) the DuET notation model."""
    global _duet_model
    with _model_lock:
        if _duet_model is None:
            import torch
            from duet.pipeline import DuET

            torch.set_num_threads(max(1, (torch.get_num_threads() or 4)))
            _duet_model = DuET.from_pretrained(model, device=device)
        return _duet_model


def get_audio_model(device: str = "cpu"):
    """Load (and cache) the Transkun audio frontend."""
    global _audio_model
    with _model_lock:
        if _audio_model is None:
            from duet.audio import load_audio_model

            _audio_model = load_audio_model(device)
        return _audio_model


def reset_model_cache() -> None:
    """Drop cached models (used by tests and by the web app on shutdown)."""
    global _duet_model, _audio_model
    with _model_lock:
        _duet_model = None
        _audio_model = None


# --------------------------------------------------------------------------- #
# stage 1: ingest
# --------------------------------------------------------------------------- #
def _run(cmd: list[str], *, timeout: int | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        check=True,
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def validate_source(source: str) -> str:
    """Reject anything that is not a plain YouTube video link.

    Only public videos on youtube.com / youtu.be are accepted, which keeps
    the service away from arbitrary URL fetching (SSRF).
    """
    from urllib.parse import urlparse

    source = (source or "").strip()
    if not source:
        raise InvalidSourceError("No link was provided.")
    if not source.startswith(("http://", "https://")):
        source = "https://" + source
    parsed = urlparse(source)
    host = (parsed.hostname or "").lower()
    if host in {"youtu.be"}:
        if not parsed.path.strip("/"):
            raise InvalidSourceError("That YouTube link has no video id.")
        return source
    if host in {"youtube.com", "m.youtube.com", "music.youtube.com", "www.youtube.com"}:
        if parsed.path.startswith(("/playlist", "/channel", "/@", "/results", "/feed")):
            raise InvalidSourceError(
                "Please paste a link to a single video, not a playlist or channel."
            )
        if "v=" in (parsed.query or "") or parsed.path.startswith("/shorts/"):
            return source
        raise InvalidSourceError("That does not look like a YouTube video link.")
    raise InvalidSourceError(
        "Only YouTube links are supported.",
        detail=f"host was {host or 'unknown'}",
    )


def ingest(
    source: str,
    workdir: Path,
    *,
    keep_audio: bool = True,
    start: float | None = None,
    duration: float | None = None,
) -> tuple[Path, str]:
    """Return (wav_path, title). ``source`` is a URL or a local media file."""
    workdir.mkdir(parents=True, exist_ok=True)

    if Path(source).exists():
        raw = Path(source)
        title = raw.stem
    else:
        import yt_dlp

        url = validate_source(source)
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:
            raise SourceUnavailableError(
                "That video could not be reached.",
                detail=str(exc),
            ) from exc
        if info.get("_type") == "playlist":
            raise InvalidSourceError("Please paste a link to a single video.")
        title = (info.get("title") or "untitled").strip()
        log.info("downloading %r (%.1f min)", title, (info.get("duration") or 0) / 60)
        total = info.get("duration") or 0
        if total and total > MAX_DURATION_SECONDS:
            raise SourceTooLongError(
                "That video is too long.",
                detail=f"{total // 60} min (limit {MAX_DURATION_SECONDS // 60} min)",
            )
        raw = workdir / f"{info.get('id', 'audio')}.m4a"
        fmt = "140/bestaudio[ext=m4a]/bestaudio/best"
        try:
            _run(["yt-dlp", "-f", fmt, "--no-playlist", "-o", str(raw), url])
        except Exception as exc:
            raise DownloadError(
                "The audio could not be downloaded.", detail=str(exc)
            ) from exc

    wav = workdir / f"{raw.stem}.wav"
    if not wav.exists():
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(raw),
            "-ac", "1", "-ar", str(AUDIO_SR),
            "-c:a", "pcm_s16le",
            str(wav),
        ]
        if start is not None:
            cmd[1:1] = ["-ss", str(start)]
        if duration is not None:
            cmd[1:1] = ["-t", str(duration)]
        try:
            _run(cmd)
        except Exception as exc:
            raise DownloadError(
                "The audio could not be converted.", detail=str(exc)
            ) from exc

    if not keep_audio and raw.exists() and raw.suffix != ".wav":
        raw.unlink(missing_ok=True)

    return wav, title


# --------------------------------------------------------------------------- #
# stage 2 + 3: transcription (Transkun -> DuET), in-process
# --------------------------------------------------------------------------- #
def transcribe(
    wav: Path,
    outdir: Path,
    *,
    device: str = "cpu",
    model: str = DEFAULT_MODEL,
    title: str | None = None,
) -> Path:
    """Audio -> MusicXML. Returns the path to the MusicXML file."""
    outdir.parent.mkdir(parents=True, exist_ok=True)
    # DuET refuses to write into an existing directory.
    base = outdir
    n = 1
    while outdir.exists():
        outdir = base.with_name(f"{base.name}_{n}")
        n += 1

    log.info("transcribing %s with %s on %s", wav.name, model, device)
    duet_model = get_duet_model(device=device, model=model)
    audio_model = get_audio_model(device=device)
    try:
        duet_model.transcribe_audio(
            str(wav), str(outdir), progress=None, audio_model=audio_model
        )
    except Exception as exc:
        raise TranscriptionError(
            "The recording could not be transcribed.", detail=str(exc)
        ) from exc

    musicxml = outdir / "score.musicxml"
    if not musicxml.exists():
        raise TranscriptionError(
            "The transcriber produced no MusicXML.", detail=str(outdir)
        )
    if title:
        set_title(musicxml, title)
    return musicxml


# --------------------------------------------------------------------------- #
# stage 4: engraving
# --------------------------------------------------------------------------- #
def engrave(
    musescore: Path,
    musicxml: Path,
    outdir: Path,
    *,
    png: bool = True,
    mscz: bool = True,
) -> tuple[Path | None, Path | None, list[Path]]:
    """Render MusicXML to PDF, MSCZ and (optionally) PNG pages."""
    outdir.mkdir(parents=True, exist_ok=True)
    pdf = outdir / "score.pdf"
    try:
        _run([str(musescore), str(musicxml), "-o", str(pdf)], timeout=600)
    except Exception as exc:
        raise EngravingError(
            "MuseScore could not render the score.", detail=str(exc)
        ) from exc
    if not pdf.exists():
        raise EngravingError("MuseScore produced no PDF.", detail=str(outdir))

    mscz_path: Path | None = None
    if mscz:
        mscz_path = outdir / "score.mscz"
        try:
            _run([str(musescore), str(musicxml), "-o", str(mscz_path)], timeout=600)
        except Exception:
            mscz_path = None

    pages: list[Path] = []
    if png:
        try:
            _run([str(musescore), str(musicxml), "-o", str(outdir / "page.png")],
                 timeout=600)
            pages = sorted(outdir.glob("page-*.png"))
        except Exception:
            pages = []
    return pdf, mscz_path, pages


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def set_title(musicxml: Path, title: str) -> None:
    """Replace DuET's placeholder work title with the real one."""
    text = musicxml.read_text(encoding="utf-8")
    safe = (
        title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    text = text.replace(
        "<work-title>performance</work-title>", f"<work-title>{safe}</work-title>", 1
    )
    musicxml.write_text(text, encoding="utf-8")


def cleanup(path: Path) -> None:
    """Remove a directory tree, ignoring errors."""
    shutil.rmtree(path, ignore_errors=True)


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #
def run_pipeline(
    source: str,
    out_root: Path,
    *,
    device: str = "cpu",
    musescore: str | None = None,
    keep_audio: bool = False,
    start: float | None = None,
    duration: float | None = None,
    skip_png: bool = False,
    model: str = DEFAULT_MODEL,
    progress: ProgressFn | None = None,
) -> dict:
    """Run the whole pipeline and return a report dictionary."""
    out_root.mkdir(parents=True, exist_ok=True)
    workdir = out_root / "work"
    scoredir = out_root / "score"
    stages: list[StageResult] = []

    def notify(stage: str, pct: int) -> None:
        if progress:
            progress(stage, pct)

    notify("ingest", 5)
    wav, title = ingest(
        source, workdir, keep_audio=keep_audio, start=start, duration=duration
    )
    stages.append(StageResult("ingest", True, f"{title} -> {wav.name}", [wav]))

    notify("transcribe", 20)
    musicxml = transcribe(
        wav, scoredir, device=device, model=model, title=title
    )
    stages.append(StageResult("transcribe", True, "MusicXML", [musicxml]))

    notify("engrave", 70)
    ms = find_musescore(musescore)
    pdf, mscz, pages = engrave(ms, musicxml, scoredir, png=not skip_png)
    outputs = [p for p in (pdf, mscz) if p] + pages
    stages.append(StageResult("engrave", True, str(pdf), outputs))

    notify("package", 90)
    meta = {
        "source": source,
        "title": title,
        "device": device,
        "score_dir": str(scoredir),
        "musicxml": str(musicxml),
        "pdf": str(pdf) if pdf else None,
        "mscz": str(mscz) if mscz else None,
        "pages": [str(p) for p in pages],
        "stages": [s.as_dict() for s in stages],
    }
    (out_root / "report.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )
    notify("done", 100)
    return meta
