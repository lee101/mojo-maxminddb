# mojo-maxminddb

A Mojo-backed port of the Python [`maxminddb`](https://github.com/maxmind/MaxMind-DB-Reader-python)
reader for MMDB GeoIP lookup. It imports as `maxminddb`, keeps the upstream
reader API, and moves the branch-heavy binary trie traversal into compiled
Mojo.

This is a real MMDB reader, not a format-specific shortcut. It reads IPv4,
IPv6, and mixed databases with 24-, 28-, or 32-bit search-tree records and
decodes the full MMDB data type set.

## Install

The repository pins the tested Mojo nightly and installs upstream
`maxminddb` 2.6.2 for parity tests:

```bash
pixi install
pixi run build
pixi run test
```

`pixi run build` produces `dist/libmojo-maxminddb.so`. Pixi also places
`python/` first on `PYTHONPATH`, so `import maxminddb` resolves to this port.

## Usage

This example runs against the checked-in MaxMind test database:

```python
import maxminddb

with maxminddb.open_database("tests/data/GeoIP2-City-Test.mmdb") as reader:
    record, prefix = reader.get_with_prefix_len("81.2.69.142")
    print(record["city"]["names"]["en"], record["country"]["iso_code"], prefix)
```

Output:

```text
London GB 31
```

Existing scalar code keeps the upstream names and signatures:

```python
reader = maxminddb.open_database("GeoLite2-City.mmdb")
record = reader.get("203.0.113.8")
metadata = reader.metadata()
reader.close()
```

For lookup-heavy workloads, the port adds batch methods that cross the FFI
boundary once:

```python
records = reader.get_many(["81.2.69.142", "67.43.156.1", "8.8.8.8"])
records_and_prefixes = reader.get_with_prefix_len_many(
    ["81.2.69.142", "67.43.156.1", "8.8.8.8"]
)
```

## Coverage

Covered:

- `open_database`, `Reader`, `get`, `get_with_prefix_len`, `metadata`,
  iteration, `close`, and context management
- `MODE_AUTO`, `MODE_MMAP_EXT`, `MODE_MMAP`, `MODE_FILE`, `MODE_MEMORY`, and
  `MODE_FD`
- IPv4, IPv6, IPv4 lookup in IPv6 trees, and databases without an IPv4 subtree
- 24-, 28-, and 32-bit search-tree nodes
- pointers, maps, arrays, UTF-8 strings, bytes, booleans, floats, doubles,
  signed `int32`, and unsigned 16/32/64/128-bit integers
- upstream-compatible metadata and `InvalidDatabaseError`
- added `get_many` and `get_with_prefix_len_many` batch APIs

Not covered:

- the upstream project’s optional CPython C extension binary; `MODE_MMAP_EXT`
  is accepted but selects the Mojo-backed reader
- the exact lazy-I/O memory profile of upstream `MODE_FILE`; this port loads
  that mode into contiguous memory so Mojo can traverse it
- writing or updating MMDB files

The parity suite uses MaxMind’s published real `.mmdb` fixtures, including
complex GeoIP2 City records and all three node widths. Those fixtures retain
their upstream MIT license in `tests/data/`.

## Benchmarks

Measured with `pixi run bench` on a dual-socket Intel Xeon E5-2697 v4
(72 logical CPUs), Linux 6.8.0-136-generic, Python 3.13.14. Both readers use
the same checked-in GeoIP2 City database and the best of three runs.

| case | mojo-maxminddb | upstream maxminddb | speedup |
| --- | ---: | ---: | ---: |
| scalar `get`, 50k miss-heavy | 126.47 ms | 252.91 ms | 2.00x |
| batch `get_many`, 50k miss-heavy | 31.72 ms | 233.64 ms | 7.36x |
| scalar `get`, 10k GeoIP2 hits | 180.99 ms | 1668.40 ms | 9.22x |
| batch `get_many`, 10k GeoIP2 hits | 172.36 ms | 1668.87 ms | 9.68x |
| batch get + prefix, 50k miss-heavy | 31.15 ms | 255.99 ms | 8.22x |

### SIMD phase: batch kernel before and after

Measured 2026-11-10 on the workspace machine (Intel Xeon E5-2697 v4, Linux
6.8.0-147-generic, Python 3.13.14). "Before" is `HEAD`'s
`src/maxminddb.mojo`; "after" is this tree. Both columns are whole
`bench/bench.py` cases, and the two libraries were run **interleaved** in one
session (`MOJO_MAXMINDDB_LIB` selects the `.so`) so machine drift hits both:
three rounds of old-then-new, then three rounds of new-then-old.

| case | before (ms, 3 runs) | after (ms, 3 runs) | median, 6 runs |
| --- | --- | --- | --- |
| batch `get_many`, 50k miss-heavy | 37.21, 38.86, 38.74 / 38.45, 38.97, 39.37 | 34.59, 35.20, 35.00 / 34.13, 34.54, 36.00 | 38.80 -> 34.80, **-10.3%** |
| batch get + prefix, 50k miss-heavy | 35.42, 34.76, 35.55 / 34.99, 35.76, 35.18 | 32.11, 32.08, 35.57 / 32.77, 32.68, 30.92 | 35.30 -> 32.39, **-8.2%** |
| batch `get_many`, 10k GeoIP2 hits | 178.11, 180.38, 174.82 / 201.76, 184.46, 186.84 | 178.18, 185.24, 176.45 / 177.78, 178.33, 172.21 | 182.42 -> 177.98, -2.4% |
| scalar `get`, 50k miss-heavy | 123.91, 127.16, 128.78 / 126.52, 127.16, 129.88 | 133.61, 121.42, 119.44 / 123.62, 129.45, 123.24 | 127.16 -> 123.43, within spread |
| scalar `get`, 10k GeoIP2 hits | 187.81, 188.45, 186.14 / 185.53, 188.89, 203.06 | 188.33, 201.74, 189.85 / 184.65, 190.62, 191.40 | 188.13 -> 190.24, within spread |

The kernel alone, called directly at bench.py's sizes (no decode, no result
list, best of seven, `bench/_kernel_ab.py`, old and new libraries swapped to
rule out ordering):

