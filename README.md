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

Scalar traversal passes the address bits directly as integer arguments and
returns pointer and prefix in one value, avoiding temporary NumPy arrays and
three `ctypes` result objects per call. IPv4 lookup uses a dedicated five-argument
ABI and dispatches the database record width once before traversing the trie.
Homogeneous IPv4 batches load each SIMD-width group of addresses as one vector
and walk the group's lanes against a width-specialised walk, with a scalar
tail; the walk itself is the tree's dependent-load chain, one bit at a time
(see the SIMD phase section below for what a lockstep gather measured).
Batches of at least 16,384 addresses are split into 16 coarse parallel chunks;
smaller batches stay serial.

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
