"""A/B one built library through the public Python API at bench.py's sizes.

    pixi run python bench/_api_ab.py OLD.so NEW.so

The port caches one ctypes handle per process, so each library runs in its own
subprocess (selected with MOJO_MAXMINDDB_LIB). Reports the best of five whole
calls of the exact public methods `bench/bench.py` times.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA = ROOT / "tests" / "data" / "GeoIP2-City-Test.mmdb"
CASES = ("misses-50k", "hits-10k")


def addresses() -> dict[str, list]:
    import ipaddress

    import numpy as np

    rng = np.random.default_rng(20260730)
    misses = [
        ipaddress.IPv4Address(int(value))
        for value in rng.integers(0, 2**32, size=50_000, dtype=np.uint32)
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
        ipaddress.ip_address(known[index % len(known)]) for index in range(10_000)
    ]
    return {"misses-50k": misses, "hits-10k": hits}


def measure() -> dict[str, float]:
    import gc

    import maxminddb

    all_addresses = addresses()
    results: dict[str, float] = {}
    with maxminddb.open_database(DATA) as reader:
        for kind in CASES:
            values = all_addresses[kind]
            reader.get_many(values)
            best = float("inf")
            for _ in range(5):
                gc.collect()
                start = time.perf_counter()
                reader.get_many(values)
                best = min(best, time.perf_counter() - start)
            results[kind] = best
    return results


def main() -> None:
    if sys.argv[1:2] == ["--one"]:
        print(json.dumps(measure()))
        return
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    samples = []
    for path in sys.argv[1:3]:
        environment = dict(os.environ, MOJO_MAXMINDDB_LIB=str(pathlib.Path(path).resolve()))
        out = subprocess.run(
            [sys.executable, __file__, "--one"],
            capture_output=True,
            text=True,
            check=True,
            env=environment,
        )
        samples.append(json.loads(out.stdout))
    for kind in CASES:
        old, new = samples[0][kind], samples[1][kind]
        print(
            f"| get_many {kind} | {old * 1e3:.2f} ms | {new * 1e3:.2f} ms | "
            f"{old / new:.2f}x |"
        )


if __name__ == "__main__":
    main()
