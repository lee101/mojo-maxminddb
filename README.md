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

Scalar traversal passes the address bits directly as integer arguments and
returns pointer and prefix in one value, avoiding temporary NumPy arrays and
three `ctypes` result objects per call. IPv4 lookup uses a dedicated five-argument
ABI and dispatches the database record width once before traversing the trie.
Homogeneous IPv4 batches traverse SIMD-width groups and use a scalar tail.
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
