"""Small local-disk helpers: atomic writes (temp file, fsync, rename) and tolerant reads."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def atomic_write_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: Path, obj: Any, mode: int = 0o600) -> None:
    atomic_write_bytes(path, json.dumps(obj, separators=(",", ":"), default=str).encode(), mode)


def read_json(path: Path) -> Any:
    """Parsed JSON, or None if the file is missing or unreadable (logged)."""
    try:
        return json.loads(path.read_bytes())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        log.warning("ignoring unreadable cache file %s (%s)", path.name, type(e).__name__)
        return None