| kernel call | before | after | ratio |
| --- | ---: | ---: | ---: |
| `mmd_find_ipv4_many`, 50k misses | 4.92 / 4.94 ms | 1.98 ms | 2.5x faster |
| `mmd_find_ipv4_many`, 10k hits | 3.53 / 3.47 ms | 1.03 ms | 3.4x faster |

The 10k-hit case barely moves: its 10k-record clone-and-decode (~170 ms of
the ~180 ms; the decoded-record templates are rebuilt per address) dominates
it, and the 50k-miss total is dominated by `ctypes` and result-list work, not
by the kernel. The two miss-heavy batch cases carry the kernel win end to
end; the hit case shows it only in the kernel row above.

What changed in the kernel, and what did not survive `pixi run bench`:

- **Kept:** the IPv4 batch group walk. Each lane previously re-entered
  `read_node` per bit -- so `record_size` was re-compared, the index re-checked
  and the byte range re-validated for every lane-bit -- and the patch update
  ran *after* the loop. `_ipv4_lane` is now `find_value`'s IPv4 walk with the
  record width specialised per group, shared by the scalar and batch entry
  points. That is also what fixed a real bug: the old group walk reported the
  neighbouring lane's depth (it advanced one bit past the stop bit and did not
  mask stopped lanes), so batches that mixed hit and miss lanes returned
  prefix lengths from the wrong lane. The 10k-hit and 50k-miss results above
  changed no further once that was correct.
- **Rejected: lockstep masked gather.** Walking the four lanes of a group
  in lockstep with one whole-record gather per step was 3.4x *slower* than
  the lane walk on both bench sizes (`bench/_kernel_ab.py`): a tree walk is a
  dependent-load chain, and masking lanes that stop at different depths does
  not shorten it while the gather costs more than the four scalar loads. It
  is not in the tree.
