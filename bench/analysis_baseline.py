"""Plain-Python and numpy baselines (no Mojo library) for bench/analysis.md.

Run with: pixi run python -u bench/analysis_baseline.py
"""

from __future__ import annotations

import gc
import ipaddress
import math
import pathlib
import sys
import time

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from maxminddb.reader import Decoder, Reader  # noqa: E402

DB = ROOT / "tests" / "data" / "GeoIP2-City-Test.mmdb"


def best(function, repeat: int = 5) -> float:
    value = math.inf
    for _ in range(repeat):
        gc.collect()
        start = time.perf_counter()
        result = function()
        if result is None:
            raise AssertionError("returned None")
        value = min(value, time.perf_counter() - start)
    return value


def main() -> None:
    reader = Reader(DB)
    meta = reader.metadata()
    node_count = meta.node_count
    record_size = meta.record_size
    db = reader._buffer
    start4 = reader._ipv4_start
    rng = np.random.default_rng(20260730)
    misses = [int(v) for v in rng.integers(0, 2**32, size=50_000, dtype=np.uint32)]
    known = ["2.125.160.216", "67.43.156.1", "81.2.69.142", "175.16.199.1", "216.160.83.56"]
    hits = [(i, known[i % len(known)]) for i in range(10_000)]

    def py_traverse(value):
        node = start4
        bit = 0
        while bit < 32 and node < node_count:
            off = node * 6 + ((value >> (31 - bit)) & 1) * 3
            node = int.from_bytes(db[off:off + 3], "big")
            bit += 1
        return node, bit

    t = best(lambda: [py_traverse(v) for v in misses])
    print(f"| pure-python trie walk x50k | {t*1e3:.3f} ms | {t*1e6/50_000:.3f} us |", flush=True)
    t = best(lambda: [py_traverse(int(ipaddress.ip_address(s))) for _, s in hits])
    print(f"| pure-python trie walk x10k hits | {t*1e3:.3f} ms | {t*1e6/10_000:.3f} us |", flush=True)

    # decode baseline: decode each hit record with plain python Decoder
    base = meta.search_tree_size + 16
    ptrs = []
    for _, s in hits:
        node, _bit = py_traverse(int(ipaddress.ip_address(s)))
        if node > node_count:
            ptrs.append(node - node_count + meta.search_tree_size)
    print(f"resolved {len(ptrs)} hit pointers", flush=True)
    t = best(lambda: [Decoder(db, base).decode(p) for p in ptrs], repeat=3)
    print(f"| pure-python decode x{len(ptrs)} uncached hits | {t*1e3:.3f} ms | {t*1e6/len(ptrs):.3f} us |", flush=True)

    # numpy: can the trie walk be vectorised at all? measure a vectorised bit extract
    addrs = np.array(misses[:50_000], dtype=np.uint32).astype(np.int64)
    t = best(lambda: [(addrs >> (31 - b)) & 1 for b in range(32)])
    print(f"| numpy bit-extract 32 bits x50k (32 passes) | {t*1e3:.3f} ms | {t*1e6/50_000:.3f} us |", flush=True)
    nodes = np.full(50_000, start4, dtype=np.int64)
    t = best(lambda: np.lexsort((nodes, addrs)))
    print(f"| numpy gather+sort control x50k | {t*1e3:.3f} ms |", flush=True)
    reader.close()
    print(f"node_count={node_count} start4={start4} host_db_bytes={len(db)}", flush=True)


if __name__ == "__main__":
    main()
