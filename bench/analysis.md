# ANALYZE: where the time goes in mojo-maxminddb

Measured 2026-11-10 on the workspace checkout, commit `274dd93`, clean worktree.
No file under `src/` or `tests/` was touched. Every number below is from a run in
this session; nothing is estimated or carried over from the README.

## Method

| what | how |
| --- | --- |
| headline table | `pixi run bench` (flock-wrapped, run alone) |
| stage isolation | `bench/analysis_profile.py` — one stage timed per row via the library's own bound ctypes handles |
| plain-Python / numpy baseline | inline script using `python/maxminddb/reader.py` + a 28-bit walker, no Mojo call in the loop |
| trie shape | depth histogram over the same 50k/10k address sets |

Machine: Intel Xeon E5-2697 v4 @ 2.30 GHz, Linux 6.8.0-147-generic, Python 3.13.14.
Fixture: `tests/data/GeoIP2-City-Test.mmdb`, 22,569 B, 1,547 nodes, **28-bit records**
(7 B/node), ip_version 6, search tree 10,829 B, IPv4 start node 96.
Timings are best-of-N wall time of a whole loop, same convention as `bench/bench.py`.

Reproduce:

```bash
pixi run build
pixi run bench                                # headline table
flock /tmp/mojo-bench.lock pixi run python -u bench/analysis_profile.py
```

Stage rows (find_integer, ctypes, decode, clone, open, iter) were re-run in the
second and third profile invocations; the numbers below are the repeat values,
which agreed to within ~3% across runs.

## Headline: port vs upstream (from `pixi run bench`)

| case | mojo-maxminddb | upstream maxminddb | speedup |
| --- | ---: | ---: | ---: |
| scalar `get`, 50k miss-heavy | 123.69 ms | 253.88 ms | 2.05x |
| batch `get_many`, 50k miss-heavy | 36.34 ms | 259.45 ms | 7.14x |
| scalar `get`, 10k GeoIP2 hits | 194.41 ms | 1735.15 ms | 8.93x |
| batch `get_many`, 10k GeoIP2 hits | 180.46 ms | 1694.96 ms | 9.39x |
| batch get + prefix, 50k miss-heavy | 34.74 ms | 282.55 ms | 8.13x |

No case is slower than upstream. The port column splits further below.

## Ranked hotspot table

Ranked by wall time contributed to the workloads real callers run (50k miss-heavy and
10k GeoIP2-hit batches). `flops` is the arithmetic actually required by the MMDB
format: per trie node one record select (shift/and on the address bit) plus the
width-specific reassembly of the 28-bit record — counted as 8 integer ops/node, and
`bytes` is the 7 B node load plus the address-bit extraction, i.e. bytes actually
touched on the dependent pointer-chase. "Bound" uses those figures, not peak-DRAM
figures, because the hot node pool is 1,547 × 7 B = 10.8 KB (L1-resident) and what
stalls is the *dependent* load latency, not bandwidth.

