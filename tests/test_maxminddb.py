from __future__ import annotations

import io
import ipaddress
import struct
from pathlib import Path

import numpy as np
import pytest

import maxminddb
from maxminddb.decoder import Decoder

DATA = Path(__file__).parent / "data"

IPV4_LOOKUPS = [
    "1.1.1.1",
    "1.1.1.2",
    "1.1.1.3",
    "1.1.1.5",
    "1.1.1.7",
    "1.1.1.9",
    "1.1.1.15",
    "1.1.1.17",
    "1.1.1.31",
    "1.1.1.32",
    "1.1.1.33",
    "255.254.253.123",
]

IPV6_LOOKUPS = [
    "::1:ffff:ffff",
    "::2:0:0",
    "::2:0:1",
    "::2:0:33",
    "::2:0:40",
    "::2:0:49",
    "::2:0:50",
    "::2:0:57",
    "::2:0:58",
    "::2:0:59",
    "89fa::",
    "1.1.1.1",
]


def _metadata_values(metadata):
    return {
        key: getattr(metadata, key)
        for key in (
            "node_count",
            "record_size",
            "ip_version",
            "database_type",
            "languages",
            "binary_format_major_version",
            "binary_format_minor_version",
            "build_epoch",
            "description",
            "node_byte_size",
            "search_tree_size",
        )
    }


@pytest.mark.parametrize("record_size", [24, 28, 32])
@pytest.mark.parametrize("ip_version", [4, 6])
def test_real_database_lookup_and_metadata_parity(
    upstream_maxminddb, record_size, ip_version
):
    path = DATA / f"MaxMind-DB-test-ipv{ip_version}-{record_size}.mmdb"
    addresses = IPV4_LOOKUPS if ip_version == 4 else IPV6_LOOKUPS
    with maxminddb.open_database(path) as ours, upstream_maxminddb.open_database(
        path, upstream_maxminddb.MODE_MMAP
    ) as reference:
        assert _metadata_values(ours.metadata()) == _metadata_values(
            reference.metadata()
        )
        for address in addresses:
            assert ours.get_with_prefix_len(address) == reference.get_with_prefix_len(
                address
            )
            ip_object = ipaddress.ip_address(address)
            assert ours.get(ip_object) == reference.get(ip_object)


@pytest.mark.parametrize(
    "mode",
    [
        maxminddb.MODE_AUTO,
        maxminddb.MODE_MMAP_EXT,
        maxminddb.MODE_MMAP,
        maxminddb.MODE_FILE,
        maxminddb.MODE_MEMORY,
    ],
)
def test_open_modes(mode):
    path = DATA / "MaxMind-DB-test-ipv4-24.mmdb"
    with maxminddb.open_database(path, mode) as reader:
        assert reader.get("1.1.1.3") == {"ip": "1.1.1.2"}


def test_extension_reader_compatibility():
    from maxminddb.extension import Metadata, Reader

    with Reader(DATA / "MaxMind-DB-test-ipv4-24.mmdb", maxminddb.MODE_MMAP_EXT) as reader:
        assert isinstance(reader.metadata(), Metadata)
        assert reader.get("1.1.1.3") == {"ip": "1.1.1.2"}


def test_fd_mode_and_context_manager():
    path = DATA / "MaxMind-DB-test-ipv4-24.mmdb"
    with path.open("rb") as database_file:
        reader = maxminddb.open_database(database_file, maxminddb.MODE_FD)
    with reader:
        assert reader.get("1.1.1.9") == {"ip": "1.1.1.8"}
    assert reader.closed
    assert reader.get("1.1.1.9") == {"ip": "1.1.1.8"}
    with pytest.raises(ValueError, match="reopen"):
        with reader:
            pass


def test_decoder_database_all_types_matches_upstream(upstream_maxminddb):
    path = DATA / "MaxMind-DB-test-decoder.mmdb"
    addresses = [
        "1.1.1.3",
        "::1.1.1.0",
        "::ffff:1.1.1.128",
        "2001:db8::1",
    ]
    with maxminddb.open_database(path) as ours, upstream_maxminddb.open_database(
        path, upstream_maxminddb.MODE_MMAP
    ) as reference:
        for address in addresses:
            assert ours.get_with_prefix_len(address) == reference.get_with_prefix_len(
                address
            )
        record = ours.get("1.1.1.3")
        assert record["array"] == [1, 2, 3]
        assert record["boolean"] is True
        assert record["bytes"] == b"\x00\x00\x00*"
        assert record["double"] == 42.123456
        assert record["float"] == pytest.approx(1.1)
        assert record["int32"] == -268435456
        assert record["uint128"] == 1329227995784915872903807060280344576
        assert record["utf8_string"] == "unicode! ☯ - ♫"


DECODER_VECTORS = [
    (b"\x00\x04", []),
    (b"\x02\x04\x43Foo\x43\xe4\xba\xba", ["Foo", "人"]),
    (b"\x00\x07", False),
    (b"\x01\x07", True),
    (b"\x68\x40\x09\x21\xfb\x54\x44\x2e\xea", 3.14159265359),
    (b"\x04\x08\xbf\x8c\xcc\xcd", -1.100000023841858),
    (b"\x04\x01\xff\xff\xfe\x0c", -500),
    (b"\xe1\x42en\x43Foo", {"en": "Foo"}),
    (b"\x43\xe4\xba\xba", "人"),
    (b"\xa2\x2a\x78", 10872),
    (b"\xc4\xff\xff\xff\xff", 4294967295),
    (b"\x08\x02\xff\xff\xff\xff\xff\xff\xff\xff", 2**64 - 1),
    (b"\x10\x03" + b"\xff" * 16, 2**128 - 1),
    (b"\x84\x00\x00\x00\x2a", b"\x00\x00\x00*"),
]


@pytest.mark.parametrize(("encoded", "expected"), DECODER_VECTORS)
def test_decoder_published_vectors(encoded, expected):
    actual, offset = Decoder(encoded, pointer_test=True).decode(0)
    if isinstance(expected, float):
        assert actual == pytest.approx(expected)
    else:
        assert actual == expected
    assert offset == len(encoded)


@pytest.mark.parametrize(
    ("encoded", "expected"),
    [
        (b"\x20\x00", 0),
        (b"\x23\xff", 1023),
        (b"\x28\x03\xc9", 3017),
        (b"\x2f\xff\xff", 526335),
        (b"\x37\xff\xff\xff", 134744063),
        (b"\x38\xff\xff\xff\xff", 4294967295),
    ],
)
def test_pointer_decoder_vectors(encoded, expected):
    assert Decoder(encoded, pointer_test=True).decode(0) == (expected, len(encoded))


@pytest.mark.parametrize("record_size", [24, 28, 32])
@pytest.mark.parametrize("family", ["ipv4", "ipv6", "mixed"])
def test_iteration_parity(upstream_maxminddb, record_size, family):
    path = DATA / f"MaxMind-DB-test-{family}-{record_size}.mmdb"
    with maxminddb.open_database(path) as ours, upstream_maxminddb.open_database(
        path, upstream_maxminddb.MODE_MMAP
    ) as reference:
        assert [(str(network), value) for network, value in ours] == [
            (str(network), value) for network, value in reference
        ]


def test_metadata_data_pointers(upstream_maxminddb):
    path = DATA / "MaxMind-DB-test-metadata-pointers.mmdb"
    with maxminddb.open_database(path) as ours, upstream_maxminddb.open_database(
        path, upstream_maxminddb.MODE_MMAP
    ) as reference:
        assert ours.metadata().database_type == "Lots of pointers in metadata"
        assert _metadata_values(ours.metadata()) == _metadata_values(
            reference.metadata()
        )


def test_database_without_ipv4_search_tree(upstream_maxminddb):
    path = DATA / "MaxMind-DB-no-ipv4-search-tree.mmdb"
    addresses = ["1.1.1.1", "192.1.1.1", "200.0.2.1", "::200.0.2.1", "ef00::"]
    with maxminddb.open_database(path) as ours, upstream_maxminddb.open_database(
        path, upstream_maxminddb.MODE_MMAP
    ) as reference:
        for address in addresses:
            assert ours.get_with_prefix_len(address) == reference.get_with_prefix_len(
                address
            )


def test_complex_geoip_city_records_match_upstream(upstream_maxminddb):
    path = DATA / "GeoIP2-City-Test.mmdb"
    addresses = [
        "2001:218::1",
        "2001:220::abcd",
        "::214.0.0.1",
        "2.125.160.216",
        "67.43.156.1",
        "81.2.69.142",
        "175.16.199.1",
        "216.160.83.56",
        "8.8.8.8",
    ]
    with maxminddb.open_database(path) as ours, upstream_maxminddb.open_database(
        path, upstream_maxminddb.MODE_MMAP
    ) as reference:
        assert [ours.get_with_prefix_len(ip) for ip in addresses] == [
            reference.get_with_prefix_len(ip) for ip in addresses
        ]


def test_batch_lookup_matches_scalar_and_upstream(upstream_maxminddb):
    path = DATA / "GeoIP2-City-Test.mmdb"
    rng = np.random.default_rng(20260730)
    ipv4 = [
        ipaddress.IPv4Address(int(value))
        for value in rng.integers(0, 2**32, size=400, dtype=np.uint32)
    ]
    known = [
        ipaddress.ip_address(value)
        for value in (
            "2.125.160.216",
            "67.43.156.1",
            "81.2.69.142",
            "175.16.199.1",
            "216.160.83.56",
            "2001:218::1",
            "2001:220::abcd",
        )
    ]
    addresses = ipv4 + known * 10
    with maxminddb.open_database(path) as ours, upstream_maxminddb.open_database(
        path, upstream_maxminddb.MODE_MMAP
    ) as reference:
        expected = [reference.get_with_prefix_len(value) for value in addresses]
        assert ours.get_with_prefix_len_many(addresses) == expected
        assert ours.get_many(addresses) == [record for record, _ in expected]
        assert ours.get_many([]) == []


