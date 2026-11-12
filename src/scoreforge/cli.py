"""Command line interface for ScoreForge.

    scoreforge convert <url> [-o out]     # one-off conversion
    scoreforge serve [--host --port]       # the web application
    scoreforge doctor                      # check the environment
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from . import __version__
from .errors import MuseScoreUnusableError
from .logging_setup import get_logger, setup_logging

log = get_logger("cli")


def _cmd_convert(args: argparse.Namespace) -> int:
    from .errors import ScoreForgeError
    from .package import build_package
    from .pipeline import run_pipeline

    log.info("converting %s into %s", args.url, args.out)
    out = Path(args.out)
    try:
        meta = run_pipeline(
            args.url,
            out,
            device=args.device,
            musescore=args.musescore,
            keep_audio=args.keep_audio,
            start=args.start,
            duration=args.duration,
            skip_png=args.no_png,
            model=args.model,
            # The CLI is the only caller allowed to name a local file.
            allow_local=True,
            progress=(lambda s, p: print(f"[{p:3d}%] {s}", flush=True)),
        )
    except ScoreForgeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    archive = build_package(
        Path(meta["score_dir"]),
        out,
        title=meta["title"],
        source=meta["source"],
        pages=[Path(p) for p in meta.get("pages", [])],
    )
    meta["zip"] = str(archive)
    print(json.dumps(meta, indent=2))
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .web.app import create_app

    setup_logging()
    app = create_app()
    log.info("starting the web server on %s:%d", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    """Report whether everything the pipeline needs is available."""
    ok = True
    print(f"ScoreForge {__version__} environment check")
    print("-" * 40)

    for tool in ("ffmpeg", "yt-dlp"):
        found = shutil.which(tool)
        print(f"{tool:12} {'OK  ' + found if found else 'MISSING'}")
        ok = ok and bool(found)

    # Finding the binary proves nothing - a root-owned AppImage extraction is
    # invisible to the service user - so execute it as well.
    try:
        from .musescore import find_musescore, probe_musescore

        path = find_musescore(None)
        version = probe_musescore(path)
        print(f"{'musescore':12} OK  {path} ({version})")
    except MuseScoreUnusableError as exc:
        print(f"{'musescore':12} BROKEN ({exc})")
        ok = False
    except Exception as exc:
        print(f"{'musescore':12} MISSING ({exc})")
        ok = False

    try:
        import torch

        print(f"{'torch':12} OK  {torch.__version__} (cuda: {torch.cuda.is_available()})")
    except Exception as exc:
        print(f"{'torch':12} MISSING ({exc})")
        ok = False

    try:
        from duet.pipeline import DuET  # noqa: F401

        print(f"{'duet':12} OK  installed")
    except Exception as exc:
        print(f"{'duet':12} MISSING ({exc})")
        ok = False

    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="scoreforge", description="Turn a piano recording into sheet music."
    )
    ap.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="print the version and exit",
    )
    ap.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="how much to log (default: INFO, or $SCOREFORGE_LOG_LEVEL)",
    )
    sub = ap.add_subparsers(dest="command", required=True)

    conv = sub.add_parser("convert", help="convert one YouTube link")
    conv.add_argument("url", help="YouTube URL")
    conv.add_argument("-o", "--out", default="out", help="output directory")
    conv.add_argument("--device", default="cpu", choices=["cpu", "cuda"])
    conv.add_argument("--musescore", default=None)
    conv.add_argument("--model", default=None)
    conv.add_argument("--start", type=float, default=None)
    conv.add_argument("--duration", type=float, default=None)
    conv.add_argument("--keep-audio", action="store_true", default=False)
    conv.add_argument("--no-png", dest="no_png", action="store_true")
    conv.set_defaults(func=_cmd_convert)

    serve = sub.add_parser("serve", help="run the web application")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--log-level", default="info")
    serve.set_defaults(func=_cmd_serve)

    doctor = sub.add_parser("doctor", help="check the environment")
    doctor.set_defaults(func=_cmd_doctor)

    args = ap.parse_args(argv)
    setup_logging(args.log_level)
    if args.command == "convert" and args.model is None:
        from .pipeline import DEFAULT_MODEL

        args.model = DEFAULT_MODEL
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