| # | function | input size | time (total / per call) | bytes touched | flops | flops/byte | bound | time sits in | vs upstream | next phase |
| ---: | --- | --- | ---: | ---: | ---: | ---: | --- | --- | --- | --- |
| 1 | `Decoder._decode` (uncached record decode) | 10k hit records | 1892.35 ms / 189.2 us | ~1.2 KB decoded record/branch | 0 (object construction) | ~0 | allocation-bound (Python objects) | Python | upstream `get` 165.5 us/call total, i.e. same class | **none** (no kernel exists) |
| 2 | `_resolve_data_pointer` → `_clone_record` (warm cache path) | 10k hits, 5 distinct pointers | 177.12 ms / 17.7 us | 0 (pure Python) | 0 | 0 | allocation-bound | Python | part of upstream's 165.5 us `get` | **none** |
| 3 | `Reader.get` scalar glue (coerce → `_check_version` → `_find_integer` → dispatch) | 50k calls | 125.98 ms / 2.52 us | n/a (glue) | n/a | n/a | ctypes-call-bound | **Python/ctypes** (1.52 us of it is the bare FFI call) | 5.10 us upstream → **2.0x faster** | simd (batch-only; scalar is ABI floor) |
| 4 | `find_value` kernel, IPv4 (behind `mmd_find_ipv4_value`) | 50k lookups, 5.2 nodes mean | 105.12 ms / 2.10 us | 36.5 B (5.2 × 7 B) | ~42 | 1.15 | latency-bound dependent loads | **kernel** (`ctypes` floor already stripped: 105.1 of it, 76 needed) | n/a (upstream has no kernel) | unroll |
| 5 | `find_value` kernel, IPv4 hits | 10k lookups, 27.4 nodes mean | 25.07 ms / 2.51 us | 191.8 B (27.4 × 7 B) | ~219 | 1.14 | latency-bound dependent loads | kernel | n/a | unroll |
| 6 | `mmd_find_ipv4_many` kernel (SIMD-width-lane loop + serial chunks) | 50k packed int64 | 4.95 ms / 0.099 us | 36.5 B/lookup | ~42 | 1.15 | latency-bound; *serial* | kernel (0.099 us/lookup ≈ 44 ns = 5.2 L2-dependent loads) | n/a | **parallel** (SIMD already done) |
| 7 | `get_many` batch glue on misses | 50k | 35.47 ms / 0.709 us | n/a | n/a | n/a | ctypes + list-build | Python (6.8 ms `np.fromiter` + 4.95 ms kernel + 10.4 ms result list + ~13 ms status/list machinery) | 5.19 us upstream → **7.3x faster** | parallel |
| 8 | `get_with_prefix_len_many` mixed v4/v6 path | 50k (25k v4 + 25k v6) | 408.92 ms / 8.18 us | 16 B staged + nodes | ~63 (mixed depth) | ~0.5 | Python re-entry per element | **Python loop re-entering per element** (64.4 ms encode loop + 408.9 ms total) | not benchmarked by `bench.py`; upstream `get_with_prefix_len` 5.10 us/call x 50k = 255 ms class → **1.6x slower** | unroll |
| 9 | mixed-batch encode loop (`address.packed` → `n×16` buffer) | 50k | 64.37 ms / 1.29 us | 16 B/address written | 0 | 0 | Python per-element | Python (`np.frombuffer` + row assign per address) | n/a | unroll |
| 10 | `ipaddress.ip_address(str)` coercion | 10k strings | 32.48 ms / 3.25 us | n/a | 0 | 0 | Python parsing | Python stdlib | upstream pays the same | none |
| 11 | `Reader.__iter__` full tree | 1,547 nodes | 6.76 ms | 10.8 KB tree | ~12k | ~1.1 | Python `yield`-driven per node | Python (`_read_node` slice+`int.from_bytes` per node; driver alone 4.35 ms) | upstream `__iter__` 31.30 ms → **4.6x faster** | unroll |
| 12 | `open_database` | 1 open | 0.210 ms | 22.6 KB mmap | 0 | 0 | setup | Python | 0.187 ms → **12% slower** (below noise floor of one mmap; not a hotspot) | none |
| 13 | `ctypes` bare call floor (`mmd_find_ipv4_value`, node_count=0) | 50k | 76.12 ms / 1.52 us | 0 | 0 | 0 | FFI | Python/ctypes | unavoidable ABI cost | none |

Supporting measurements used for the bound/flops columns (all this session):

| measurement | value |
| --- | ---: |
| mean nodes touched, 50k misses | 5.22 (median 5, p99 11, max 24) → 36.5 B at 7 B/node |
| mean nodes touched, 10k hits | 27.40 (median 29, max 31) → 191.8 B at 7 B/node |
| kernel us/lookup, random address order | 0.0980 |
| kernel us/lookup, sorted address order | 0.0866 → 1.13x penalty from unpredictability |
| pure-Python 28-bit trie walk, 50k misses | 144.90 ms (2.90 us) — kernel is 29x faster |
| pure-Python 28-bit trie walk, 10k hits | 137.58 ms (13.76 us) — kernel is 5.5x faster |
| pure-Python uncached decode, 10k hits | 2070.24 ms (207.0 us) — same order as port's 189.2 us |
| numpy 32-pass bit extract, 50k | 8.16 ms |

## Python-level loops that re-enter per element

These are the only places where the port pays Python per address or per node; `src/`
was not modified, so these are findings, not fixes.

1. **`get_with_prefix_len_many` -> mixed branch** (`reader.py:183-192`): a Python `for`
   over every address doing `_check_version`, `address.packed`, `np.frombuffer(raw)`
   and two scalar buffer writes. 64.4 ms of the 408.9 ms mixed-batch time is this loop
   alone; the rest is the per-element `zip`/resolve list build at `reader.py:211-217`.
   This is why the mixed path (row 8) is the one row where the port loses to upstream.
2. **`_find_ipv4_many` result list** (`reader.py:240-248`): one Python iteration per
   encoded int64 with `int(value)` twice. 10.4 ms at 50k.
3. **`np.fromiter(int(address) for …)`** (`reader.py:221-225`): a Python generator
   crossing into numpy, 6.8 ms at 50k; a `uint32→int64` view is 13.2 ms only because it
   round-trips through `tobytes()` — a direct `.astype` on an already-int array is the
   shape to aim for in a later phase.
4. **`Reader.__iter__` / `_generate_children`** (`reader.py:420-439`): recursive generator
   with `self._read_node` per node — `bytes` slice + `int.from_bytes` + three range
   checks per node in Python. 4.35 ms of the 6.76 ms is this Python driver.
5. **`get_many`** (`reader.py:162-167`): builds the full prefix list then throws the
   prefix away. Free in the miss case (36.3 vs 34.7 ms for the prefix variant) but
   it doubles list construction for hits.

## Per-call allocations and copies