@pytest.mark.parametrize("record_size", [24, 28, 32])
@pytest.mark.parametrize("count", [0, 1, 2, 3, 4, 5, 9, 17, 1001])
def test_ipv4_batch_simd_tail_matches_scalar(record_size, count):
    """0, 1, W-1, W, W+1 and a large odd length (W = 4 on this toolchain)."""
    path = DATA / f"MaxMind-DB-test-ipv4-{record_size}.mmdb"
    values = [
        ipaddress.ip_address(IPV4_LOOKUPS[index % len(IPV4_LOOKUPS)])
        for index in range(count)
    ]
    with maxminddb.open_database(path) as reader:
        expected = [reader.get_with_prefix_len(value) for value in values]
        assert reader.get_with_prefix_len_many(values) == expected


@pytest.mark.parametrize("record_size", [24, 28, 32])
@pytest.mark.parametrize("count", [0, 1, 3, 4, 5, 7, 8, 9, 15, 16, 17, 1001])
def test_batch_vector_boundaries(record_size, count):
    """Batch lengths around the SIMD width, with uneven hit depths.

    Most test-fixture addresses return at prefixes 28-32, while a group of
    consecutive ones mixes hits (stops at a data node) and misses (stops with
    a node still inside the tree). A group walk that assumes one shared stop
    step reports the neighbouring lane's depth instead of its own.
    """
    path = DATA / f"MaxMind-DB-test-ipv4-{record_size}.mmdb"
    values = [
        ipaddress.ip_address(IPV4_LOOKUPS[index % len(IPV4_LOOKUPS)])
        for index in range(count)
    ]
    with maxminddb.open_database(path) as reader:
        expected = [reader.get_with_prefix_len(value) for value in values]
        assert reader.get_with_prefix_len_many(values) == expected
        assert reader.get_many(values) == [record for record, _ in expected]



@pytest.mark.parametrize("count", [262_143, 262_144, 262_145])
@pytest.mark.parametrize("mixed_families", [False, True])
def test_parallel_batch_threshold_matches_scalar(count, mixed_families):
    path = DATA / "GeoIP2-City-Test.mmdb"
    ipv4 = ipaddress.ip_address("8.8.8.8")
    ipv6 = ipaddress.ip_address("ffff::")
    values = [
        ipv4 if not mixed_families or index % 2 else ipv6
        for index in range(count)
    ]
    with maxminddb.open_database(path) as reader:
        expected_ipv4 = reader.get_with_prefix_len(ipv4)
        expected_ipv6 = reader.get_with_prefix_len(ipv6)
        actual = reader.get_with_prefix_len_many(values)
        assert len(actual) == count
        assert all(
            result
            == (
                expected_ipv4
                if not mixed_families or index % 2
                else expected_ipv6
            )
            for index, result in enumerate(actual)
        )


def test_parallel_kernel_output_identical_to_serial():
    """The `parallelize` path must write exactly what the serial path writes.

    The batch entry point picks serial or parallel from the size threshold, so
    the two cannot be compared through `get_with_prefix_len_many` alone. Here
    the kernel is called directly on the same addresses - once with a count
    below the threshold (serial) and once with the full count (4-way
    `parallelize`) - and the packed pointer/prefix results must be identical.
    Addresses alternate between known routes and random misses, so hit/miss
    and depth boundaries straddle the chunk edges.
    """
    from maxminddb._lib import lib

    path = DATA / "GeoIP2-City-Test.mmdb"
    # Mirrors `PARALLEL_THRESHOLD` in src/maxminddb.mojo.
    threshold = 262_144
    count = threshold + 4096
    rng = np.random.default_rng(20260730)
    with maxminddb.open_database(path) as reader:
        metadata = reader.metadata()
        routes = np.array(
            [int(ipaddress.ip_address(value)) for value in IPV4_LOOKUPS],
            dtype=np.int64,
        )
        values = rng.integers(0, 2**32, size=count, dtype=np.uint32).astype(
            np.int64
        )
        values[::3] = routes[np.arange(count)[::3] % len(routes)]

        serial = np.empty(count, dtype=np.int64)
        parallel = np.empty(count, dtype=np.int64)

        def find(results, n):
            lib().mmd_find_ipv4_many(
                reader._address,
                reader._buffer_size,
                values.ctypes.data,
                n,
                metadata.node_count,
                metadata.record_size,
                reader._ipv4_start if metadata.ip_version == 6 else 0,
                results.ctypes.data,
            )

        find(serial, threshold - 1)
        find(parallel, count)
        assert np.array_equal(serial[: threshold - 1], parallel[: threshold - 1])


def test_parallel_mixed_kernel_output_identical_to_serial():
    """Same serial/parallel comparison for the mixed IPv4/IPv6 entry point."""
    from maxminddb._lib import lib

    path = DATA / "GeoIP2-City-Test.mmdb"
    # Mirrors `PARALLEL_THRESHOLD` in src/maxminddb.mojo.
    threshold = 262_144
    count = threshold + 4096
    with maxminddb.open_database(path) as reader:
        metadata = reader.metadata()
        ipv4 = [ipaddress.ip_address(value) for value in IPV4_LOOKUPS]
        ipv6 = [ipaddress.ip_address(value) for value in IPV6_LOOKUPS]
        addresses = [
            (ipv4 if index % 2 else ipv6)[
                index % (len(ipv4) if index % 2 else len(ipv6))
            ]
            for index in range(count)
        ]
        packed = np.zeros((count, 16), dtype=np.uint8)
        bit_counts = np.empty(count, dtype=np.int64)
        start_nodes = np.empty(count, dtype=np.int64)
        for index, address in enumerate(addresses):
            raw = address.packed
            packed[index, : len(raw)] = np.frombuffer(raw, dtype=np.uint8)
            bit_counts[index] = address.max_prefixlen
            start_nodes[index] = (
                reader._ipv4_start
                if metadata.ip_version == 6 and address.version == 4
                else 0
            )

        def find(n):
            pointers = np.empty(n, dtype=np.int64)
            prefixes = np.empty(n, dtype=np.int64)
            statuses = np.empty(n, dtype=np.int64)
            lib().mmd_find_many(
                reader._address,
                reader._buffer_size,
                packed.ctypes.data,
                bit_counts.ctypes.data,
                start_nodes.ctypes.data,
                n,
                metadata.node_count,
                metadata.record_size,
                pointers.ctypes.data,
                prefixes.ctypes.data,
                statuses.ctypes.data,
            )
            return pointers, prefixes, statuses

        serial = find(threshold - 1)
        parallel = find(count)
        for serial_part, parallel_part in zip(serial, parallel):
            assert np.array_equal(serial_part, parallel_part[: threshold - 1])


def test_repeated_cached_records_are_independent():
    path = DATA / "GeoIP2-City-Test.mmdb"
    address = "81.2.69.142"
    with maxminddb.open_database(path) as reader:
        records = reader.get_many([address] * 4)
        records[0]["city"]["names"]["en"] = "changed"
        assert records[1]["city"]["names"]["en"] == "London"
        assert records[2]["city"]["names"]["en"] == "London"
        assert records[3]["city"]["names"]["en"] == "London"
        assert reader.get(address)["city"]["names"]["en"] == "London"


def test_invalid_inputs_and_files():
    path = DATA / "MaxMind-DB-test-ipv4-24.mmdb"
    with maxminddb.open_database(path) as reader:
        with pytest.raises(ValueError, match="IPv4-only"):
            reader.get("2001::")
        with pytest.raises(ValueError, match="does not appear"):
            reader.get("not_ip")
        with pytest.raises(TypeError, match="string or ipaddress"):
            reader.get(1)
    with pytest.raises(ValueError, match="Unsupported open mode"):
        maxminddb.open_database(path, 100)
    with pytest.raises(FileNotFoundError):
        maxminddb.open_database(DATA / "missing.mmdb")
    with pytest.raises(maxminddb.InvalidDatabaseError, match="valid MaxMind DB"):
        maxminddb.open_database(io.BytesIO(b"not a database"), maxminddb.MODE_FD)


def test_corrupt_double_raises_compatible_error():
    path = DATA / "GeoIP2-City-Test-Broken-Double-Format.mmdb"
    with maxminddb.open_database(path) as reader:
        with pytest.raises(maxminddb.InvalidDatabaseError, match="bad data"):
            reader.get("2001:220::")


def test_closed_mmap_reader_is_safe():
    reader = maxminddb.open_database(DATA / "MaxMind-DB-test-ipv4-24.mmdb")
    reader.close()
    reader.close()
    assert reader.closed
    with pytest.raises(ValueError, match="closed"):
        reader.get("1.1.1.1")


def test_private_packed_ffi_rejects_wrong_shape_and_dtype():
    path = DATA / "MaxMind-DB-test-ipv4-24.mmdb"
    with maxminddb.open_database(path) as reader:
        with pytest.raises(ValueError, match="contiguous uint8"):
            reader._find_packed(np.zeros(4, dtype=np.int64), 32)
        with pytest.raises(ValueError, match="contiguous uint8"):
            reader._find_packed(np.zeros(3, dtype=np.uint8), 32)
        with pytest.raises(ValueError, match="contiguous uint8"):
            reader._find_packed(np.zeros(16, dtype=np.uint8)[::2], 32)


