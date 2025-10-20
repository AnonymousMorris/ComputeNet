"""Lightweight temporary directory context manager reused across components."""

from __future__ import annotations

import contextlib
import shutil
import tempfile
from pathlib import Path
from typing import Optional


class TemporaryDirectory(contextlib.AbstractContextManager[Path]):
    """Context manager that exposes a temporary directory as a Path object."""

    def __init__(self, *, prefix: str = "computenet_", dir: Optional[str | Path] = None) -> None:
        self._prefix = prefix
        self._base_dir = Path(dir) if dir is not None else None
        self._path: Optional[Path] = None

    def __enter__(self) -> Path:
        base_dir = str(self._base_dir) if self._base_dir is not None else None
        directory = tempfile.mkdtemp(prefix=self._prefix, dir=base_dir)
        self._path = Path(directory)
        return self._path

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._path is not None:
            shutil.rmtree(self._path, ignore_errors=True)
            self._path = None

    @property
    def path(self) -> Path:
        if self._path is None:
            raise RuntimeError("TemporaryDirectory is not active; enter context first")
        return self._path

