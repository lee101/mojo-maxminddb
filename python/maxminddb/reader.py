"""MaxMind DB reader with Mojo search-tree traversal."""

from __future__ import annotations

import ctypes
import ipaddress
import mmap
import os
from ipaddress import IPv4Address, IPv6Address
from typing import Any, Iterable, Iterator

import numpy as np

from ._lib import lib
from .const import MODE_AUTO, MODE_FD, MODE_FILE, MODE_MEMORY, MODE_MMAP, MODE_MMAP_EXT
from .decoder import Decoder
from .errors import InvalidDatabaseError

_IPV4_MAX_NUM = 2**32


def _clone_record(value):
    if isinstance(value, dict):
        return {key: _clone_record(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clone_record(item) for item in value]
    return value


class Metadata:
    def __init__(self, **values) -> None:
        self.node_count = values["node_count"]
        self.record_size = values["record_size"]
        self.ip_version = values["ip_version"]
        self.database_type = values["database_type"]
        self.languages = values["languages"]
        self.binary_format_major_version = values["binary_format_major_version"]
        self.binary_format_minor_version = values["binary_format_minor_version"]
        self.build_epoch = values["build_epoch"]
        self.description = values["description"]

    @property
    def node_byte_size(self) -> int:
        return self.record_size // 4

    @property
    def search_tree_size(self) -> int:
        return self.node_count * self.node_byte_size

    def __repr__(self) -> str:
        arguments = ", ".join(f"{key}={value!r}" for key, value in self.__dict__.items())
        return f"{self.__module__}.{self.__class__.__name__}({arguments})"


class Reader:
    _DATA_SECTION_SEPARATOR_SIZE = 16
    _METADATA_START_MARKER = b"\xAB\xCD\xEFMaxMind.com"

    def __init__(self, database, mode: int = MODE_AUTO) -> None:
        if mode not in (
            MODE_AUTO,
            MODE_MMAP_EXT,
            MODE_MMAP,
            MODE_FILE,
            MODE_MEMORY,
            MODE_FD,
        ):
            raise ValueError(f"Unsupported open mode ({mode})")

        self._mode = mode
        self._mmap: mmap.mmap | None = None
        filename: Any = database
        if mode == MODE_FD:
            self._buffer = database.read()
            filename = getattr(database, "name", database)
        elif mode in (MODE_MEMORY, MODE_FILE):
            with open(database, "rb") as database_file:
                self._buffer = database_file.read()
        else:
            with open(database, "rb") as database_file:
                self._mmap = mmap.mmap(database_file.fileno(), 0, access=mmap.ACCESS_READ)
            self._buffer = self._mmap

        self._buffer_size = len(self._buffer)
        self._array = np.frombuffer(self._buffer, dtype=np.uint8)
        self._address = int(self._array.ctypes.data)
        self.closed = False

        metadata_start = self._buffer.rfind(
            self._METADATA_START_MARKER,
            max(0, self._buffer_size - 128 * 1024),
        )
        if metadata_start == -1:
            self.close()
            raise InvalidDatabaseError(
                f"Error opening database file ({filename}). "
                "Is this a valid MaxMind DB file?"
            )

        metadata_start += len(self._METADATA_START_MARKER)
        metadata, _ = Decoder(self._buffer, metadata_start).decode(metadata_start)
        if not isinstance(metadata, dict):
            self.close()
            raise InvalidDatabaseError(
                f"Error reading metadata in database file ({filename})."
            )
        try:
            self._metadata = Metadata(**metadata)
        except (KeyError, TypeError) as error:
            self.close()
            raise InvalidDatabaseError(
                f"Error reading metadata in database file ({filename})."
            ) from error
        if (
            type(self._metadata.node_count) is not int
            or self._metadata.node_count < 0
            or self._metadata.record_size not in (24, 28, 32)
            or self._metadata.ip_version not in (4, 6)
            or self._metadata.search_tree_size
            + self._DATA_SECTION_SEPARATOR_SIZE
            > self._buffer_size
        ):
            self.close()
            raise InvalidDatabaseError(
                f"Error reading metadata in database file ({filename})."
            )

        self._decoder = Decoder(
            self._buffer,
            self._metadata.search_tree_size + self._DATA_SECTION_SEPARATOR_SIZE,
        )
        self._decode_cache = {}
        self._seen_pointers = set()

        node = 0
        if self._metadata.ip_version == 6:
            for _ in range(96):
                if node >= self._metadata.node_count:
                    break
                node = self._read_node(node, 0)
        self._ipv4_start = node

    def metadata(self) -> Metadata:
        return self._metadata

    def get(self, ip_address: str | IPv4Address | IPv6Address):
        record, _ = self.get_with_prefix_len(ip_address)
        return record

    def get_with_prefix_len(
        self, ip_address: str | IPv4Address | IPv6Address
    ) -> tuple[Any | None, int]:
        address = self._coerce_address(ip_address)
        self._check_version(address, ip_address)
        pointer, prefix_length = self._find_integer(address)
        if pointer:
            return self._resolve_data_pointer(pointer), prefix_length
        return None, prefix_length

    def get_many(self, ip_addresses: Iterable[str | IPv4Address | IPv6Address]):
        """Look up many addresses with one Mojo traversal call."""
        return [
            record
            for record, _ in self.get_with_prefix_len_many(ip_addresses)
        ]

    def get_with_prefix_len_many(
        self, ip_addresses: Iterable[str | IPv4Address | IPv6Address]
    ) -> list[tuple[Any | None, int]]:
        self._ensure_readable()
        addresses = [self._coerce_address(value) for value in ip_addresses]
        count = len(addresses)
        if not count:
            return []
        if all(isinstance(address, IPv4Address) for address in addresses):
            return self._find_ipv4_many(addresses)

        packed = np.zeros((count, 16), dtype=np.uint8)
        bit_counts = np.empty(count, dtype=np.int64)
        start_nodes = np.empty(count, dtype=np.int64)
        for index, address in enumerate(addresses):
            self._check_version(address, address)
            raw = address.packed
            packed[index, : len(raw)] = np.frombuffer(raw, dtype=np.uint8)
            bit_counts[index] = address.max_prefixlen
            start_nodes[index] = (
                self._ipv4_start
                if self._metadata.ip_version == 6 and address.version == 4
                else 0
            )

        pointers = np.zeros(count, dtype=np.int64)
        prefixes = np.zeros(count, dtype=np.int64)
        statuses = np.zeros(count, dtype=np.int64)
        lib().mmd_find_many(
            self._address,
            self._buffer_size,
            packed.ctypes.data,
            bit_counts.ctypes.data,
            start_nodes.ctypes.data,
            count,
            self._metadata.node_count,
            self._metadata.record_size,
            pointers.ctypes.data,
            prefixes.ctypes.data,
            statuses.ctypes.data,
        )
        self._raise_statuses(statuses)
        return [
            (
                self._resolve_data_pointer(int(pointer)) if pointer else None,
                int(prefix),
            )
            for pointer, prefix in zip(pointers, prefixes)
        ]

    def _find_ipv4_many(self, addresses) -> list[tuple[Any | None, int]]:
        count = len(addresses)
        values = np.fromiter(
            (int(address) for address in addresses),
            dtype=np.int64,
            count=count,
        )
        encoded = np.empty(count, dtype=np.int64)
        lib().mmd_find_ipv4_many(
            self._address,
            self._buffer_size,
            values.ctypes.data,
            count,
            self._metadata.node_count,
            self._metadata.record_size,
            self._ipv4_start if self._metadata.ip_version == 6 else 0,
            encoded.ctypes.data,
        )
        failures = encoded[encoded < 0]
        if failures.size:
            self._raise_status(int(failures[0]))
        return [
            (
                self._resolve_data_pointer(int(value) >> 8)
                if int(value) >> 8
                else None,
                int(value) & 0xFF,
            )
            for value in encoded
        ]

    def _coerce_address(self, value):
        if isinstance(value, str):
            return ipaddress.ip_address(value)
        try:
            value.packed
            value.version
            return value
        except AttributeError as error:
            raise TypeError("argument 1 must be a string or ipaddress object") from error

    def _check_version(self, address, original) -> None:
        if address.version == 6 and self._metadata.ip_version == 4:
            raise ValueError(
                f"Error looking up {original}. You attempted to look up "
                "an IPv6 address in an IPv4-only database."
            )

    def _find_packed(self, packed: np.ndarray, bit_count: int) -> tuple[int, int]:
        self._ensure_readable()
        if (
            packed.dtype != np.uint8
            or packed.ndim != 1
            or not packed.flags.c_contiguous
            or bit_count not in (32, 128)
            or packed.size * 8 < bit_count
        ):
            raise ValueError("packed address must be contiguous uint8 IPv4 or IPv6 data")
        pointer = ctypes.c_int64()
        prefix = ctypes.c_int64()
        status = ctypes.c_int64()
        start_node = (
            self._ipv4_start
            if self._metadata.ip_version == 6 and bit_count == 32
            else 0
        )
        lib().mmd_find(
            self._address,
            self._buffer_size,
            packed.ctypes.data,
            bit_count,
            self._metadata.node_count,
            self._metadata.record_size,
            start_node,
            ctypes.addressof(pointer),
            ctypes.addressof(prefix),
            ctypes.addressof(status),
        )
        self._raise_status(int(status.value))
        return int(pointer.value), int(prefix.value)

    def _find_integer(self, address) -> tuple[int, int]:
        self._ensure_readable()
        bit_count = address.max_prefixlen
        value = int(address)
        encoded = lib().mmd_find_value(
            self._address,
            self._buffer_size,
            value >> 64,
            value & ((1 << 64) - 1),
            bit_count,
            self._metadata.node_count,
            self._metadata.record_size,
            (
                self._ipv4_start
                if self._metadata.ip_version == 6 and bit_count == 32
                else 0
            ),
        )
        if encoded < 0:
            self._raise_status(int(encoded))
        return int(encoded >> 8), int(encoded & 0xFF)

    def _find_address_in_tree(self, packed: bytearray) -> tuple[int, int]:
        array = np.frombuffer(packed, dtype=np.uint8)
        return self._find_packed(array, len(packed) * 8)

    @staticmethod
    def _raise_statuses(statuses: np.ndarray) -> None:
        failures = statuses[statuses != 0]
        if failures.size:
            Reader._raise_status(int(failures[0]))

    @staticmethod
    def _raise_status(status: int) -> None:
        if status == -1:
            raise InvalidDatabaseError("Unknown record size")
        if status == -2:
            raise InvalidDatabaseError("The MaxMind DB file's search tree is corrupt")
        if status == -3:
            raise InvalidDatabaseError("Invalid node in search tree")
        if status:
            raise InvalidDatabaseError("Unknown MMDB traversal error")

    def _read_node(self, node_number: int, index: int) -> int:
        self._ensure_readable()
        if node_number < 0 or node_number >= self._metadata.node_count:
            raise InvalidDatabaseError("The MaxMind DB file's search tree is corrupt")
        if index not in (0, 1):
            raise InvalidDatabaseError("The MaxMind DB file's search tree is corrupt")
        offset = node_number * self._metadata.node_byte_size
        record_size = self._metadata.record_size
        if record_size == 24:
            start = offset + index * 3
            raw = self._buffer[start : start + 3]
            if len(raw) != 3:
                raise InvalidDatabaseError("The MaxMind DB file's search tree is corrupt")
            return int.from_bytes(raw, "big")
        if record_size == 28:
            raw = self._buffer[offset : offset + 7]
            if len(raw) != 7:
                raise InvalidDatabaseError("The MaxMind DB file's search tree is corrupt")
            if index == 0:
                return (
                    (raw[3] >> 4) << 24
                    | raw[0] << 16
                    | raw[1] << 8
                    | raw[2]
                )
            return (
                (raw[3] & 0x0F) << 24
                | raw[4] << 16
                | raw[5] << 8
                | raw[6]
            )
        if record_size == 32:
            start = offset + index * 4
            raw = self._buffer[start : start + 4]
            if len(raw) != 4:
                raise InvalidDatabaseError("The MaxMind DB file's search tree is corrupt")
            return int.from_bytes(raw, "big")
        raise InvalidDatabaseError(f"Unknown record size: {record_size}")

    def _resolve_data_pointer(self, pointer: int):
        cached = self._decode_cache.get(pointer)
        if cached is not None:
            return _clone_record(cached)

        resolved = (
            pointer
            - self._metadata.node_count
            + self._metadata.search_tree_size
        )
        if resolved < 0 or resolved >= self._buffer_size:
            raise InvalidDatabaseError("The MaxMind DB file's search tree is corrupt")
        value, _ = self._decoder.decode(resolved)
        if pointer in self._seen_pointers:
            if len(self._decode_cache) >= 256:
                del self._decode_cache[next(iter(self._decode_cache))]
            self._decode_cache[pointer] = _clone_record(value)
        else:
            if len(self._seen_pointers) >= 2048:
                self._seen_pointers.clear()
            self._seen_pointers.add(pointer)
        return value

    def __iter__(self) -> Iterator[tuple[ipaddress._BaseNetwork, Any]]:
        self._ensure_readable()
        return self._generate_children(0, 0, 0)

    def _ensure_readable(self) -> None:
        if self.closed and self._mode not in (MODE_MEMORY, MODE_FD):
            raise ValueError("Attempt to read from a closed MaxMind DB.")

    def _generate_children(self, node: int, depth: int, ip_accumulator: int):
        if ip_accumulator != 0 and node == self._ipv4_start:
            return
        node_count = self._metadata.node_count
        if node > node_count:
            bits = 128 if self._metadata.ip_version == 6 else 32
            ip_accumulator <<= bits - depth
            if ip_accumulator <= _IPV4_MAX_NUM and bits == 128:
                depth -= 96
            yield (
                ipaddress.ip_network((ip_accumulator, depth)),
                self._resolve_data_pointer(node),
            )
        elif node < node_count:
            left = self._read_node(node, 0)
            yield from self._generate_children(left, depth + 1, ip_accumulator << 1)
            right = self._read_node(node, 1)
            yield from self._generate_children(
                right, depth + 1, ip_accumulator << 1 | 1
            )

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self._mmap is not None:
            self._array = None
            try:
                self._mmap.close()
            except BufferError:
                pass

    def __enter__(self):
        if self.closed:
            raise ValueError("Attempt to reopen a closed MaxMind DB")
        return self

    def __exit__(self, *args) -> None:
        self.close()
