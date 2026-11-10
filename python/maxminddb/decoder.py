"""Decoder for MaxMind DB data-section values."""

from __future__ import annotations

import struct
from typing import Any

from .errors import InvalidDatabaseError


class Decoder:
    def __init__(
        self,
        database_buffer,
        pointer_base: int = 0,
        pointer_test: bool = False,
    ) -> None:
        self._buffer = database_buffer
        self._pointer_base = pointer_base
        self._pointer_test = pointer_test

    def _need(self, offset: int, size: int) -> None:
        if offset < 0 or size < 0 or offset + size > len(self._buffer):
            raise InvalidDatabaseError(
                "The MaxMind DB file's data section contains bad data "
                "(unknown data type or corrupt data)"
            )

    def _bytes(self, offset: int, size: int) -> bytes:
        # Matches upstream maxminddb/decoder.py, which slices the buffer for
        # value payloads: a declared size past the end truncates, it does not
        # raise. Only the control bytes themselves are bounds-checked.
        self._need(offset, 0)
        return bytes(self._buffer[offset : offset + size])

    def _unpack(self, fmt: str, offset: int, size: int):
        # Matches maxminddb/decoder.py `_decode_double` / `_decode_float`, which
        # call struct.unpack on the raw slice, so a short payload raises
        # struct.error rather than being zero-filled.
        self._need(offset, 0)
        return struct.unpack(fmt, self._buffer[offset : offset + size])[0], offset + size

    def decode(self, offset: int) -> tuple[Any, int]:
        return self._decode(offset, 0)

    def _decode(self, offset: int, depth: int) -> tuple[Any, int]:
        if depth > 512:
            raise InvalidDatabaseError("Too many nested values in data section")
        self._need(offset, 1)
        control = self._buffer[offset]
        new_offset = offset + 1
        type_number = control >> 5
        if not type_number:
            self._need(new_offset, 1)
            type_number = self._buffer[new_offset] + 7
            new_offset += 1
            # Matches maxminddb/decoder.py `_read_extended`, which rejects only
            # type numbers below 7: extension byte 0 is the map type.
            if type_number < 7:
                raise InvalidDatabaseError(
                    "Something went horribly wrong in the decoder. An "
                    f"extended type resolved to a type number < 8 ({type_number})"
                )

        size, new_offset = self._size(control, new_offset, type_number)

        if type_number == 1:
            return self._pointer(size, new_offset, depth)
        if type_number == 2:
            value = self._bytes(new_offset, size).decode("utf-8")
            return value, new_offset + size
        if type_number == 3:
            self._verify_size(size, 8)
            # Matches maxminddb/decoder.py `_decode_double`.
            return self._unpack("!d", new_offset, size)
        if type_number == 4:
            return self._bytes(new_offset, size), new_offset + size
        if type_number in (5, 6, 9, 10):
            raw = self._bytes(new_offset, size)
            return int.from_bytes(raw, "big"), new_offset + size
        if type_number == 7:
            result = {}
            for _ in range(size):
                key, new_offset = self._decode(new_offset, depth + 1)
                value, new_offset = self._decode(new_offset, depth + 1)
                result[key] = value
            return result, new_offset
        if type_number == 8:
            # Matches maxminddb/decoder.py `_decode_int32`: zero is zero, and a
            # shortened payload is left-padded to 4 bytes before a signed read.
            if size == 0:
                return 0, new_offset
            raw = self._bytes(new_offset, size)
            return struct.unpack("!i", bytes(raw).rjust(4, b"\x00"))[0], new_offset + size
        if type_number == 11:
            result = []
            for _ in range(size):
                value, new_offset = self._decode(new_offset, depth + 1)
                result.append(value)
            return result, new_offset
        if type_number == 14:
            return size != 0, new_offset
        if type_number == 15:
            self._verify_size(size, 4)
            # Matches maxminddb/decoder.py `_decode_float`.
            return self._unpack("!f", new_offset, size)
        raise InvalidDatabaseError(
            f"Unexpected type number ({type_number}) encountered"
        )

    def _pointer(self, size: int, offset: int, depth: int) -> tuple[Any, int]:
        pointer_size = (size >> 3) + 1
        raw = self._bytes(offset, pointer_size)
        new_offset = offset + pointer_size
        high = size & 0x7
        if pointer_size == 1:
            pointer = (high << 8 | raw[0]) + self._pointer_base
        elif pointer_size == 2:
            pointer = (
                high << 16 | raw[0] << 8 | raw[1]
            ) + 2048 + self._pointer_base
        elif pointer_size == 3:
            pointer = (
                high << 24 | raw[0] << 16 | raw[1] << 8 | raw[2]
            ) + 526336 + self._pointer_base
        elif pointer_size == 4:
            pointer = int.from_bytes(raw, "big") + self._pointer_base
        else:
            raise InvalidDatabaseError("Invalid pointer size in data section")
        if self._pointer_test:
            return pointer, new_offset
        value, _ = self._decode(pointer, depth + 1)
        return value, new_offset

    def _size(
        self, control: int, offset: int, type_number: int
    ) -> tuple[int, int]:
        size = control & 0x1F
        if type_number == 1 or size < 29:
            return size, offset
        if size == 29:
            self._need(offset, 1)
            return 29 + self._buffer[offset], offset + 1
        if size == 30:
            raw = self._bytes(offset, 2)
            return 285 + int.from_bytes(raw, "big"), offset + 2
        raw = self._bytes(offset, 3)
        return 65821 + int.from_bytes(raw, "big"), offset + 3

    @staticmethod
    def _verify_size(actual: int, expected: int) -> None:
        if actual != expected:
            raise InvalidDatabaseError(
                "The MaxMind DB file's data section contains bad data "
                "(unknown data type or corrupt data)"
            )
