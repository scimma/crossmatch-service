"""File formats shared by replay samples and snapshots, kept free of Django imports.

The comparison step reads snapshots anywhere (including a laptop without the
app's database settings), so it must not import the snapshot builder.
"""

import hashlib
import json
from pathlib import Path

SNAPSHOT_KIND = "crossmatch-replay-snapshot"
SNAPSHOT_FORMAT_VERSION = 1


def write_json(data: dict, path: Path | str) -> None:
    """Write a replay file as JSON; NaN is rejected so the file stays valid JSON."""
    Path(path).write_text(json.dumps(data, indent=1, allow_nan=False) + "\n")


def content_digest(value) -> str:
    """SHA-256 of a value's canonical JSON form (sorted keys, no whitespace)."""
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()
