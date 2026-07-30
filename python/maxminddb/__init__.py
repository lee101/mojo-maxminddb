"""A Mojo-backed, API-compatible MaxMind DB reader."""

from __future__ import annotations

from .const import (
    MODE_AUTO,
    MODE_FD,
    MODE_FILE,
    MODE_MEMORY,
    MODE_MMAP,
    MODE_MMAP_EXT,
)
from .errors import InvalidDatabaseError
from .reader import Reader

from . import extension

_extension = extension

__all__ = [
    "InvalidDatabaseError",
    "MODE_AUTO",
    "MODE_FD",
    "MODE_FILE",
    "MODE_MEMORY",
    "MODE_MMAP",
    "MODE_MMAP_EXT",
    "Reader",
    "open_database",
]

__version__ = "0.1.0"


def open_database(database, mode: int = MODE_AUTO) -> Reader:
    if mode not in (
        MODE_AUTO,
        MODE_FD,
        MODE_FILE,
        MODE_MEMORY,
        MODE_MMAP,
        MODE_MMAP_EXT,
    ):
        raise ValueError(f"Unsupported open mode: {mode}")
    return Reader(database, mode)