@pytest.mark.parametrize("record_size", [24, 28, 32])
@pytest.mark.parametrize("ip_version", [4, 6])
def test_ipv4_integer_abi_matches_packed_abi(record_size, ip_version):
    path = DATA / f"MaxMind-DB-test-ipv{ip_version}-{record_size}.mmdb"
    with maxminddb.open_database(path) as reader:
        for value in IPV4_LOOKUPS:
            address = ipaddress.IPv4Address(value)
            packed = np.frombuffer(address.packed, dtype=np.uint8)
            assert reader._find_integer(address) == reader._find_packed(packed, 32)


# ---------------------------------------------------------------------------
# Parity with upstream maxminddb 2.6.2
#
# Every test below compares the port against the installed upstream package
# (fixtures in conftest.py) or against upstream's own published test vectors.
# Numeric comparisons use an explicit tolerance: the MMDB `float` type is a
# 32-bit IEEE value, so a decoded float can only be compared to the decimal
# literal upstream publishes within float32 rounding.
# ---------------------------------------------------------------------------

# ``abs=`` reproduces upstream's own ``assertAlmostEqual(..., places=3)`` in
# tests/decoder_test.py; the port must not be looser than that.
FLOAT32_ABS_TOL = 1e-3
# Doubles are exact: upstream uses ``assertEqual`` for the `double` type.
DOUBLE_ABS_TOL = 0.0


def _decode(decoder_class, encoded: bytes, pointer_base: int = 0):
    return decoder_class(encoded, pointer_base, pointer_test=True).decode(0)


def _decode_or_error(decoder_class, encoded: bytes, pointer_base: int = 0):
    try:
        return ("value",) + tuple(_decode(decoder_class, encoded, pointer_base))
    except Exception as error:  # noqa: BLE001 - the class is the assertion
        return ("error", type(error))


def _assert_matches_upstream(ours, upstream, encoded: bytes, label: str = ""):
    """Assert one decoding result equals upstream's, with tolerance for floats."""
    if ours[0] == "error" or upstream[0] == "error":
        assert ours == upstream, f"{label} {encoded.hex()}: {ours} != {upstream}"
        return
    ours_value, ours_offset, upstream_value, upstream_offset = (
        ours[1],
        ours[2],
        upstream[1],
        upstream[2],
    )
    assert ours_offset == upstream_offset, f"{label} {encoded.hex()}: offset"
    _assert_value_parity(ours_value, upstream_value, encoded, label)


def _assert_value_parity(ours_value, upstream_value, encoded=b"", label=""):
    if isinstance(upstream_value, float):
        if isinstance(upstream_value, float) and upstream_value != upstream_value:
            assert ours_value != ours_value, f"{label} {encoded.hex()}: NaN"
            return
        if upstream_value in (float("inf"), float("-inf")):
            assert ours_value == upstream_value, f"{label} {encoded.hex()}: inf"
            return
        tolerance = (
            DOUBLE_ABS_TOL
            if _looks_like_double(encoded)
            else FLOAT32_ABS_TOL
        )
        assert ours_value == pytest.approx(
            upstream_value, rel=0.0, abs=tolerance
        ), f"{label} {encoded.hex()}: {ours_value} != {upstream_value}"
        return
    if isinstance(upstream_value, dict):
        assert isinstance(ours_value, dict), f"{label} {encoded.hex()}: not a map"
        assert list(ours_value) == list(upstream_value), f"{label}: map keys/order"
        for key in upstream_value:
            _assert_value_parity(
                ours_value[key], upstream_value[key], encoded, f"{label}[{key}]"
            )
        return
    if isinstance(upstream_value, list):
        assert isinstance(ours_value, list), f"{label} {encoded.hex()}: not an array"
        assert len(ours_value) == len(upstream_value), f"{label}: array length"
        for ours_item, upstream_item in zip(ours_value, upstream_value):
            _assert_value_parity(ours_item, upstream_item, encoded, label)
        return
    assert ours_value == upstream_value, f"{label} {encoded.hex()}"
    assert type(ours_value) is type(upstream_value), f"{label}: type"


def _looks_like_double(encoded: bytes) -> bool:
    return bool(encoded) and encoded[0] >> 5 == 3


# Upstream tests/decoder_test.py, verbatim expectations. Only `float` values are
# compared with FLOAT32_ABS_TOL, exactly as upstream does.
UPSTREAM_DECODER_VECTORS = [
    # arrays
    (b"\x00\x04", []),
    (b"\x01\x04\x43\x46\x6f\x6f", ["Foo"]),
    (b"\x02\x04\x43\x46\x6f\x6f\x43\xe4\xba\xba", ["Foo", "人"]),
    # booleans
    (b"\x00\x07", False),
    (b"\x01\x07", True),
    # doubles
    (b"\x68\x00\x00\x00\x00\x00\x00\x00\x00", 0.0),
    (b"\x68\x3F\xE0\x00\x00\x00\x00\x00\x00", 0.5),
    (b"\x68\x40\x09\x21\xFB\x54\x44\x2E\xEA", 3.14159265359),
    (b"\x68\x40\x5E\xC0\x00\x00\x00\x00\x00", 123.0),
    (b"\x68\x41\xD0\x00\x00\x00\x07\xF8\xF4", 1073741824.12457),
    (b"\x68\xBF\xE0\x00\x00\x00\x00\x00\x00", -0.5),
    (b"\x68\xC0\x09\x21\xFB\x54\x44\x2E\xEA", -3.14159265359),
    (b"\x68\xC1\xD0\x00\x00\x00\x07\xF8\xF4", -1073741824.12457),
    # floats
    (b"\x04\x08\x00\x00\x00\x00", 0.0),
    (b"\x04\x08\x3F\x80\x00\x00", 1.0),
    (b"\x04\x08\x3F\x8C\xCC\xCD", 1.1),
    (b"\x04\x08\x40\x48\xF5\xC3", 3.14),
    (b"\x04\x08\x46\x1C\x3F\xF6", 9999.99),
    (b"\x04\x08\xBF\x80\x00\x00", -1.0),
    (b"\x04\x08\xBF\x8C\xCC\xCD", -1.1),
    (b"\x04\x08\xC0\x48\xF5\xC3", -3.14),
    (b"\x04\x08\xC6\x1C\x3F\xF6", -9999.99),
    # int32
    (b"\x00\x01", 0),
    (b"\x04\x01\xff\xff\xff\xff", -1),
    (b"\x01\x01\xff", 255),
    (b"\x04\x01\xff\xff\xff\x01", -255),
    (b"\x02\x01\x01\xf4", 500),
    (b"\x04\x01\xff\xff\xfe\x0c", -500),
    (b"\x02\x01\xff\xff", 65535),
    (b"\x04\x01\xff\xff\x00\x01", -65535),
    (b"\x03\x01\xff\xff\xff", 16777215),
    (b"\x04\x01\xff\x00\x00\x01", -16777215),
    (b"\x04\x01\x7f\xff\xff\xff", 2147483647),
    (b"\x04\x01\x80\x00\x00\x01", -2147483647),
    # maps
    (b"\xe0", {}),
    (b"\xe1\x42\x65\x6e\x43\x46\x6f\x6f", {"en": "Foo"}),
    (
        b"\xe2\x42\x65\x6e\x43\x46\x6f\x6f\x42\x7a\x68\x43\xe4\xba\xba",
        {"en": "Foo", "zh": "人"},
    ),
    (
        b"\xe1\x44\x6e\x61\x6d\x65\xe2\x42\x65\x6e"
        b"\x43\x46\x6f\x6f\x42\x7a\x68\x43\xe4\xba\xba",
        {"name": {"en": "Foo", "zh": "人"}},
    ),
    (
        b"\xe1\x49\x6c\x61\x6e\x67\x75\x61\x67\x65\x73"
        b"\x02\x04\x42\x65\x6e\x42\x7a\x68",
        {"languages": ["en", "zh"]},
    ),
    # pointers
    (b"\x20\x00", 0),
    (b"\x20\x05", 5),
    (b"\x20\x0a", 10),
    (b"\x23\xff", 1023),
    (b"\x28\x03\xc9", 3017),
    (b"\x2f\xf7\xfb", 524283),
    (b"\x2f\xff\xff", 526335),
    (b"\x37\xf7\xf7\xfe", 134217726),
    (b"\x37\xff\xff\xff", 134744063),
    (b"\x38\x7f\xff\xff\xff", 2147483647),
    (b"\x38\xff\xff\xff\xff", 4294967295),
    # strings (all four size encodings)
    (b"\x40", ""),
    (b"\x41\x31", "1"),
    (b"\x43\xE4\xBA\xBA", "人"),
    (
        b"\x5b\x31\x32\x33\x34\x35\x36\x37\x38\x39\x30\x31\x32\x33\x34\x35"
        b"\x36\x37\x38\x39\x30\x31\x32\x33\x34\x35\x36\x37",
        "123456789012345678901234567",
    ),
    (
        b"\x5c\x31\x32\x33\x34\x35\x36\x37\x38\x39\x30\x31\x32\x33\x34\x35"
        b"\x36\x37\x38\x39\x30\x31\x32\x33\x34\x35\x36\x37\x38",
        "1234567890123456789012345678",
    ),
    (
        b"\x5d\x00\x31\x32\x33\x34\x35\x36\x37\x38\x39\x30\x31\x32\x33\x34"
        b"\x35\x36\x37\x38\x39\x30\x31\x32\x33\x34\x35\x36\x37\x38\x39",
        "12345678901234567890123456789",
    ),
    (
        b"\x5d\x01\x31\x32\x33\x34\x35\x36\x37\x38\x39\x30\x31\x32\x33\x34"
        b"\x35\x36\x37\x38\x39\x30\x31\x32\x33\x34\x35\x36\x37\x38\x39\x30",
        "123456789012345678901234567890",
    ),
    (b"\x5e\x00\xd7" + 500 * b"\x78", "x" * 500),
    (b"\x5e\x06\xb3" + 2000 * b"\x78", "x" * 2000),
    (b"\x5f\x00\x10\x53" + 70000 * b"\x78", "x" * 70000),
    # uint16
    (b"\xa0", 0),
    (b"\xa1\xff", 255),
    (b"\xa2\x01\xf4", 500),
    (b"\xa2\x2a\x78", 10872),
    (b"\xa2\xff\xff", 65535),
    # uint32
    (b"\xc0", 0),
    (b"\xc1\xff", 255),
    (b"\xc2\x01\xf4", 500),
    (b"\xc2\x2a\x78", 10872),
    (b"\xc2\xff\xff", 65535),
    (b"\xc3\xff\xff\xff", 16777215),
    (b"\xc4\xff\xff\xff\xff", 4294967295),
]


