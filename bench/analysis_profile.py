"""Instrumented micro-benchmarks for bench/analysis.md (ANALYZE phase).

Each measurement prints best-of-N wall time for one isolated stage so the
hotspot table can separate Python/ctypes glue from the Mojo kernel.
Run with: pixi run python -u bench/analysis_profile.py
"""

from __future__ import annotations

import gc
import importlib.util
import ipaddress
import math
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
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    finally:
        for name in list(sys.modules):
            if name == "maxminddb" or name.startswith("maxminddb."):
                del sys.modules[name]
        sys.modules.update(saved)
    return module


def best(function, repeat: int = 5) -> float:
    value = math.inf
    for _ in range(repeat):
        gc.collect()
        start = time.perf_counter()
        function()
        value = min(value, time.perf_counter() - start)
    return value


def bestq(function, repeat: int = 5, n: int = 1) -> float:
    """Best per-call time over `n` calls inside one timed region."""
    value = math.inf
    for _ in range(repeat):
        gc.collect()
        start = time.perf_counter()
        for _ in range(n):
            function()
        value = min(value, (time.perf_counter() - start) / n)
    return value


def row(label, seconds, count=None):
    per = f"{seconds * 1e6 / count:.3f}" if count else f"{seconds * 1e6:.3f}"
    print(f"| {label} | {count or 1} | {seconds * 1e3:.3f} | {per} |", flush=True)


