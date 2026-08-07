"""Filesystem helpers.

``Path.write_text`` translates ``\n`` to ``\r\n`` on Windows. That silently
broke Polygraph's own integrity claim: the incident document's sha-256 is
computed over the in-memory markdown, but the file written next to it hashed
differently on Windows, so "verify the catalog copy against examples/ byte for
byte" was false on the exact platform the demo runs on.

Every Polygraph artifact is written through here, with LF pinned.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def write_text_lf(path: Path, text: str) -> None:
    """Write UTF-8 with LF endings on every platform."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def write_json_lf(path: Path, obj: Any, indent: int = 2) -> None:
    write_text_lf(Path(path), json.dumps(obj, indent=indent, default=str) + "\n")