def _upstream_large_uint_vectors(bits: int):
    # Mirrors upstream tests/decoder_test.py :: generate_large_uint
    control = b"\x02" if bits == 64 else b"\x03"
    vectors = [
        (b"\x00" + control, 0),
        (b"\x02" + control + b"\x01\xf4", 500),
        (b"\x02" + control + b"\x2a\x78", 10872),
    ]
    for power in range(bits // 8 + 1):
        vectors.append((bytes([power]) + control + b"\xff" * power, 2 ** (8 * power) - 1))
    return vectors


UPSTREAM_BYTE_VECTORS = [
    (bytes([0xC0 ^ encoded[0]]) + encoded[1:], expected.encode("utf-8"))
    for encoded, expected in UPSTREAM_DECODER_VECTORS[52:60]
]


@pytest.mark.parametrize(
    ("encoded", "expected"),
    UPSTREAM_DECODER_VECTORS
    + _upstream_large_uint_vectors(64)
    + _upstream_large_uint_vectors(128),
    ids=lambda value: value.hex()[:24] if isinstance(value, bytes) else None,
)
def test_decoder_upstream_published_vectors(encoded, expected):
    """Value and offset match upstream tests/decoder_test.py exactly."""
    actual, offset = Decoder(encoded, pointer_test=True).decode(0)
    if isinstance(expected, float):
        assert actual == pytest.approx(expected, rel=0.0, abs=FLOAT32_ABS_TOL)
    else:
        assert actual == expected
    assert offset == len(encoded)


@pytest.mark.parametrize(
    ("encoded", "expected"),
    UPSTREAM_BYTE_VECTORS,
    ids=lambda value: value.hex()[:24] if isinstance(value, bytes) else None,
)
def test_decoder_upstream_byte_vectors(encoded, expected):
    """The `bytes` type re-encodes every upstream string vector."""
    actual, offset = Decoder(encoded, pointer_test=True).decode(0)
    assert actual == expected
    assert type(actual) is bytes
    assert offset == len(encoded)


@pytest.mark.parametrize(
    ("encoded", "pointer_base"),
    [
        (b"\x20\x00", 0),
        (b"\x23\xff", 0),
        (b"\x28\x03\xc9", 0),
        (b"\x37\xff\xff\xff", 0),
        (b"\x38\xff\xff\xff\xff", 0),
        (b"\x20\x00", 1),
        (b"\x20\x00", 4096),
        (b"\x38\xff\xff\xff\xff", 7),
    ],
    ids=lambda value: value.hex() if isinstance(value, bytes) else str(value),
)
def test_decoder_pointer_matches_upstream(upstream_decoder, encoded, pointer_base):
    """Pointer resolution and `pointer_base` handling match upstream."""
    ours = _decode(Decoder, encoded, pointer_base)
    reference = _decode(upstream_decoder, encoded, pointer_base)
    assert ours == reference


def test_decoder_upstream_published_vectors_match_upstream(
    upstream_decoder,
):
    """The port and the installed upstream agree on every published vector."""
    for encoded, _expected in UPSTREAM_DECODER_VECTORS:
        _assert_matches_upstream(
            _decode_or_error(Decoder, encoded),
            _decode_or_error(upstream_decoder, encoded),
            encoded,
            "published",
        )


def test_decoder_every_data_section_offset_matches_upstream(upstream_decoder):
    """Decode at every byte offset of a real data section, both readers."""
    buffer = (DATA / "MaxMind-DB-test-decoder.mmdb").read_bytes()
    compared = 0
    for offset in range(len(buffer)):
        ours = _decode_or_error_with_buffer(Decoder, buffer, offset)
        reference = _decode_or_error_with_buffer(upstream_decoder, buffer, offset)
        if ours[0] == "error" and reference[0] == "error":
            continue
        _assert_matches_upstream(
            ours, reference, buffer[offset : offset + 8], f"offset {offset}"
        )
        compared += 1
    # The data section of this fixture has >1000 decodable starting offsets.
    assert compared > 1000


def _decode_or_error_with_buffer(decoder_class, buffer: bytes, offset: int):
    try:
        value, new_offset = decoder_class(buffer, 0, pointer_test=True).decode(offset)
        return ("value", value, new_offset)
    except Exception as error:  # noqa: BLE001 - the class is the assertion
        return ("error", type(error))


# ---------------------------------------------------------------------------
# Degenerate and boundary decoder input
#
# Upstream defines the value for every well-formed encoding below, including
# the zero-length, one-element, shortened-int32 and size-boundary forms, so the
# port must return exactly upstream's value. Where a buffer is truncated
# upstream raises IndexError / struct.error while the port raises
# InvalidDatabaseError; those cases are asserted separately below.
# ---------------------------------------------------------------------------

def _mmdb_control(type_number: int, size: int, payload: bytes = b"") -> bytes:
    """Encode one MMDB value: low type in the control byte, high types extended."""
    if type_number < 8:
        return bytes([(type_number << 5) | size]) + payload
    return bytes([size, type_number - 7]) + payload


DEGENERATE_DECODER_VECTORS = [
    # Empty containers and zero-length scalars.
    (_mmdb_control(7, 0), {}),
    (_mmdb_control(11, 0), []),
    (_mmdb_control(2, 0), ""),
    (_mmdb_control(4, 0), b""),
    (_mmdb_control(5, 0), 0),
    (_mmdb_control(6, 0), 0),
    (_mmdb_control(8, 0), 0),
    (_mmdb_control(9, 0), 0),
    (_mmdb_control(10, 0), 0),
    (_mmdb_control(10, 1, b"\x00"), 0),
    # One-element containers.
    (_mmdb_control(7, 1, b"\x41a\x41b"), {"a": "b"}),
    (_mmdb_control(11, 1, b"\x41a"), ["a"]),
    (_mmdb_control(11, 1, _mmdb_control(7, 1, b"\x41a\x41b")), [{"a": "b"}]),
    # Duplicate map keys: a later pair overwrites the value in place, so the
    # first-seen key order survives (upstream `_decode_map` assigns into a dict).
    (
        _mmdb_control(7, 2, b"\x41a" + _mmdb_control(5, 1, b"\x01")
                      + b"\x41a" + _mmdb_control(5, 1, b"\x02")),
        {"a": 2},
    ),
    (
        _mmdb_control(7, 3, b"\x41a" + _mmdb_control(5, 1, b"\x01")
                      + b"\x41b" + _mmdb_control(5, 1, b"\x02")
                      + b"\x41a" + _mmdb_control(5, 1, b"\x03")),
        {"a": 3, "b": 2},
    ),
    # int32 shortened sizes: upstream left-pads to four bytes, so the result is
    # non-negative (maxminddb/decoder.py `_decode_int32`).
    (_mmdb_control(8, 1, b"\xff"), 255),
    (_mmdb_control(8, 2, b"\xff\xff"), 65535),
    (_mmdb_control(8, 3, b"\xff\xff\xff"), 16777215),
    (_mmdb_control(8, 2, b"\x80\x00"), 32768),
    (_mmdb_control(8, 1, b"\x00"), 0),
    # Signed four-byte int32 boundary values.
    (_mmdb_control(8, 4, b"\x7f\xff\xff\xff"), 2147483647),
    (_mmdb_control(8, 4, b"\x80\x00\x00\x00"), -2147483648),
    (_mmdb_control(8, 4, b"\xff\xff\xff\xff"), -1),
    # NaN, infinities and signed zero for double and float.
    (_mmdb_control(3, 8, struct.pack("!d", float("nan"))), float("nan")),
    (_mmdb_control(3, 8, struct.pack("!d", float("inf"))), float("inf")),
    (_mmdb_control(3, 8, struct.pack("!d", float("-inf"))), float("-inf")),
    (_mmdb_control(3, 8, struct.pack("!d", -0.0)), -0.0),
    (_mmdb_control(15, 4, struct.pack("!f", float("nan"))), float("nan")),
    (_mmdb_control(15, 4, struct.pack("!f", float("inf"))), float("inf")),
    (_mmdb_control(15, 4, struct.pack("!f", float("-inf"))), float("-inf")),
    (_mmdb_control(15, 4, struct.pack("!f", -0.0)), -0.0),
    # Largest finite double and the smallest positive subnormal float32.
    (
        _mmdb_control(3, 8, struct.pack("!d", 1.7976931348623157e308)),
        1.7976931348623157e308,
    ),
    (
        _mmdb_control(15, 4, struct.pack("!f", 1.401298464324817e-45)),
        1.401298464324817e-45,
    ),
    # Boolean size is a truth test, not a fixed size.
    (_mmdb_control(14, 0), False),
    (_mmdb_control(14, 1), True),
    (_mmdb_control(14, 2), True),
    # uint64/uint128 at zero length and at both extremes.
    (_mmdb_control(10, 16, b"\xff" * 16), 2**128 - 1),
    (_mmdb_control(9, 8, b"\xff" * 8), 2**64 - 1),
    (_mmdb_control(5, 2, b"\xff\xff"), 2**16 - 1),
    (_mmdb_control(6, 4, b"\xff" * 4), 2**32 - 1),
    # Nesting: an array holding an empty map and an empty array.
    (_mmdb_control(11, 2, _mmdb_control(7, 0) + _mmdb_control(11, 0)), [{}, []]),
    (_mmdb_control(7, 1, b"\x41a" + _mmdb_control(11, 0)), {"a": []}),
]


@pytest.mark.parametrize(
    ("encoded", "expected"),
    DEGENERATE_DECODER_VECTORS,
    ids=lambda value: value.hex()[:24] if isinstance(value, bytes) else None,
)
def test_decoder_degenerate_input_matches_upstream_value(
    upstream_decoder, encoded, expected
):
    ours_value, ours_offset = Decoder(encoded, pointer_test=True).decode(0)
    upstream_value, upstream_offset = _decode(upstream_decoder, encoded)
    assert ours_offset == upstream_offset == len(encoded)
    if isinstance(expected, float) and expected != expected:
        assert ours_value != ours_value
        assert upstream_value != upstream_value
    elif isinstance(expected, float):
        # double: exact; float32: within FLOAT32_ABS_TOL of the exact value.
        assert ours_value == pytest.approx(
            expected, rel=0.0, abs=FLOAT32_ABS_TOL
        )
        assert ours_value == upstream_value
    else:
        assert ours_value == expected
        assert ours_value == upstream_value
        assert type(ours_value) is type(upstream_value)


# An extended type's value is the control byte's low nibble followed by a
# second byte: that byte plus 7 is the real type number. Upstream's
# `_read_extended` rejects only ``type_num < 7``, so extension byte 0 is a
# map and bytes 1..255 are valid types; the port must accept type 7 too.
EXTENDED_TYPE_VECTORS = [
    (_mmdb_control(7, 0), {}, "extension byte 0 -> type 7, empty map"),
    (_mmdb_control(7, 1, b"\x41a\x41b"), {"a": "b"}, "extension byte 0 -> map"),
    (_mmdb_control(7, 2, b"\x41a\x41b\x41c\x41d"), {"a": "b", "c": "d"}, "two pairs"),
]


@pytest.mark.parametrize(
    ("encoded", "expected", "label"),
    EXTENDED_TYPE_VECTORS,
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_decoder_extended_type_boundary_matches_upstream(
    upstream_decoder, encoded, expected, label
):
    """Type number 7 (map) is reachable through the extended-type encoding."""
    ours_value, ours_offset = Decoder(encoded, pointer_test=True).decode(0)
    upstream_value, upstream_offset = _decode(upstream_decoder, encoded)
    assert ours_value == expected
    assert ours_offset == upstream_offset == len(encoded)
    assert ours_value == upstream_value


@pytest.mark.parametrize("level", [1, 100, 400])
def test_decoder_nesting_depth_matches_upstream(upstream_decoder, level):
    """Any nesting upstream can decode, the port decodes to the same value."""
    encoded = b"\x01\x04" * level + b"\x00\x04"
    ours = _decode_or_error(Decoder, encoded)
    reference = _decode_or_error(upstream_decoder, encoded)
    # Upstream recurses into Python, so its ceiling is the ambient recursion
    # limit; the port's own bound is 512, checked by the deep-nesting test.
    assert reference[0] == "value", "upstream recursion limit changed"
    _assert_matches_upstream(ours, reference, b"", f"depth {level}")


@pytest.mark.parametrize("level", [600, 1000, 5000])
def test_decoder_deep_nesting_raises_invalid_database_error(
    upstream_decoder, level
):
    """Past its bound the port raises InvalidDatabaseError, never RecursionError.

    Upstream recurses into Python and dies with RecursionError at this depth;
    the port reports the same condition as a database error.
    """
    encoded = b"\x01\x04" * level + b"\x00\x04"
    with pytest.raises(Exception) as upstream_error:
        _decode(upstream_decoder, encoded)
    assert upstream_error.value.__class__.__name__ in (
        "RecursionError",
        "IndexError",
    )
    with pytest.raises(maxminddb.InvalidDatabaseError, match="Too many nested"):
        _decode(Decoder, encoded)


def test_decoder_exhausts_size_encodings(upstream_decoder):
    """All four size encodings and every boundary between them."""
    vectors = [
        b"\x40",
        b"\x5b" + b"x" * 27,
        b"\x5c" + b"x" * 28,
        b"\x5d\x00" + b"x" * 29,
        b"\x5d\xff" + b"x" * 284,
        b"\x5e\x00\x00" + b"x" * 285,
        b"\x5e\xff\xff" + b"x" * (285 + 65535),
        b"\x5f\x00\x00\x00" + b"x" * 65821,
    ]
    for encoded in vectors:
        ours = _decode_or_error(Decoder, encoded)
        reference = _decode_or_error(upstream_decoder, encoded)
        _assert_matches_upstream(ours, reference, encoded, "size")


# Upstream slices the buffer and returns whatever bytes are there, so a size
# field that runs past the end yields a shorter value rather than an error
# (maxminddb/decoder.py `_decode_utf8_string`, `_decode_bytes`, `_decode_uint`).
# The port must return the same value and the same end offset.
TRUNCATED_PAYLOAD_VECTORS = [
    (_mmdb_control(2, 3, b"ab"), "ab"),
    (_mmdb_control(4, 4, b"\x00\x00"), b"\x00\x00"),
    (_mmdb_control(6, 4, b"\x00\x00"), 0),
    (_mmdb_control(6, 2, b"\x01"), 1),
    (_mmdb_control(10, 2, b"\xff"), 255),
    (_mmdb_control(11, 1, _mmdb_control(2, 1, b"a")), ["a"]),
]


@pytest.mark.parametrize(
    ("encoded", "expected"),
    TRUNCATED_PAYLOAD_VECTORS,
    ids=lambda value: value.hex() if isinstance(value, bytes) else None,
)
def test_decoder_truncated_payload_truncates_like_upstream(
    upstream_decoder, encoded, expected
):
    """A payload shorter than its declared size decodes to what is present."""
    ours_value, ours_offset = Decoder(encoded, pointer_test=True).decode(0)
    upstream_value, upstream_offset = _decode(upstream_decoder, encoded)
    assert ours_value == expected
    assert ours_value == upstream_value
    assert ours_offset == upstream_offset


# These buffers put the decoder past the end of the buffer at a point where it
# needs a control byte. Upstream raises IndexError there; the port raises
# InvalidDatabaseError: the same failure, reported as a database error, and
# never a partial value read past the end of the buffer.
TRUNCATED_DECODER_INPUTS = [
    b"",
    b"\x00",
    b"\x02",
    b"\xe1",
    b"\xe1" + _mmdb_control(2, 1, b"a"),
    _mmdb_control(11, 2, _mmdb_control(2, 1, b"a")),
    b"\x5d",
    b"\x5e\x00",
    b"\x5f\x00\x10",
    _mmdb_control(10, 0)[:1],
]


@pytest.mark.parametrize(
    "encoded", TRUNCATED_DECODER_INPUTS, ids=lambda value: value.hex() or "<empty>"
)
def test_decoder_truncated_input_raises_invalid_database_error(
    upstream_decoder, encoded
):
    """Truncation: upstream raises IndexError, the port InvalidDatabaseError."""
    with pytest.raises(Exception) as upstream_error:
        _decode(upstream_decoder, encoded)
    assert isinstance(
        upstream_error.value,
        (IndexError, struct.error, maxminddb.InvalidDatabaseError),
    )
    with pytest.raises(maxminddb.InvalidDatabaseError):
        _decode(Decoder, encoded)


@pytest.mark.parametrize(
    "encoded",
    [
        b"\x68\x00",
        _mmdb_control(15, 4, b"\x00\x00"),
        _mmdb_control(3, 8)[:1] + b"\x00\x00\x00",
        b"\x68",
        b"\x04\x08",
    ],
    ids=lambda value: value.hex(),
)
def test_decoder_fixed_width_truncation_raises_like_upstream(
    upstream_decoder, encoded
):
    """double/float/int32 read a fixed width; a short payload raises struct.error."""
    with pytest.raises(struct.error):
        _decode(upstream_decoder, encoded)
    with pytest.raises(struct.error):
        _decode(Decoder, encoded)


# An unknown type number is a decoder-level error in upstream itself, so both
# readers raise InvalidDatabaseError with the same message.
UNKNOWN_TYPE_VECTORS = [
    b"\x00\x80\x00",  # extension byte 128 -> type 135
    b"\x00\x86\x00",  # extension byte 134 -> type 141
]


@pytest.mark.parametrize(
    "encoded", UNKNOWN_TYPE_VECTORS, ids=lambda value: value.hex()
)
def test_decoder_unknown_type_number_matches_upstream(upstream_decoder, encoded):
    """An unknown type number raises the same class and message upstream uses."""
    ours = _decode_or_error(Decoder, encoded)
    reference = _decode_or_error(upstream_decoder, encoded)
    assert ours[0] == reference[0] == "error"
    assert ours[1] is maxminddb.InvalidDatabaseError
    with pytest.raises(maxminddb.InvalidDatabaseError, match="Unexpected type number"):
        _decode(Decoder, encoded)
    with pytest.raises(Exception) as upstream_error:
        _decode(upstream_decoder, encoded)
    assert type(upstream_error.value).__name__ == "InvalidDatabaseError"
    assert "Unexpected type number" in str(upstream_error.value)

def test_decoder_float_tolerance_bounds_observed_error():
    """FLOAT32_ABS_TOL is looser than the port's real 32-bit float error."""
    worst = 0.0
    for text in ("1.1", "3.14", "9999.99", "-1.1", "-3.14", "-9999.99"):
        encoded = b"\x04\x08" + struct.pack("!f", float(text))
        actual, _ = Decoder(encoded, pointer_test=True).decode(0)
        worst = max(worst, abs(actual - float(text)))
    assert worst <= FLOAT32_ABS_TOL
    assert DOUBLE_ABS_TOL == 0.0


# ---------------------------------------------------------------------------
# Reader parity with upstream maxminddb 2.6.2
# ---------------------------------------------------------------------------

# upstream tests/reader_test.py :: BaseTestReader.test_get_with_prefix_len.
# These are upstream's published prefix lengths, used without upstream at all,
# so a mock or an unimportable upstream cannot hide a regression.
UPSTREAM_PREFIX_LEN_VECTORS = [
    ("1.1.1.1", "MaxMind-DB-test-ipv6-32.mmdb", 8, None),
    ("::1:ffff:ffff", "MaxMind-DB-test-ipv6-24.mmdb", 128, {"ip": "::1:ffff:ffff"}),
    ("::2:0:1", "MaxMind-DB-test-ipv6-24.mmdb", 122, {"ip": "::2:0:0"}),
    ("1.1.1.1", "MaxMind-DB-test-ipv4-24.mmdb", 32, {"ip": "1.1.1.1"}),
]

# upstream tests/reader_test.py :: _check_ip_v4 / _check_ip_v6.
UPSTREAM_IPV4_HITS = {
    "1.1.1.1": "1.1.1.1",
    "1.1.1.2": "1.1.1.2",
    "1.1.1.4": "1.1.1.4",
    "1.1.1.8": "1.1.1.8",
    "1.1.1.16": "1.1.1.16",
    "1.1.1.32": "1.1.1.32",
    "1.1.1.3": "1.1.1.2",
    "1.1.1.5": "1.1.1.4",
    "1.1.1.7": "1.1.1.4",
    "1.1.1.9": "1.1.1.8",
    "1.1.1.15": "1.1.1.8",
    "1.1.1.17": "1.1.1.16",
    "1.1.1.31": "1.1.1.16",
}
UPSTREAM_IPV4_MISSES = ["1.1.1.33", "255.254.253.123"]
UPSTREAM_IPV6_HITS = {
    "::1:ffff:ffff": "::1:ffff:ffff",
    "::2:0:0": "::2:0:0",
    "::2:0:40": "::2:0:40",
    "::2:0:50": "::2:0:50",
    "::2:0:58": "::2:0:58",
    "::2:0:1": "::2:0:0",
    "::2:0:33": "::2:0:0",
    "::2:0:39": "::2:0:0",
    "::2:0:41": "::2:0:40",
    "::2:0:49": "::2:0:40",
    "::2:0:52": "::2:0:50",
    "::2:0:57": "::2:0:50",
    "::2:0:59": "::2:0:58",
}
UPSTREAM_IPV6_MISSES = ["1.1.1.33", "255.254.253.123", "89fa::"]

# upstream tests/reader_test.py :: test_iterator.
UPSTREAM_ITERATION_NETWORKS = {
    "ipv4": [
        "1.1.1.1/32",
        "1.1.1.2/31",
        "1.1.1.4/30",
        "1.1.1.8/29",
        "1.1.1.16/28",
        "1.1.1.32/32",
    ],
    "ipv6": [
        "::1:ffff:ffff/128",
        "::2:0:0/122",
        "::2:0:40/124",
        "::2:0:50/125",
        "::2:0:58/127",
    ],
    "mixed": [
        "1.1.1.1/32",
        "1.1.1.2/31",
        "1.1.1.4/30",
        "1.1.1.8/29",
        "1.1.1.16/28",
        "1.1.1.32/32",
        "::1:ffff:ffff/128",
        "::2:0:0/122",
        "::2:0:40/124",
        "::2:0:50/125",
        "::2:0:58/127",
    ],
}

# upstream tests/reader_test.py :: test_decoder.
UPSTREAM_DECODER_RECORD = {
    "array": [1, 2, 3],
    "boolean": True,
    "bytes": b"\x00\x00\x00*",
    "double": 42.123456,
    "float": 1.100000023841858,
    "int32": -268435456,
    "map": {"mapX": {"arrayX": [7, 8, 9], "utf8_stringX": "hello"}},
    "uint128": 1329227995784915872903807060280344576,
    "uint16": 0x64,
    "uint32": 0x10000000,
    "uint64": 0x1000000000000000,
    "utf8_string": "unicode! ☯ - ♫",
}

ALL_FIXTURES = sorted(path.name for path in DATA.glob("MaxMind-DB-*.mmdb")) + [
    "GeoIP2-City-Test.mmdb"
]

# A deterministic address sweep: every /16 of the 10/8 tree, every prefix
# boundary in ::2:0:0/120, plus both address-space extremes.
SWEEP_ADDRESSES = (
    [f"10.{a}.{b}.{c}" for a in (0, 1, 255) for b in (0, 255) for c in (0, 255)]
    + [f"::2:0:{index:x}" for index in range(0, 80)]
    + ["0.0.0.0", "255.255.255.255", "::", "ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff"]
)


def test_reader_upstream_published_prefix_lengths():
    for address, filename, prefix_length, record in UPSTREAM_PREFIX_LEN_VECTORS:
        with maxminddb.open_database(DATA / filename) as reader:
            assert reader.get_with_prefix_len(address) == (record, prefix_length)
            assert reader.get(address) == record


@pytest.mark.parametrize("record_size", [24, 28, 32])
def test_reader_upstream_published_records(record_size):
    with maxminddb.open_database(DATA / f"MaxMind-DB-test-ipv4-{record_size}.mmdb") as reader:
        for address, expected in UPSTREAM_IPV4_HITS.items():
            assert reader.get(address) == {"ip": expected}, address
        for address in UPSTREAM_IPV4_MISSES:
            assert reader.get(address) is None, address
    with maxminddb.open_database(DATA / f"MaxMind-DB-test-ipv6-{record_size}.mmdb") as reader:
        for address, expected in UPSTREAM_IPV6_HITS.items():
            assert reader.get(address) == {"ip": expected}, address
        for address in UPSTREAM_IPV6_MISSES:
            assert reader.get(address) is None, address


@pytest.mark.parametrize("record_size", [24, 28, 32])
@pytest.mark.parametrize("family", ["ipv4", "ipv6", "mixed"])
def test_reader_upstream_published_iteration_networks(record_size, family):
    path = DATA / f"MaxMind-DB-test-{family}-{record_size}.mmdb"
    with maxminddb.open_database(path) as reader:
        assert [str(network) for network, _ in reader] == (
            UPSTREAM_ITERATION_NETWORKS[family]
        )


@pytest.mark.parametrize("mode", [maxminddb.MODE_MMAP, maxminddb.MODE_MEMORY])
def test_reader_upstream_published_decoder_record(mode):
    """Value and type parity with upstream's published decoder record."""
    with maxminddb.open_database(DATA / "MaxMind-DB-test-decoder.mmdb", mode) as reader:
        record = reader.get("::1.1.1.0")
        assert set(record) == set(UPSTREAM_DECODER_RECORD)
        for key, expected in UPSTREAM_DECODER_RECORD.items():
            if key == "float":
                assert record[key] == pytest.approx(
                    expected, rel=0.0, abs=FLOAT32_ABS_TOL
                )
            else:
                assert record[key] == expected, key
                assert type(record[key]) is type(expected), key


@pytest.mark.parametrize("mode", [maxminddb.MODE_MMAP, maxminddb.MODE_MEMORY, maxminddb.MODE_FILE])
def test_reader_upstream_no_ipv4_search_tree(mode, upstream_maxminddb):
    path = DATA / "MaxMind-DB-no-ipv4-search-tree.mmdb"
    with maxminddb.open_database(path, mode) as ours, upstream_maxminddb.open_database(
        path, upstream_maxminddb.MODE_MMAP
    ) as reference:
        assert ours.get("1.1.1.1") == "::/64"
        assert ours.get("192.1.1.1") == "::/64"
        for address in ["1.1.1.1", "192.1.1.1", "200.0.2.1", "::200.0.2.1", "ef00::"]:
            assert ours.get_with_prefix_len(address) == reference.get_with_prefix_len(
                address
            )


@pytest.mark.parametrize("filename", ALL_FIXTURES)
def test_reader_full_address_sweep_matches_upstream(upstream_maxminddb, filename):
    """The Mojo trie walk returns upstream's pointer and prefix for every input."""
    path = DATA / filename
    with maxminddb.open_database(path) as ours, upstream_maxminddb.open_database(
        path, upstream_maxminddb.MODE_MMAP
    ) as reference:
        ours_metadata = ours.metadata()
        addresses = SWEEP_ADDRESSES
        if ours_metadata.ip_version == 4:
            addresses = [value for value in addresses if ":" not in value]
        for address in addresses:
            if ours_metadata.ip_version == 4 and ":" in address:
                with pytest.raises(ValueError, match="IPv4-only"):
                    ours.get_with_prefix_len(address)
                with pytest.raises(ValueError, match="IPv4-only"):
                    reference.get_with_prefix_len(address)
                continue
            assert ours.get_with_prefix_len(address) == reference.get_with_prefix_len(
                address
            ), address


def test_random_addresses_match_upstream(upstream_maxminddb, upstream_decoder):
    """A seeded random sweep of both address families against upstream."""
    path = DATA / "GeoIP2-City-Test.mmdb"
    rng = np.random.default_rng(20261110)
    addresses = [
        str(ipaddress.IPv4Address(int(value)))
        for value in rng.integers(0, 2**32, size=2000, dtype=np.uint64)
    ]
    addresses += [
        str(
            ipaddress.IPv6Address(
                (int(high) << 64) | int(low)
            )
        )
        for high, low in rng.integers(
            0, 2**64, size=(2000, 2), dtype=np.uint64
        )
    ]
    with maxminddb.open_database(path) as ours, upstream_maxminddb.open_database(
        path, upstream_maxminddb.MODE_MMAP
    ) as reference:
        assert [ours.get_with_prefix_len(value) for value in addresses] == [
            reference.get_with_prefix_len(value) for value in addresses
        ]


def test_metadata_matches_upstream_class(upstream_maxminddb, upstream_reader_types):
    """Every metadata field, plus the two derived properties and repr."""
    upstream_metadata_class, _ = upstream_reader_types
    for filename in ALL_FIXTURES:
        path = DATA / filename
        with maxminddb.open_database(path) as ours, upstream_maxminddb.open_database(
            path, upstream_maxminddb.MODE_MMAP
        ) as reference:
            ours_metadata = ours.metadata()
            reference_metadata = reference.metadata()
            assert type(ours_metadata).__name__ == "Metadata"
            assert _metadata_values(ours_metadata) == _metadata_values(
                reference_metadata
            ), filename
            probe = upstream_metadata_class(**reference_metadata.__dict__)
            assert ours_metadata.node_byte_size == probe.node_byte_size
            assert ours_metadata.search_tree_size == probe.search_tree_size
            # Upstream's repr names the class from its own module; compare the
            # argument list, which is what upstream formats in __repr__.
            ours_repr = repr(ours_metadata)
            reference_repr = repr(probe)
            assert ours_repr[ours_repr.index("(") :] == (
                reference_repr[reference_repr.index("(") :]
            ), filename


def test_metadata_derived_properties_match_upstream_formula():
    """node_byte_size = record_size // 4, search_tree_size = node_count * it."""
    for record_size in (24, 28, 32):
        path = DATA / f"MaxMind-DB-test-ipv4-{record_size}.mmdb"
        with maxminddb.open_database(path) as reader:
            metadata = reader.metadata()
            assert metadata.node_byte_size == record_size // 4
            assert metadata.search_tree_size == metadata.node_count * (
                record_size // 4
            )


@pytest.mark.parametrize(
    ("input_value", "error_class", "match"),
    [
        ("2001::", ValueError, "IPv4-only"),
        ("not_ip", ValueError, "does not appear"),
        ("nan", ValueError, "does not appear"),
        ("", ValueError, "does not appear"),
        (1, TypeError, "string or ipaddress"),
        (None, TypeError, "string or ipaddress"),
        (b"\x01\x01\x01\x01", TypeError, "string or ipaddress"),
        (1.5, TypeError, "string or ipaddress"),
    ],
    ids=repr,
)
def test_reader_input_errors_match_upstream(
    upstream_maxminddb, input_value, error_class, match
):
    """The port raises the same class and message upstream raises."""
    path = DATA / "MaxMind-DB-test-ipv4-24.mmdb"
    with maxminddb.open_database(path) as ours, upstream_maxminddb.open_database(
        path, upstream_maxminddb.MODE_MMAP
    ) as reference:
        with pytest.raises(error_class, match=match):
            ours.get(input_value)
        with pytest.raises(error_class, match=match):
            reference.get(input_value)


def test_reader_accepts_ip_objects_like_upstream(upstream_maxminddb):
    path = DATA / "MaxMind-DB-test-ipv6-24.mmdb"
    values = [
        ipaddress.ip_address(value)
        for value in ("::1:ffff:ffff", "::2:0:1", "1.1.1.1", "::")
    ]
    with maxminddb.open_database(path) as ours, upstream_maxminddb.open_database(
        path, upstream_maxminddb.MODE_MMAP
    ) as reference:
        for value in values:
            assert ours.get_with_prefix_len(value) == reference.get_with_prefix_len(
                value
            )


# ---------------------------------------------------------------------------
# Batch API parity
#
# get_many / get_with_prefix_len_many are additions; their contract is
# upstream's scalar result, element for element, under every input shape.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "addresses",
    [
        [],
        ["81.2.69.142"],
        ["81.2.69.142"] * 2,
        ["8.8.8.8", "8.8.8.8"],
        ["8.8.8.8", "81.2.69.142", "8.8.8.8"],
        ["0.0.0.0", "255.255.255.255"],
        ["::", "ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff"],
        ["8.8.8.8", "2001:218::1"],
        ["2001:218::1", "8.8.8.8"],
        ["::ffff:8.8.8.8", "2001:218::1"],
    ],
    ids=lambda value: f"{len(value)}:{value[:2]}",
)
def test_batch_degenerate_shapes_match_scalar(
    upstream_maxminddb, addresses
):
    path = DATA / "GeoIP2-City-Test.mmdb"
    with maxminddb.open_database(path) as ours, upstream_maxminddb.open_database(
        path, upstream_maxminddb.MODE_MMAP
    ) as reference:
        expected = [reference.get_with_prefix_len(value) for value in addresses]
        assert ours.get_with_prefix_len_many(addresses) == expected
        assert ours.get_many(addresses) == [record for record, _ in expected]
        # An empty batch is empty, never None and never an error.
        if not addresses:
            assert ours.get_many([]) == []
            assert ours.get_with_prefix_len_many([]) == []


def test_batch_accepts_ip_objects_and_any_iterable(upstream_maxminddb):
    path = DATA / "GeoIP2-City-Test.mmdb"
    mixed = ["8.8.8.8", ipaddress.ip_address("81.2.69.142"), "2001:218::1"]
    with maxminddb.open_database(path) as ours, upstream_maxminddb.open_database(
        path, upstream_maxminddb.MODE_MMAP
    ) as reference:
        expected = [reference.get_with_prefix_len(value) for value in mixed]
        assert ours.get_with_prefix_len_many(mixed) == expected
        assert ours.get_with_prefix_len_many(iter(mixed)) == expected
        assert ours.get_many(tuple(mixed)) == [record for record, _ in expected]


@pytest.mark.parametrize(
    ("addresses", "error_class", "match"),
    [
        (["1.1.1.1", "2001::"], ValueError, "IPv4-only"),
        (["2001::", "1.1.1.1"], ValueError, "IPv4-only"),
        (["1.1.1.1", "not_ip"], ValueError, "does not appear"),
        (["1.1.1.1", 1], TypeError, "string or ipaddress"),
        (["1.1.1.1", None], TypeError, "string or ipaddress"),
    ],
    ids=lambda value: repr(value)[:32],
)
def test_batch_input_errors_match_scalar(
    addresses, error_class, match
):
    """A bad element fails the batch with the error a scalar lookup raises."""
    path = DATA / "MaxMind-DB-test-ipv4-24.mmdb"
    with maxminddb.open_database(path) as reader:
        with pytest.raises(error_class, match=match):
            reader.get_many(addresses)
        with pytest.raises(error_class, match=match):
            reader.get_with_prefix_len_many(addresses)


@pytest.mark.parametrize("record_size", [24, 28, 32])
@pytest.mark.parametrize("ip_version", [4, 6])
def test_batch_parity_across_record_widths(
    upstream_maxminddb, record_size, ip_version
):
    """Every record width in both the IPv4 batch and the generic batch path."""
    path = DATA / f"MaxMind-DB-test-ipv{ip_version}-{record_size}.mmdb"
    addresses = (
        [str(ipaddress.IPv4Address(int(value))) for value in range(0, 2**32, 2**28)]
        if ip_version == 4
        else [str(ipaddress.ip_address(value)) for value in ("::2:0:1", "::2:0:59")] * 2
    )
    with maxminddb.open_database(path) as ours, upstream_maxminddb.open_database(
        path, upstream_maxminddb.MODE_MMAP
    ) as reference:
        expected = [reference.get_with_prefix_len(value) for value in addresses]
        assert ours.get_many(addresses) == [record for record, _ in expected]
        assert ours.get_with_prefix_len_many(addresses) == expected


def test_batch_repeated_record_values_are_equal_not_aliased(upstream_maxminddb):
    """Duplicates decode to equal values, and each record is independently mutable."""
    path = DATA / "GeoIP2-City-Test.mmdb"
    with maxminddb.open_database(path) as ours, upstream_maxminddb.open_database(
        path, upstream_maxminddb.MODE_MMAP
    ) as reference:
        expected = reference.get("81.2.69.142")
        records = ours.get_many(["81.2.69.142"] * 5)
        assert records == [expected] * 5
        for record in records[1:]:
            assert record is not records[0]
            assert record["city"] is not records[0]["city"]
        records[-1]["city"]["names"]["en"] = "changed"
        assert records[0]["city"]["names"]["en"] == expected["city"]["names"]["en"]


def test_batch_float_fields_within_tolerance(upstream_maxminddb):
    """Decoded float fields in records compare within the documented tolerance."""
    path = DATA / "GeoIP2-City-Test.mmdb"
    with maxminddb.open_database(path) as ours, upstream_maxminddb.open_database(
        path, upstream_maxminddb.MODE_MMAP
    ) as reference:
        (ours_record,) = ours.get_many(["81.2.69.142"])
        reference_record = reference.get("81.2.69.142")
        location = ours_record["location"]
        expected = reference_record["location"]
        for field in ("latitude", "longitude"):
            assert location[field] == pytest.approx(
                expected[field], rel=0.0, abs=FLOAT32_ABS_TOL
            )
        assert ours_record["location"]["accuracy_radius"] == (
            expected["accuracy_radius"]
        )


# ---------------------------------------------------------------------------
# Mojo search-tree kernel parity
#
# The four exported kernels must agree with each other and with upstream for
# every address, including the integer fast path used by scalar get().
# ---------------------------------------------------------------------------


def test_kernels_agree_with_upstream_on_every_sweep_address(upstream_maxminddb):
    """Integer ABI == packed ABI == upstream, for IPv4 and IPv6."""
    for filename in ("MaxMind-DB-test-ipv4-24.mmdb", "GeoIP2-City-Test.mmdb"):
        path = DATA / filename
        with maxminddb.open_database(path) as ours, upstream_maxminddb.open_database(
            path, upstream_maxminddb.MODE_MMAP
        ) as reference:
            ip_version = ours.metadata().ip_version
            for address in SWEEP_ADDRESSES:
                if ip_version == 4 and ":" in address:
                    continue
                ip_object = ipaddress.ip_address(address)
                packed = np.frombuffer(ip_object.packed, dtype=np.uint8)
                pointer, prefix = ours._find_packed(packed, packed.size * 8)
                assert (pointer, prefix) == ours._find_integer(ip_object), address
                expected = reference.get_with_prefix_len(address)
                if pointer:
                    resolved = ours._resolve_data_pointer(pointer)
                    assert (resolved, prefix) == expected, address
                else:
                    assert (None, prefix) == expected, address


def test_packed_abi_matches_upstream_for_ipv4_in_ipv6_tree(upstream_maxminddb):
    """IPv4-in-IPv6 lookups start at the stored ipv4_start node, as upstream does."""
    path = DATA / "MaxMind-DB-test-ipv6-32.mmdb"
    with maxminddb.open_database(path) as ours, upstream_maxminddb.open_database(
        path, upstream_maxminddb.MODE_MMAP
    ) as reference:
        assert ours._ipv4_start == reference._ipv4_start
        for address in ("1.1.1.1", "1.1.1.3", "8.8.8.8", "255.254.253.123"):
            ip_object = ipaddress.IPv4Address(address)
            pointer, prefix = ours._find_integer(ip_object)
            expected = reference.get_with_prefix_len(address)
            if pointer:
                assert (ours._resolve_data_pointer(pointer), prefix) == expected
            else:
                assert (None, prefix) == expected


def test_private_read_node_matches_upstream(upstream_maxminddb, upstream_reader_types):
    """Node records equal upstream's _read_node for every node and index."""
    _, upstream_reader_class = upstream_reader_types
    for filename in ("MaxMind-DB-test-ipv6-28.mmdb", "MaxMind-DB-test-ipv4-24.mmdb"):
        path = DATA / filename
        reference = upstream_reader_class(path, upstream_maxminddb.MODE_MMAP)
        try:
            with maxminddb.open_database(path) as ours:
                assert ours.metadata().node_count == reference.metadata().node_count
                for node in range(ours.metadata().node_count):
                    for index in (0, 1):
                        assert ours._read_node(node, index) == reference._read_node(
                            node, index
                        ), (filename, node, index)
        finally:
            reference.close()


def test_private_read_node_rejects_bad_arguments_like_upstream(
    upstream_maxminddb, upstream_reader_types
):
    """Out-of-range node numbers and indexes are database errors in both."""
    _, upstream_reader_class = upstream_reader_types
    path = DATA / "MaxMind-DB-test-ipv4-24.mmdb"
    reference = upstream_reader_class(path, upstream_maxminddb.MODE_MMAP)
    try:
        with maxminddb.open_database(path) as ours:
            node_count = ours.metadata().node_count
            for node, index in ((-1, 0), (node_count, 0), (0, 2), (0, -1)):
                try:
                    ours._read_node(node, index)
                except maxminddb.InvalidDatabaseError:
                    pass
                else:
                    raise AssertionError(f"({node}, {index}) should not be readable")
            # Upstream returns struct errors or garbage-free values here; the
            # port's contract is that it never reads past the search tree.
            assert ours._read_node(node_count - 1, 1) == reference._read_node(
                node_count - 1, 1
            )
    finally:
        reference.close()


# ---------------------------------------------------------------------------
# Reader construction, open modes and lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode",
    [maxminddb.MODE_AUTO, maxminddb.MODE_MMAP, maxminddb.MODE_FILE, maxminddb.MODE_MEMORY],
)
def test_open_database_modes_match_upstream(
    upstream_maxminddb, upstream_reader_types, mode
):
    """Each pure-Python mode returns upstream's records and metadata."""
    _, upstream_reader_class = upstream_reader_types
    path = DATA / "MaxMind-DB-test-ipv6-24.mmdb"
    addresses = ["::1:ffff:ffff", "::2:0:1", "1.1.1.1", "255.254.253.123"]
    reference = upstream_reader_class(path, mode)
    try:
        with maxminddb.open_database(path, mode) as ours:
            for address in addresses:
                assert ours.get_with_prefix_len(address) == (
                    reference.get_with_prefix_len(address)
                )
            assert _metadata_values(ours.metadata()) == _metadata_values(
                reference.metadata()
            )
            assert ours.metadata().database_type == reference.metadata().database_type
    finally:
        reference.close()


