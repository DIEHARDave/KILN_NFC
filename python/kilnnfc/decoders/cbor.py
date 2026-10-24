"""Minimal CBOR (RFC 8949) decoder for OpenPrintTag: maps, arrays, strings,
integers, half/single/double floats, indefinite-length containers. Tags
(major type 6) are unwrapped to their content."""

import struct


class CborError(ValueError):
    pass


_BREAK = object()


def decode(data, offset=0):
    """(value, offset just past it)."""
    value, offset = _item(bytes(data), offset)
    if value is _BREAK:
        raise CborError("unexpected break")
    return value, offset


def _argument(data, pos, info):
    if info < 24:
        return info, pos
    sizes = {24: 1, 25: 2, 26: 4, 27: 8}
    if info not in sizes:
        raise CborError("bad additional info %d" % info)
    size = sizes[info]
    if pos + size > len(data):
        raise CborError("truncated")
    return int.from_bytes(data[pos:pos + size], "big"), pos + size


def _half(bits):
    sign = -1. if bits & 0x8000 else 1.
    exp = (bits >> 10) & 0x1F
    frac = bits & 0x3FF
    if exp == 0:
        return sign * frac * 2 ** -24
    if exp == 31:
        return sign * float("inf") if not frac else float("nan")
    return sign * (1 + frac / 1024.) * 2 ** (exp - 15)


def _item(data, pos):
    if pos >= len(data):
        raise CborError("truncated")
    initial = data[pos]
    pos += 1
    major, info = initial >> 5, initial & 0x1F

    if major == 7:
        if info == 31:
            return _BREAK, pos
        if info == 25:
            return _half(int.from_bytes(data[pos:pos + 2], "big")), pos + 2
        if info == 26:
            return struct.unpack(">f", data[pos:pos + 4])[0], pos + 4
        if info == 27:
            return struct.unpack(">d", data[pos:pos + 8])[0], pos + 8
        simple, pos = _argument(data, pos, info)
        return {20: False, 21: True, 22: None, 23: None}.get(simple), pos

    if info == 31 and major in (2, 3, 4, 5):
        return _indefinite(data, pos, major)

    arg, pos = _argument(data, pos, info)
    if major == 0:
        return arg, pos
    if major == 1:
        return -1 - arg, pos
    if major in (2, 3):
        if pos + arg > len(data):
            raise CborError("truncated string")
        raw = data[pos:pos + arg]
        return (raw if major == 2 else raw.decode("utf-8", "replace")), pos + arg
    if major == 4:
        items = []
        for _ in range(arg):
            value, pos = decode(data, pos)
            items.append(value)
        return items, pos
    if major == 5:
        result = {}
        for _ in range(arg):
            key, pos = decode(data, pos)
            value, pos = decode(data, pos)
            result[_hashable(key)] = value
        return result, pos
    # major 6: tagged item; the tag number is not needed here.
    return decode(data, pos)


def _indefinite(data, pos, major):
    if major in (2, 3):
        chunks = []
        while True:
            chunk, pos = _item(data, pos)
            if chunk is _BREAK:
                break
            chunks.append(chunk)
        joined = b"".join(c if isinstance(c, bytes) else c.encode() for c in chunks)
        return (joined if major == 2 else joined.decode("utf-8", "replace")), pos
    if major == 4:
        items = []
        while True:
            value, pos = _item(data, pos)
            if value is _BREAK:
                return items, pos
            items.append(value)
    result = {}
    while True:
        key, pos = _item(data, pos)
        if key is _BREAK:
            return result, pos
        value, pos = decode(data, pos)
        result[_hashable(key)] = value


def _hashable(key):
    return tuple(key) if isinstance(key, list) else key
