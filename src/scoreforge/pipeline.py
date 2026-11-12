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
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .errors import (
    DownloadError,
    EngravingError,
    InvalidSourceError,
    MuseScoreUnusableError,
    SourceTooLongError,
    SourceUnavailableError,
    TimeoutError_,
    TranscriptionError,
)
from .logging_setup import get_logger
from .musescore import find_musescore, probe_musescore

log = get_logger("pipeline")

AUDIO_SR = 44100
DEFAULT_MODEL = "Wayne42/DuET"

#: Videos longer than this are rejected (seconds).
MAX_DURATION_SECONDS = 30 * 60

#: Hard ceiling for a single audio download (bytes). A 30 minute stream is far
#: below this; the limit only exists so a hostile source cannot fill the disk.
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024

#: Wall-clock ceilings for the external tools (seconds).
DOWNLOAD_TIMEOUT = 20 * 60
FFMPEG_TIMEOUT = 5 * 60

#: Total time a single job may take before the pipeline gives up (seconds).
MAX_JOB_SECONDS = 60 * 60

#: A YouTube video id, as reported by yt-dlp metadata.
VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")

#: yt-dlp options for every metadata call: no config file, no shelling out,
#: no network retries beyond a small budget.
YTDLP_OPTIONS = {
    "quiet": True,
    "no_warnings": True,
    "ignoreconfig": True,
    "noplaylist": True,
    "socket_timeout": 20,
    "retries": 1,
}

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


#: The only source formats accepted by the public service.  Keeping this
#: intentionally narrow prevents arbitrary URL fetching (SSRF) and avoids
#: accepting alternate YouTube surfaces such as playlists and Shorts.
YOUTUBE_SOURCE_RE = re.compile(
    r"https://(?:youtu\.be/[A-Za-z0-9_-]{11}"
    r"|www\.youtube\.com/watch\?v=[A-Za-z0-9_-]{11})"
)


def validate_source(source: str) -> str:
    """Validate and return an exact, supported YouTube video URL.

    Only these two forms are accepted::

        https://youtu.be/ZOaFiTStPt4
        https://www.youtube.com/watch?v=ZOaFiTStPt4

    Every rejection is logged with the raw input so that abuse attempts are
    visible in the service logs.
    """
    value = source if isinstance(source, str) else ""
    if not YOUTUBE_SOURCE_RE.fullmatch(value):
        # ``%r`` escapes control characters and prevents a submitted URL from
        # forging additional log lines.
        log.warning("rejected invalid source URL: %r", value)
        raise InvalidSourceError(
            "Use a YouTube link in the form "
            "https://youtu.be/VIDEO_ID or "
            "https://www.youtube.com/watch?v=VIDEO_ID."
        )
    return value