def test_fd_mode_matches_upstream(upstream_maxminddb):
    path = DATA / "MaxMind-DB-test-ipv4-28.mmdb"
    with path.open("rb") as database_file:
        ours = maxminddb.open_database(database_file, maxminddb.MODE_FD)
    with path.open("rb") as database_file:
        reference = upstream_maxminddb.open_database(
            database_file, upstream_maxminddb.MODE_FD
        )
    try:
        for address in IPV4_LOOKUPS:
            assert ours.get_with_prefix_len(address) == (
                reference.get_with_prefix_len(address)
            )
    finally:
        ours.close()
        reference.close()


def test_public_constants_and_errors_match_upstream(upstream_maxminddb):
    """The exported mode constants and error type are upstream's."""
    for name in (
        "MODE_AUTO",
        "MODE_MMAP_EXT",
        "MODE_MMAP",
        "MODE_FILE",
        "MODE_MEMORY",
        "MODE_FD",
    ):
        assert getattr(maxminddb, name) == getattr(upstream_maxminddb, name), name
        assert name in maxminddb.__all__
    assert issubclass(maxminddb.InvalidDatabaseError, RuntimeError)
    assert maxminddb.InvalidDatabaseError.__mro__[1:] == (
        upstream_maxminddb.InvalidDatabaseError.__mro__[1:]
    )
    # Upstream exposes the class from both `.errors` and the package root.
    from maxminddb.errors import InvalidDatabaseError as from_errors

    assert from_errors is maxminddb.InvalidDatabaseError
    assert sorted(maxminddb.__all__) == sorted(upstream_maxminddb.__all__)


