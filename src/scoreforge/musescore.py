"""Locating the MuseScore 4 executable.

MuseScore is not redistributable in our images (GPL), so it is installed
separately: on Linux the official AppImage is mounted at runtime, on Windows
the official installer is used. We only need to find the binary.
"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

from .errors import MuseScoreNotFoundError

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
