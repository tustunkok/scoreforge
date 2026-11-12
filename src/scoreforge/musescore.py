"""Locating the MuseScore 4 executable.

MuseScore is not redistributable in our images (GPL), so it is installed
separately: on Linux the official AppImage is mounted at runtime, on Windows
the official installer is used. We only need to find the binary.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

from .errors import MuseScoreNotFoundError, MuseScoreUnusableError

ENV_VAR = "SCOREFORGE_MUSESCORE"

#: Places a Linux image is likely to keep it.
LINUX_CANDIDATES = (
    "/opt/musescore/musescore",
    "/opt/MuseScore-4.7.6.261022093-x86_64.AppImage",
    "/opt/musescore.AppImage",
    "/usr/local/bin/mscore",
    "/usr/bin/mscore",
)

#: Places a Windows install puts it.
WINDOWS_CANDIDATES = (
    r"C:\Program Files\MuseScore 4\bin\MuseScore4.exe",
    r"C:\Program Files\MuseScore 4\bin\mscore.exe",
    r"C:\Program Files (x86)\MuseScore 4\bin\MuseScore4.exe",
)

NAMES = ("MuseScore4", "mscore", "mscore4", "musescore")


def _executable(path: Path) -> bool:
    if not path.is_file():
        return False
    if os.name == "nt":
        return True
    return bool(path.stat().st_mode & stat.S_IXUSR)


def find_musescore(explicit: str | None = None) -> Path:
    """Return the path to a MuseScore 4 binary.

    Search order: explicit argument, ``SCOREFORGE_MUSESCORE``, the usual
    install locations, then ``PATH``.
    """
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env = os.environ.get(ENV_VAR)
    if env:
        candidates.append(Path(env))
    candidates.extend(Path(p) for p in LINUX_CANDIDATES)
    candidates.extend(Path(p) for p in WINDOWS_CANDIDATES)
    for candidate in candidates:
        if _executable(candidate):
            return candidate
    for name in NAMES:
        found = shutil.which(name)
        if found:
            return Path(found)
    raise MuseScoreNotFoundError(
        "MuseScore 4 was not found.",
        detail=(
            f"set {ENV_VAR} to the executable, or install MuseScore 4 "
            "(https://musescore.org/download)"
        ),
    )


def _hint(stderr: str) -> str:
    """Turn MuseScore's stderr into something a person can act on."""
    text = stderr.strip()
    lowered = text.lower()
    for needle, hint in (
        (
            "permission denied",
            "the executable or its directory is not readable by this user",
        ),
        ("no such file or directory", "the executable path does not exist"),
        ("cannot open display", "no display is available; run it through xvfb-run"),
        ("libqt6", "MuseScore's Qt libraries are missing or not on LD_LIBRARY_PATH"),
        (
            "error while loading shared libraries",
            "a shared library MuseScore needs is missing",
        ),
    ):
        if needle in lowered:
            return hint
    return (text or "no output").splitlines()[-1][:200]


def probe_musescore(path: Path, *, timeout: float = 120.0) -> str:
    """Run ``path --version`` and return the reported version.

    Finding a file is not enough: an extracted AppImage can be complete and
    executable yet unreadable by the service user (a directory with mode 700
    blocks traversal). Only executing it proves the install works, so both
    ``doctor`` and the pipeline call this before promising a score.
    """
    try:
        result = subprocess.run(
            [str(path), "--version"],
            check=False,
            text=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except PermissionError as exc:
        raise MuseScoreUnusableError(
            "MuseScore 4 is installed but cannot be executed.",
            detail=(
                f"{path}: permission denied - the file or its directory "
                "is not readable by this user"
            ),
        ) from exc
    except OSError as exc:
        raise MuseScoreUnusableError(
            "MuseScore 4 is installed but cannot be executed.",
            detail=f"{path}: {exc}",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise MuseScoreUnusableError(
            "MuseScore 4 did not answer in time.",
            detail=f"{path} --version took longer than {timeout:.0f}s",
        ) from exc

    if result.returncode != 0:
        raise MuseScoreUnusableError(
            "MuseScore 4 is installed but cannot be executed.",
            detail=f"{path} --version exited {result.returncode}: {_hint(result.stderr)}",
        )

    output = f"{result.stdout}\n{result.stderr}"
    for line in output.splitlines():
        if "musescore" in line.lower() and any(ch.isdigit() for ch in line):
            return line.strip()
    return (result.stdout.strip().splitlines() or ["unknown"])[-1][:200]