| site | allocation | measured cost |
| --- | --- | --- |
| `_find_ipv4_many` | `values` int64, `encoded` int64 (n each) | 2 × 400 KB at 50k |
| `get_with_prefix_len_many` mixed | `packed` n×16, `bit_counts`, `start_nodes`, `pointers`, `prefixes`, `statuses` | 6 arrays, ~1.4 MB at 50k |
| `find_ipv4_simd` | `addresses`, `nodes`, `depths`, `statuses`, `bits`, `encoded` SIMD registers | per W-group, register-level (no heap) |
| `read_node` (`src/maxminddb.mojo:15`) | none — raw byte loads | — |
| `_resolve_data_pointer` cache hit | `_clone_record` rebuilds the full dict/list tree | 17.7 us/call, row 2 |
| `_clone_record` on insert | decodes then clones, so the cached copy is decoded twice | included in row 1 |
| `Decoder._bytes` | `bytes(self._buffer[offset:offset+size])` per value, every value | part of row 1 |

## Verdict per hotspot

- **Rows 1-2, 12, 10 — `none`.** Record decoding is object construction in Python;
  there is no kernel to accelerate and the port is already at or below upstream
  (189.2 us uncached vs upstream's 207.0 us plain-Python decode; 165.5 us for a whole
  upstream `get` vs the port's 19.4 us warm-cache `get`). A Mojo decoder behind the
  C ABI would mean marshalling the whole record graph through a second serialization
  format before rebuilding Python dicts — the README's stated reason for keeping it
  in Python still holds, and nothing in these numbers contradicts it.
- **Rows 3, 7, 13 — `simd`.** Note the direction: SIMD cannot help the *scalar* entry
  points further. 1.52 us of the 2.52 us scalar `get` is the bare ctypes call, so the
  only remaining lever on scalar `get` is Amdahl-limited glue. What SIMD can still do
  is widen the existing `find_ipv4_simd` inner loop (row 6), where the per-lane
  `for lane in range(W)` is still a serial element loop inside a "SIMD" function.
- **Row 4, 5 — `unroll`.** 1.15 flops/byte and 5.2-27.4 dependent 7 B loads per lookup
  is latency-bound; unrolling the bit loop (two address bits per step, sharing the
  `node * 7` base) and hoisting the `record_size` branch out of the loop is the phase
  that fits. `find_value` already hoists it into three loops; `find_ipv4_simd` re-enters
  `read_node` with a `record_size` compare per lane-bit.
- **Rows 6, 7 — `parallel`.** The kernel is 0.099 us/lookup at 50k, serial. 5.2
  node-latency chains per miss cannot be shortened by SIMD; independent addresses can
  go to threads. Evidence that the parallel path is not in use: `mmd_find_ipv4_many`
  takes the same time at n=16,383 (1.675 ms, 0.102 us each) and n=16,384 (1.691 ms,
  0.103 us each) despite the `PARALLEL_THRESHOLD = 16_384` branch, and 50k total
  5.013 ms matches the serial per-lookup rate exactly. Per `MOJO_NOTES.md` §5b the
  `run_chunk` closure is `@__parameter`, but the chunks are dispatched by a plain
  `for chunk in range(PARALLEL_CHUNKS)` call — a serial loop over precompiled bodies,
  not `from max.algorithm import parallelize`. Also note the port already holds a
  7.3x win on this row, so parallel is a scalability lever for >1M addresses, not a
  win the harness currently rewards.
- **Row 8, 9 — `unroll`.** The mixed path's Python per-element loop is the only
  measured regression against upstream (408.9 ms vs 255 ms class). It is glue, not
  kernel, so the fix belongs in Python (vectorised packing) plus a Mojo-side
  mixed-width entry point, not in a new kernel.
- **No row gets `gpu`.** The highest arithmetic per byte anywhere on the hot path is
  1.15 flops/byte (rows 4-7), and the entire 50k-miss kernel does ~210 k integer ops
  total in 4.95 ms. At 1.15 flops/byte a PCIe round trip and kernel launch (~5-10 us
  plus 400 KB of copy) can never amortize, and row 1's real cost is Python object
  allocation, which a GPU cannot help. The README's "no GPU path" conclusion is
  confirmed by these measurements rather than merely restated.

## What this analysis did not cover

- Only the checked-in GeoIP2 City fixture (28-bit, 22.6 KB). A production
  GeoLite2-City (~70 MB, far more distinct pointers) would shift row 1 upward because
  the 256-entry decode cache would stop absorbing hits. Rows 1 and 2 are decayed to
  the cold path after the profile run (`len(_decode_cache) == 0` at the end of that
  process; the cache is not emptied by the benchmark), so treat them as the cost when
  the cache is cold — the design intent is that repeated pointers take row 2's 17.7 us
  instead of row 1's 189.2 us.
- No profiling of `close()`, mode `MODE_MEMORY`/`MODE_FILE`, or error paths.
- The mixed-batch upstream comparison uses upstream's scalar
  `get_with_prefix_len` × 50k (5.10 us) rather than a real upstream batch API, since
  upstream has no `get_with_prefix_len_many`; it is a derived figure, marked as such
  in row 8, and the only such figure in the table.
- Harness scripts `bench/analysis_profile.py` and `bench/analysis_baseline.py` were
  added beside `bench/analysis.md` to make each row reproducible; neither is imported
  by `bench/bench.py`.