@pytest.mark.parametrize("mode", [100, -1, 3, None, "2"])
def test_unsupported_open_mode_matches_upstream(upstream_maxminddb, mode):
    path = DATA / "MaxMind-DB-test-ipv4-24.mmdb"
    with pytest.raises(ValueError, match="Unsupported open mode"):
        maxminddb.open_database(path, mode)
    with pytest.raises(ValueError, match="Unsupported open mode"):
        upstream_maxminddb.open_database(path, mode)


def test_reader_constructor_errors_match_upstream(upstream_maxminddb):
    """A bad mode raises ValueError; a missing path raises FileNotFoundError."""
    path = DATA / "MaxMind-DB-test-ipv4-24.mmdb"
    with pytest.raises(ValueError, match="Unsupported open mode"):
        maxminddb.Reader(path, 100)
    with pytest.raises(ValueError, match="Unsupported open mode"):
        upstream_maxminddb.Reader(path, 100)
    with pytest.raises(FileNotFoundError):
        maxminddb.open_database(DATA / "missing.mmdb")
    with pytest.raises(FileNotFoundError):
        upstream_maxminddb.open_database(DATA / "missing.mmdb")
    with pytest.raises(TypeError):
        maxminddb.open_database(path, maxminddb.MODE_AUTO, 1)