def ingest(
    source: str,
    workdir: Path,
    *,
    keep_audio: bool = True,
    start: float | None = None,
    duration: float | None = None,
    allow_local: bool = False,
) -> tuple[Path, str]:
    """Return (wav_path, title) for a URL or (opt-in) a local media file.

    ``allow_local`` is off by default: the public service must never resolve
    a submitted string against the server filesystem.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    if allow_local and source and Path(source).exists():
        return ingest_file(source, workdir, keep_audio=keep_audio,
                           start=start, duration=duration)
    return ingest_url(source, workdir, keep_audio=keep_audio,
                      start=start, duration=duration)


def ingest_url(
    source: str,
    workdir: Path,
    *,
    keep_audio: bool = True,
    start: float | None = None,
    duration: float | None = None,
) -> tuple[Path, str]:
    """Download a validated YouTube link and decode it to mono WAV."""
    workdir.mkdir(parents=True, exist_ok=True)
    import yt_dlp

    url = validate_source(source)
    try:
        with yt_dlp.YoutubeDL(dict(YTDLP_OPTIONS)) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        raise SourceUnavailableError(
            "That video could not be reached.",
            detail=str(exc),
        ) from exc

    title, video_id, total = _inspect_metadata(info)
    log.info("downloading %r (%.1f min)", title, total / 60)
    raw = workdir / f"{video_id}.m4a"
    fmt = "140/bestaudio[ext=m4a]/bestaudio/best"
    try:
        _run(
            [
                "yt-dlp",
                "--ignore-config",
                "--no-playlist",
                "--no-progress",
                "--max-filesize", str(MAX_DOWNLOAD_BYTES),
                "-f", fmt,
                "-o", str(raw),
                url,
            ],
            timeout=DOWNLOAD_TIMEOUT,
        )
    except Exception as exc:
        raise DownloadError(
            "The audio could not be downloaded.", detail=str(exc)
        ) from exc

    wav = _to_wav(
        raw, workdir, keep_audio=keep_audio, start=start, duration=duration
    )
    return wav, title


def ingest_file(
    source: str,
    workdir: Path,
    *,
    keep_audio: bool = True,
    start: float | None = None,
    duration: float | None = None,
) -> tuple[Path, str]:
    """Decode a local media file (CLI only - never reachable from the web)."""
    raw = Path(source)
    if not raw.exists():
        raise SourceUnavailableError("That file does not exist.", detail=str(raw))
    return _to_wav(raw, workdir, keep_audio=keep_audio, start=start,
                   duration=duration), raw.stem


def _inspect_metadata(info: dict) -> tuple[str, str, float]:
    """Validate yt-dlp metadata, returning (title, video_id, duration)."""
    if info.get("_type") == "playlist":
        raise InvalidSourceError("Please paste a link to a single video.")
    if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
        raise InvalidSourceError("Live streams cannot be transcribed.")

    video_id = str(info.get("id") or "")
    if not VIDEO_ID_RE.fullmatch(video_id):
        # The id becomes a filename; anything else could escape the workdir.
        raise InvalidSourceError("That video has an unexpected identifier.")

    title = (info.get("title") or "untitled").strip()
    total = info.get("duration")
    if not isinstance(total, (int, float)) or total <= 0:
        # A missing duration usually means a live stream or a premiere, and
        # would otherwise download until the timeout fires.
        raise InvalidSourceError("That video has no known length.")
    if total > MAX_DURATION_SECONDS:
        raise SourceTooLongError(
            "That video is too long.",
            detail=f"{int(total) // 60} min (limit {MAX_DURATION_SECONDS // 60} min)",
        )
    return title, video_id, float(total)


def _to_wav(
    raw: Path,
    workdir: Path,
    *,
    keep_audio: bool,
    start: float | None,
    duration: float | None,
) -> Path:
    """Transcode ``raw`` to 44.1 kHz mono PCM WAV inside ``workdir``."""
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
            _run(cmd, timeout=FFMPEG_TIMEOUT)
        except Exception as exc:
            raise DownloadError(
                "The audio could not be converted.", detail=str(exc)
            ) from exc

    if not keep_audio and raw.exists() and raw.suffix != ".wav":
        raw.unlink(missing_ok=True)

    return wav


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
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr or ""
        if "permission denied" in stderr.lower():
            raise MuseScoreUnusableError(
                "MuseScore 4 is installed but cannot be executed.",
                detail=f"{musescore}: permission denied",
            ) from exc
        raise EngravingError(
            "MuseScore could not render the score.", detail=str(exc)
        ) from exc
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
    allow_local: bool = False,
    max_seconds: int = MAX_JOB_SECONDS,
    progress: ProgressFn | None = None,
) -> dict:
    """Run the whole pipeline and return a report dictionary.

    ``allow_local`` opts into reading a local media file instead of a URL; it
    is reserved for the CLI so that a public request can never name a path.
    """
    out_root.mkdir(parents=True, exist_ok=True)
    workdir = out_root / "work"
    scoredir = out_root / "score"
    stages: list[StageResult] = []

    deadline = time.monotonic() + max_seconds

    def notify(stage: str, pct: int) -> None:
        if time.monotonic() > deadline:
            raise TimeoutError_(
                "This job took too long and was stopped.",
                detail=f"limit {max_seconds // 60} min",
            )
        if progress:
            progress(stage, pct)

    notify("ingest", 5)
    wav, title = ingest(
        source,
        workdir,
        keep_audio=keep_audio,
        start=start,
        duration=duration,
        allow_local=allow_local,
    )
    stages.append(StageResult("ingest", True, f"{title} -> {wav.name}", [wav]))

    notify("transcribe", 20)
    musicxml = transcribe(
        wav, scoredir, device=device, model=model, title=title
    )
    stages.append(StageResult("transcribe", True, "MusicXML", [musicxml]))

    notify("engrave", 70)
    ms = find_musescore(musescore)
    # A present-but-unreadable install fails every render identically; check
    # once, up front, so the job reports a fixable cause instead of a
    # CalledProcessError from deep inside the engraving stage.
    probe_musescore(ms)
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