def main() -> None:
    print(f"Machine: {platform.processor() or platform.machine()}; {platform.system()} "
          f"{platform.release()}; Python {platform.python_version()}", flush=True)
    upstream = load_upstream()
    database = ROOT / "tests" / "data" / "GeoIP2-City-Test.mmdb"
    ours = mojo_maxminddb.open_database(database, mojo_maxminddb.MODE_MMAP)
    ref = upstream.open_database(database, upstream.MODE_MMAP)

    rng = np.random.default_rng(20260730)
    misses = [ipaddress.IPv4Address(int(v)) for v in rng.integers(0, 2**32, size=50_000, dtype=np.uint32)]
    known = ["2.125.160.216", "67.43.156.1", "81.2.69.142", "175.16.199.1",
             "216.160.83.56", "2001:218::1", "2001:220::abcd"]
    # distinct hit addresses are needed so per-call decode cannot be served by cache
    hit4 = [ipaddress.ip_address(a) for a in known if ":" not in a]
    hits = [ipaddress.ip_address(known[i % len(known)]) for i in range(10_000)]
    v6 = [ipaddress.ip_address(a) for a in known if ":" in a]

    print("\n## stage isolation (mojo port), seconds and us/call", flush=True)
    # 1. address coercion only (ipaddress object path and str path)
    row("coerce IPv4Address x50k", best(lambda: [ours._coerce_address(a) for a in misses]), 50_000)
    row("coerce str x10k (ipaddress.ip_address)", best(lambda: [ours._coerce_address(s) for s in known * 1429]), 10_003)
    # 2. traversal only (scalar integer ABI)
    row("_find_integer x50k (kernel only, IPv4)", best(lambda: [ours._find_integer(a) for a in misses]), 50_000)
    row("_find_integer x10k hits", best(lambda: [ours._find_integer(a) for a in hits]), 10_000)
    row("_find_integer x7 IPv6", best(lambda: [ours._find_integer(a) for a in v6]), len(v6))
    # 3. full scalar lookup (glue + kernel + decode)
    row("get x50k miss-heavy", best(lambda: [ours.get(a) for a in misses]), 50_000)
    row("get x10k hits (+decode+cache)", best(lambda: [ours.get(a) for a in hits]), 10_000)
    # 4. decode only: force decode of fresh pointers, bypassing cache
    def decode_only():
        out = []
        for a in hits:
            p, _ = ours._find_integer(a)
            if p:
                out.append(ours._decoder.decode(p - ours._metadata.node_count
                                                + ours._metadata.search_tree_size)[0])
        return out
    row("decode (uncached) x10k hits", best(decode_only), 10_000)

    def clone_only():
        out = []
        for a in hits:
            p, _ = ours._find_integer(a)
            if p:
                out.append(mojo_maxminddb.reader._clone_record(ours._decode_cache[p]))
        return out
    cache_hits = sum(1 for a in hits if (ours._find_integer(a)[0]) in ours._decode_cache)
    row(f"_clone_record x{cache_hits} warmed", best(clone_only), cache_hits)

    # 5. batch paths
    row("get_many x50k miss (incl 50k decode lookups)", best(lambda: ours.get_many(misses)), 50_000)
    row("get_with_prefix_len_many x50k miss", best(lambda: ours.get_with_prefix_len_many(misses)), 50_000)
    row("get_many x10k hits", best(lambda: ours.get_many(hits)), 10_000)
    # isolate python list building of batch: call the raw ABI
    values = np.fromiter((int(a) for a in misses), dtype=np.int64, count=len(misses))
    encoded = np.empty(len(misses), dtype=np.int64)
    lib = mojo_maxminddb.reader.lib()
    row("mmd_find_ipv4_many raw ABI x50k (kernel)",
        best(lambda: (lib.mmd_find_ipv4_many(ours._address, ours._buffer_size,
                                            values.ctypes.data, len(values),
                                            ours._metadata.node_count, ours._metadata.record_size,
                                            ours._ipv4_start, encoded.ctypes.data), 1)[1]), 50_000)
    row("np.fromiter(int(a)) x50k (list->int64)",
        best(lambda: np.fromiter((int(a) for a in misses), dtype=np.int64, count=50_000)), 50_000)
    row("result list comp over encoded x50k",
        best(lambda: [(int(v) >> 8, int(v) & 0xFF) for v in encoded]), 50_000)
    ctypes_find = lib.mmd_find_ipv4_value
    row("ctypes scalar call overhead x50k",
        best(lambda: [ctypes_find(ours._address, 0, ours._metadata.node_count,
                                  ours._metadata.record_size, ours._ipv4_start)
                      for _ in misses]), 50_000)
    # pure ctypes overhead: same call against an empty tree (node_count=0)
    row("ctypes call, node_count=0 (pure FFI) x50k",
        best(lambda: [ctypes_find(ours._address, 0, 0, 6, 0) for _ in misses]), 50_000)
    # numpy baseline: int-of-address conversion vs struct
    row("numpy uint32 -> int64 view x50k",
        best(lambda: np.frombuffer(np.asarray(misses, dtype=np.uint32).tobytes(), dtype=np.int64)), 50_000)

    # mixed IPv6 batch
    mixed = (misses[:25_000] + [ipaddress.ip_address(known[i % 2 + 5]) for i in range(25_000)])
    def encode_mixed():
        packed = np.zeros((len(mixed), 16), dtype=np.uint8)
        for index, address in enumerate(mixed):
            raw = address.packed
            packed[index, : len(raw)] = np.frombuffer(raw, dtype=np.uint8)
        return packed
    row("mixed batch encoding loop x50k (python -> n*16 buffer)", best(encode_mixed), 50_000)
    row("get_with_prefix_len_many x50k mixed v4/v6", best(lambda: ours.get_with_prefix_len_many(mixed)), 50_000)

    # 6. upstream equivalents
    row("UPSTREAM get x50k miss", best(lambda: [ref.get(a) for a in misses]), 50_000)
    row("UPSTREAM get x10k hits", best(lambda: [ref.get(a) for a in hits]), 10_000)
    row("UPSTREAM get_with_prefix_len x50k miss",
        best(lambda: [ref.get_with_prefix_len(a) for a in misses]), 50_000)

    # 7. open_database cost
    def open_mojo():
        reader = mojo_maxminddb.open_database(database, mojo_maxminddb.MODE_MMAP)
        reader.close()
        return 1
    def open_ref():
        reader = upstream.open_database(database, upstream.MODE_MMAP)
        reader.close()
        return 1
    row("mojo open_database", best(open_mojo, repeat=5))
    row("upstream open_database", best(open_ref, repeat=5))

    # 8. iteration (walk every node)
    def walk_ours():
        return sum(1 for _ in ours)
    def walk_ref():
        return sum(1 for _ in ref)
    def py_iter_def():
        # reader._generate_children rewritten without recursion: the upstream-style
        # loop, measured so the "python in __iter__" row has a baseline
        count = ours._metadata.node_count
        stack = [(0, 0, 0)]
        out = 0
        while stack:
            node, depth, acc = stack.pop()
            if node < count:
                out += 2
                stack.append((ours._read_node(node, 0), depth + 1, 0))
                stack.append((ours._read_node(node, 1), depth + 1, 0))
            else:
                out += 1
        return out
    row("our _read_node per-node driver (no yield)", best(py_iter_def, repeat=3))
    row("our __iter__ full tree", best(walk_ours, repeat=3))
    row("UPSTREAM __iter__ full tree", best(walk_ref, repeat=3))

    # sizes: does the batch cross the parallel threshold meaningfully?
    for n in (1_000, 16_383, 16_384, 50_000):
        sub = misses[:n]
        row(f"mmd_find_ipv4_many n={n}", best(lambda sub=sub: ours.get_many(sub)), n)

    meta = ours.metadata()
    print(f"\nmeta: node_count={meta.node_count} record_size={meta.record_size} "
          f"ip_version={meta.ip_version} search_tree={meta.search_tree_size} "
          f"db_bytes={ours._buffer_size}", flush=True)
    print("hit addresses:", known, flush=True)
    ours.close()
    ref.close()


if __name__ == "__main__":
    main()