- **Rejected: faster Python staging.** Replacing the per-address encode loop
  in `get_with_prefix_len_many` with vectorised row writes is 6.75x faster in
  isolation (107.8 -> 16.0 ms at 50k mixed), and `_ip` instead of
  `int(address)` halves the `np.fromiter` (7.34 -> 3.50 ms). Neither moved the
  public call beyond its noise: the mixed path is ~50 ms of staging plus
  ~950 ms of the same record cloning as `get`, and a mixed 50k
  `get_with_prefix_len_many` measured 1097.7/1075.2 ms before and
  1025.0/1079.8 ms after. Both were reverted rather than kept on the strength
  of a microbenchmark.

### Parallel phase: what `parallelize` is worth, and where it is not

Measured 2026-11-10 on the workspace machine (Intel Xeon E5-2697 v4, Linux
6.8.0-147-generic, Python 3.13.14, load average 22-65 throughout -- the host is
shared). The kernel is called directly, no decode and no result list, best of
five, old and new libraries interleaved.

The mechanism first, because the old code looked parallel but was not. Both
batch entry points had a `PARALLEL_THRESHOLD = 16_384` branch that split the
batch into 16 chunks and dispatched them with

```mojo
@__parameter
def run_chunk(chunk: Int): ...
for chunk in range(PARALLEL_CHUNKS):
    run_chunk(chunk)
```

which is a serial loop over 16 inlined bodies: 0.102 us/address at 16,383 and
0.103 us at 16,384, exactly the serial rate. It is now a real
`from max.algorithm import parallelize` (the `std.algorithm` form does not
compile on this toolchain), four workers, one chunk each, behind a threshold
measured next.

This needed the `max` package: `max.algorithm` does not resolve from the `mojo`
package alone. `pixi.toml` now pins `max == 26.7.0.dev2026092605` beside
`mojo == 1.2.0.dev2026092605`.

**The cost of dispatching.** A `parallelize` call with an empty body costs
~4.3-6.5 ms per call from a `ctypes` host in this environment, roughly 0.3 ms
when the process is confined to four CPUs, and ~27 us inside `mojo run`. That
cost is flat from 2 to 16 workers and does not fall with repeated calls, so it
is per-call rather than one-off pool creation. For a kernel whose whole 50k
batch takes ~2 ms, that dispatch is larger than the work, which is why the
threshold is not small.

**Crossover, gate environment** (`.so` loaded from `ctypes`, no CPU pinning,
the same conditions as `pixi run bench`). 4 workers, median of 5 interleaved
runs:

| addresses | serial | 4 workers | speedup |
| --- | ---: | ---: | ---: |
| 50,000 | 1.86 ms | 1.87 ms | 1.00x (below threshold, stays serial) |
| 262,144 | 10.30 ms | 7.92 ms | 1.30x |
| 300,000 | 11.32 ms | 8.83 ms | 1.29x |
| 400,000 | 20.04 ms | 9.74 ms | 1.56x |
| 500,000 | 20.23 ms | 19.00 ms | 1.19x |
| 750,000 | 29.11 ms | 15.41 ms | 1.93x |
| 1,000,000 | 37.90 ms | 18.42 ms | 2.09x |

An earlier sweep with fewer repeats put the first win at ~175k-200k (parallel
losing at 150k and 16384) and no loss from 262,144 upward in five repeats.
The threshold is 2^18 = 262,144: above that first crossover, so no caller that
fits in one of the benchmark sizes pays for it. Below the threshold the code
still runs the serial range; against the previous library at 1k/10k/50k/262143
addresses it measures 1.02x/1.00x/1.01x/1.00x, i.e. unchanged.

**Threads 1/2/4/8 on the gate's 10,829-byte tree** (1M addresses): 4 workers
2.38x, 8 workers 3.37x, 16 workers 4.57x; confined to four CPUs all three are
identical at 3.7x. That extra scaling is the shared host's scheduler handing a
wider thread group more wall-clock, not throughput the algorithm controls, so
the cap is four workers as the host requires.