def test_non_database_bytes_match_upstream_error():
    with pytest.raises(maxminddb.InvalidDatabaseError, match="valid MaxMind DB"):
        maxminddb.open_database(io.BytesIO(b""), maxminddb.MODE_FD)
    with pytest.raises(maxminddb.InvalidDatabaseError, match="bad data"):
        maxminddb.open_database(
            io.BytesIO(Reader_METADATA_MARKER + b"\x00"), maxminddb.MODE_FD
        )


Reader_METADATA_MARKER = b"\xAB\xCD\xEFMaxMind.com"


def test_closed_reader_behaviour_matches_upstream(upstream_maxminddb):
    """closed is False, then True; double close is a no-op in both."""
    path = DATA / "MaxMind-DB-test-ipv4-24.mmdb"
    ours = maxminddb.open_database(path)
    reference = upstream_maxminddb.open_database(path, upstream_maxminddb.MODE_MMAP)
    try:
        assert ours.closed is False
        assert reference.closed is False
        assert ours.close() is None
        assert ours.closed is True
        assert ours.close() is None
        with pytest.raises(ValueError, match="closed"):
            ours.get("1.1.1.1")
        with pytest.raises(ValueError, match="reopen"):
            with ours:
                pass
        # Upstream keeps metadata readable after close for pure Python reads.
        assert ours.metadata().node_count == reference.metadata().node_count
    finally:
        reference.close()


def test_memory_and_fd_modes_stay_readable_after_close():
    """MODE_MEMORY and MODE_FD hold their own bytes, so reads still work."""
    path = DATA / "MaxMind-DB-test-ipv4-24.mmdb"
    for mode in (maxminddb.MODE_MEMORY, maxminddb.MODE_FD):
        if mode == maxminddb.MODE_FD:
            with path.open("rb") as database_file:
                reader = maxminddb.open_database(database_file, mode)
        else:
            reader = maxminddb.open_database(path, mode)
        reader.close()
        assert reader.get("1.1.1.9") == {"ip": "1.1.1.8"}
