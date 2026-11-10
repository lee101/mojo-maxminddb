"""A/B two builds of the IPv4 batch kernel at the sizes `bench.py` uses.

    pixi run python bench/_kernel_ab.py OLD.so NEW.so

The port's ctypes wrapper caches one library handle per process, so each
library is measured in its own subprocess. Reported time is the kernel call
alone -- no record decode and no result list -- best of seven, on the same
fixture and address arrays `bench/bench.py` builds.
"""

from __future__ import annotations

import ctypes
import ipaddress
import json
import pathlib
import subprocess
import sys
import time

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA = ROOT / "tests" / "data" / "GeoIP2-City-Test.mmdb"
CASES = ("misses-50k", "hits-10k")


def addresses() -> dict[str, list[int]]:
    rng = np.random.default_rng(20260730)
    misses = [
        int(value) for value in rng.integers(0, 2**32, size=50_000, dtype=np.uint32)
    ]
    known = [
        "2.125.160.216",
        "67.43.156.1",
        "81.2.69.142",
        "175.16.199.1",
        "216.160.83.56",
        "2001:218::1",
        "2001:220::abcd",
    ]
    hits = [
        int(ipaddress.ip_address(known[index % len(known)])) & 0xFFFFFFFF
        for index in range(10_000)
    ]
    return {"misses-50k": misses, "hits-10k": hits}


def measure(lib_path: str) -> dict[str, float]:
    import maxminddb

    all_addresses = addresses()
    library = ctypes.CDLL(lib_path)
    find = library.mmd_find_ipv4_many
    find.argtypes = [ctypes.c_int64] * 8
    find.restype = None
    results: dict[str, float] = {}
    with maxminddb.open_database(DATA) as reader:
        for kind in CASES:
            values = np.array(all_addresses[kind], dtype=np.int64)
            out = np.empty(values.size, dtype=np.int64)
            args = (
                reader._address,
                reader._buffer_size,
                values.ctypes.data,
                values.size,
                reader.metadata().node_count,
                reader.metadata().record_size,
                reader._ipv4_start,
                out.ctypes.data,
            )
            find(*args)
            best = float("inf")
            for _ in range(7):
                start = time.perf_counter()
                find(*args)
                best = min(best, time.perf_counter() - start)
            results[kind] = best
    return results


def main() -> None:
    if sys.argv[1:2] == ["--one"]:
        print(json.dumps(measure(sys.argv[2])))
        return
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    samples = []
    for path in sys.argv[1:3]:
        out = subprocess.run(
            [sys.executable, __file__, "--one", path],
            capture_output=True,
            text=True,
            check=True,
        )
        samples.append(json.loads(out.stdout))
    for kind in CASES:
        old, new = samples[0][kind], samples[1][kind]
        print(
            f"| {kind} kernel | {old * 1e3:.2f} ms | {new * 1e3:.2f} ms | "
            f"{old / new:.2f}x |"
        )


if __name__ == "__main__":
    main()