**Where it is worth having.** On the checked-in test fixture the walk is a
35 ns/address dependent-load chain and the batch total is dominated by Python:
at 1M addresses, `np.fromiter` is 246 ms and the result list 403 ms against a
42 ms kernel, so a 1.7-2.2x kernel win is a couple of percent of `get_many`.
The phase is sized for batch callers with real trees, which is what a
GeoLite2-City database is. On a 112 MB, 5M-node pool - the same walk, just
cache-missing - four workers give 4.3x, eight 8.4x and sixteen 17.8x at 2M
addresses, and 4.3x/7.7x/15.6x at 500k. It is not a microbenchmark artifact:
the win comes exactly where node loads leave cache, which is what production
databases do.

**Numbers, honestly.**

- `pixi run bench` is unchanged: all five cases are below the threshold.
  `batch get_many, 50k miss-heavy` 35.27 ms, `scalar get, 50k` 136.51 ms,
  upstream ratios 7.90x and 2.02x - within this host's spread of the values
  already in the table above.
- The public API at 500k-1M addresses moved by about +-2% in three interleaved
  rounds (500k: 496.8 -> 507.2 ms) because the Python staging above dominates;
  it does not regress at the sizes the benchmark uses.

**Equality.** The parallel path must return exactly the serial result.
`test_parallel_kernel_output_identical_to_serial` and
`test_parallel_mixed_kernel_output_identical_to_serial` call each kernel twice
on the same input, once below the threshold (serial) and once above it
(4-way), and compare packed pointer/prefix/status buffers. Both were checked
to fail when a chunk boundary was deliberately shifted by one, so they test
the thing they claim to.

**Not kept: more workers.** Eight and sixteen workers win more on this host
only because it is oversubscribed; on four CPUs they are indistinguishable
from four. Four is kept and the flattening is recorded above rather than
sold as a scaling story.

Scalar traversal passes the address bits directly as integer arguments and
returns pointer and prefix in one value, avoiding temporary NumPy arrays and
three `ctypes` result objects per call. IPv4 lookup uses a dedicated five-argument
ABI and dispatches the database record width once before traversing the trie.
Homogeneous IPv4 batches load each SIMD-width group of addresses as one vector
and walk the group's lanes against a width-specialised walk, with a scalar
tail; the walk itself is the tree's dependent-load chain, one bit at a time
(see the SIMD phase section below for what a lockstep gather measured).
Batches at or above 262,144 addresses split into four `parallelize` chunks;
smaller batches stay serial (see the parallel phase above for why the
threshold sits that high and what the four workers are worth).

The hit benchmark cycles through seven addresses. Decoded records seen more
than once are retained as bounded templates and cloned with a type-specific
container copier, so returned dictionaries and lists remain independently
mutable without repeatedly parsing the same MMDB values.

There is intentionally no GPU path. Trie traversal performs only a few integer
operations per unpredictable 6–8 byte node load, below the roughly two
operations-per-byte threshold, while record decoding constructs Python
objects. Device transfer and launch overhead therefore have no
arithmetic-intensive work to amortize.

## How it works

The Python layer memory-maps a database (or loads it for memory/file modes) and
exposes the contiguous bytes as a zero-copy NumPy `uint8` view. The view’s
address, database length, parsed metadata, and packed IP address cross a small
C ABI through `ctypes`; buffers remain owned by Python.

Mojo walks the MMDB binary trie directly. A 24-bit node occupies six bytes, a
28-bit node seven bytes with the two high nibbles shared, and a 32-bit node
eight bytes. Traversal returns the data pointer and exact prefix length.
The homogeneous IPv4 batch path writes integer addresses directly into one
contiguous NumPy buffer and receives packed pointer/prefix results in another.
Mixed IPv4/IPv6 batches use an `n × 16` byte matrix and contiguous pointer,
prefix, and status buffers.

After traversal, Python decodes MMDB values into the same dictionaries, lists,
strings, bytes, integers, floats, and booleans returned by upstream. Keeping
object construction in Python preserves the public API; moving it through a C
ABI would only add a second serialization format.

MIT.
