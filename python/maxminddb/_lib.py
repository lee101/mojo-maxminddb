"""Load the compiled Mojo MMDB search-tree library."""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LIB = os.environ.get("MOJO_MAXMINDDB_LIB") or os.path.join(
    ROOT, "dist", "libmojo-maxminddb.so"
)
SRC = os.path.join(ROOT, "src", "maxminddb.mojo")

I = ctypes.c_int64
U = ctypes.c_uint64

_SIGNATURES = {
    "mmd_find": ([I] * 10, None),
    "mmd_find_value": ([I, I, U, U, I, I, I, I], I),
    "mmd_find_ipv4_value": ([I, U, I, I, I], I),
    "mmd_find_ipv4_many": ([I] * 8, None),
    "mmd_find_many": ([I] * 11, None),
}


class BuildError(RuntimeError):
    pass


def build(force: bool = False) -> str:
    if os.environ.get("MOJO_MAXMINDDB_LIB"):
        if os.path.exists(LIB):
            return LIB
        raise BuildError(f"MOJO_MAXMINDDB_LIB does not exist: {LIB}")
    if (
        not force
        and os.path.exists(LIB)
        and os.path.getmtime(LIB) >= os.path.getmtime(SRC)
    ):
        return LIB
    if shutil.which("mojo") is None:
        raise BuildError("mojo was not found; run `pixi run build` first")
    process = subprocess.run(
        ["bash", os.path.join(ROOT, "build", "build.sh")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if process.returncode or not os.path.exists(LIB):
        raise BuildError((process.stderr or process.stdout).strip()[:4000])
    return LIB


_library: ctypes.CDLL | None = None


def lib() -> ctypes.CDLL:
    global _library
    if _library is None:
        _library = ctypes.CDLL(build())
        for name, (argtypes, restype) in _SIGNATURES.items():
            function = getattr(_library, name)
            function.argtypes = argtypes
            function.restype = restype
    return _library
