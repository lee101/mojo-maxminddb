from __future__ import annotations

import io
import ipaddress
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
@pytest.mark.parametrize("count", [1, 3, 5, 9, 17])
def test_ipv4_batch_simd_tail_matches_scalar(record_size, count):
    path = DATA / f"MaxMind-DB-test-ipv4-{record_size}.mmdb"
    values = [
        ipaddress.ip_address(IPV4_LOOKUPS[index % len(IPV4_LOOKUPS)])
        for index in range(count)
    ]
    with maxminddb.open_database(path) as reader:
        expected = [reader.get_with_prefix_len(value) for value in values]
        assert reader.get_with_prefix_len_many(values) == expected


@pytest.mark.parametrize("count", [16383, 16384])
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
