"""Benchmark Mojo-backed maxminddb against upstream maxminddb."""

from __future__ import annotations

import gc
import importlib.util
import ipaddress
import math
import os
import pathlib
import platform
import site
import sys
import time

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

import maxminddb as mojo_maxminddb  # noqa: E402


def load_upstream():
    path = next(
        (
            pathlib.Path(directory) / "maxminddb" / "__init__.py"
            for directory in site.getsitepackages()
            if (pathlib.Path(directory) / "maxminddb" / "__init__.py").exists()
        ),
        None,
    )
    if path is None:
        raise RuntimeError("upstream maxminddb is not installed")
    saved = {
        name: module
        for name, module in sys.modules.items()
        if name == "maxminddb" or name.startswith("maxminddb.")
    }
    for name in saved:
        del sys.modules[name]
    try:
        spec = importlib.util.spec_from_file_location(
            "maxminddb", path, submodule_search_locations=[str(path.parent)]
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    finally:
        for name in list(sys.modules):
            if name == "maxminddb" or name.startswith("maxminddb."):
                del sys.modules[name]
        sys.modules.update(saved)
    return module


def best_time(function, repeat: int = 3) -> float:
    best = math.inf
    for _ in range(repeat):
        gc.collect()
        start = time.perf_counter()
        result = function()
        elapsed = time.perf_counter() - start
        if result is None:
            raise AssertionError("benchmark function unexpectedly returned None")
        best = min(best, elapsed)
    return best


def machine() -> str:
    cpu = platform.processor()
    if not cpu or cpu.lower() in {"x86_64", "amd64"}:
        try:
            for line in pathlib.Path("/proc/cpuinfo").read_text().splitlines():
                if line.startswith("model name"):
                    cpu = line.split(":", 1)[1].strip()
                    break
        except OSError:
            cpu = "unknown CPU"
    return f"{cpu}; {platform.system()} {platform.release()}; Python {platform.python_version()}"


def main() -> None:
    upstream = load_upstream()
    database = ROOT / "tests" / "data" / "GeoIP2-City-Test.mmdb"
    ours = mojo_maxminddb.open_database(database, mojo_maxminddb.MODE_MMAP)
    reference = upstream.open_database(database, upstream.MODE_MMAP)

    rng = np.random.default_rng(20260730)
    misses = [
        ipaddress.IPv4Address(int(value))
        for value in rng.integers(0, 2**32, size=50_000, dtype=np.uint32)
    ]
    known_values = [
        "2.125.160.216",
        "67.43.156.1",
        "81.2.69.142",
        "175.16.199.1",
        "216.160.83.56",
        "2001:218::1",
        "2001:220::abcd",
    ]
    hits = [
        ipaddress.ip_address(known_values[index % len(known_values)])
        for index in range(10_000)
    ]

    cases = [
        (
            "scalar get, 50k miss-heavy",
            lambda: [ours.get(address) for address in misses],
            lambda: [reference.get(address) for address in misses],
        ),
        (
            "batch get_many, 50k miss-heavy",
            lambda: ours.get_many(misses),
            lambda: [reference.get(address) for address in misses],
        ),
        (
            "scalar get, 10k GeoIP2 hits",
            lambda: [ours.get(address) for address in hits],
            lambda: [reference.get(address) for address in hits],
        ),
        (
            "batch get_many, 10k GeoIP2 hits",
            lambda: ours.get_many(hits),
            lambda: [reference.get(address) for address in hits],
        ),
        (
            "batch get+prefix, 50k miss-heavy",
            lambda: ours.get_with_prefix_len_many(misses),
            lambda: [reference.get_with_prefix_len(address) for address in misses],
        ),
    ]

    print(f"Machine: {machine()}")
    print()
    print("| case | mojo-maxminddb | upstream maxminddb | speedup |")
    print("| --- | ---: | ---: | ---: |")
    for name, mojo_function, upstream_function in cases:
        expected = upstream_function()
        actual = mojo_function()
        if actual != expected:
            raise AssertionError(f"benchmark parity failed for {name}")
        mojo_seconds = best_time(mojo_function)
        upstream_seconds = best_time(upstream_function)
        ratio = upstream_seconds / mojo_seconds
        print(
            f"| {name} | {mojo_seconds * 1000:.2f} ms | "
            f"{upstream_seconds * 1000:.2f} ms | {ratio:.2f}x |"
        )

    ours.close()
    reference.close()


if __name__ == "__main__":
    main()
