"""QR code reader for camera snapshots, in pure Python.

Finds a QR code in a grayscale image and returns its text. It is meant for
spool labels (Spoolman's "web+spoolman:s-<id>" codes and links, OpenSpool
JSON, short text), so it reads versions 1 to 10: up to 271 bytes at the
lowest error correction level.

The steps follow ISO/IEC 18004 and the detector design ZXing made common:
binarize against local block thresholds, find the three finder patterns
along pixel rows and cross-check them vertically and diagonally, map the
module grid with a perspective transform (refined with the alignment
pattern from version 2 on), then read the format information, unmask,
de-interleave, correct each block with Reed-Solomon and decode the data
segments.

Python 3.8 compatible, standard library only. Rows of pixels are handled
with bytes.translate and re where possible, which run at C speed; the
Python-level loops are over blocks, runs and modules, not pixels."""

import operator
import re
from itertools import accumulate, repeat

MAX_VERSION = 10
# Below this many pixels per module a code is worth a sharper second look.
SMALL_MODULE = 3.
# How much flatter than wide (or narrower than tall) a finder may appear.
MAX_SQUASH = 4.


class QrError(Exception):
    pass


# --- GF(256) and Reed-Solomon ----------------------------------------------

_EXP = [0] * 512
_LOG = [0] * 256
_value = 1
for _i in range(255):
    _EXP[_i] = _value
    _LOG[_value] = _i
    _value <<= 1
    if _value & 0x100:
        _value ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _mul(a, b):
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _div(a, b):
    if a == 0:
        return 0
    return _EXP[(_LOG[a] - _LOG[b]) % 255]


def _eval_low(poly, x):
    """Evaluate a polynomial given lowest degree first."""
    result = 0
    for coef in reversed(poly):
        result = _mul(result, x) ^ coef
    return result


def _syndromes(msg, count):
    """S_j = R(a^j) for j < count; msg[0] is R's highest coefficient."""
    synd = []
    for j in range(count):
        x = _EXP[j]
        s = 0
        for c in msg:
            s = _mul(s, x) ^ c
        synd.append(s)
    return synd


def rs_correct(codewords, ec_count):
    """Correct one Reed-Solomon block (data codewords, then EC codewords)
    and return it as a list. Raises QrError when it cannot be corrected."""
    msg = list(codewords)
    n = len(msg)
    synd = _syndromes(msg, ec_count)
    if not any(synd):
        return msg
    # Berlekamp-Massey: the error locator, lowest degree first.
    locator, previous = [1], [1]
    errors, shift, last = 0, 1, 1
    for k in range(ec_count):
        delta = synd[k]
        for i in range(1, min(errors, len(locator) - 1) + 1):
            delta ^= _mul(locator[i], synd[k - i])
        if delta == 0:
            shift += 1
            continue
        coef = _div(delta, last)
        saved = list(locator)
        if len(locator) < len(previous) + shift:
            locator += [0] * (len(previous) + shift - len(locator))
        for i, value in enumerate(previous):
            locator[i + shift] ^= _mul(coef, value)
        if 2 * errors <= k:
            errors = k + 1 - errors
            previous, last, shift = saved, delta, 1
        else:
            shift += 1
    if 2 * errors > ec_count:
        raise QrError("too many errors")
    # Chien search: position k holds the coefficient of x^(n-1-k).
    positions = [k for k in range(n)
                 if _eval_low(locator, _EXP[(255 - (n - 1 - k)) % 255]) == 0]
    if len(positions) != errors:
        raise QrError("error locations not found")
    # Forney, for codes whose generator roots start at a^0:
    # e = X * Omega(1/X) / Lambda'(1/X).
    omega = [0] * ec_count
    for i, s in enumerate(synd):
        if s:
            for j, c in enumerate(locator[:ec_count - i]):
                omega[i + j] ^= _mul(s, c)
    derivative = [locator[i] if i % 2 else 0 for i in range(1, len(locator))]
    for k in positions:
        degree = n - 1 - k
        x_inv = _EXP[(255 - degree) % 255]
        denominator = _eval_low(derivative, x_inv)
        if denominator == 0:
            raise QrError("error value not found")
        msg[k] ^= _mul(_EXP[degree % 255], _div(_eval_low(omega, x_inv), denominator))
    if any(_syndromes(msg, ec_count)):
        raise QrError("correction failed")
    return msg


# --- Symbol structure ------------------------------------------------------

# Per version and level: (EC codewords per block, [(block count, data
# codewords per block), ...]).
_BLOCKS = {
    1: {"L": (7, [(1, 19)]), "M": (10, [(1, 16)]), "Q": (13, [(1, 13)]), "H": (17, [(1, 9)])},
    2: {"L": (10, [(1, 34)]), "M": (16, [(1, 28)]), "Q": (22, [(1, 22)]), "H": (28, [(1, 16)])},
    3: {"L": (15, [(1, 55)]), "M": (26, [(1, 44)]), "Q": (18, [(2, 17)]), "H": (22, [(2, 13)])},
    4: {"L": (20, [(1, 80)]), "M": (18, [(2, 32)]), "Q": (26, [(2, 24)]), "H": (16, [(4, 9)])},
    5: {"L": (26, [(1, 108)]), "M": (24, [(2, 43)]), "Q": (18, [(2, 15), (2, 16)]),
        "H": (22, [(2, 11), (2, 12)])},
    6: {"L": (18, [(2, 68)]), "M": (16, [(4, 27)]), "Q": (24, [(4, 19)]), "H": (28, [(4, 15)])},
    7: {"L": (20, [(2, 78)]), "M": (18, [(4, 31)]), "Q": (18, [(2, 14), (4, 15)]),
        "H": (26, [(4, 13), (1, 14)])},
    8: {"L": (24, [(2, 97)]), "M": (22, [(2, 38), (2, 39)]), "Q": (22, [(4, 18), (2, 19)]),
        "H": (26, [(4, 14), (2, 15)])},
    9: {"L": (30, [(2, 116)]), "M": (22, [(3, 36), (2, 37)]), "Q": (20, [(4, 16), (4, 17)]),
        "H": (24, [(4, 12), (4, 13)])},
    10: {"L": (18, [(2, 68), (2, 69)]), "M": (26, [(4, 43), (1, 44)]),
         "Q": (24, [(6, 19), (2, 20)]), "H": (28, [(6, 15), (2, 16)])},
}

_ALIGNMENT = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
    7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50],
}

# Format information: the two level bits.
_LEVELS = {1: "L", 0: "M", 3: "Q", 2: "H"}

_MASKS = [
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
]


def _bch_remainder(value, poly):
    degree = poly.bit_length() - 1
    while value.bit_length() > degree:
        value ^= poly << (value.bit_length() - 1 - degree)
    return value


_FORMAT_CODES = [(((data << 10) | _bch_remainder(data << 10, 0x537)) ^ 0x5412, data)
                 for data in range(32)]
_VERSION_CODES = [((version << 12) | _bch_remainder(version << 12, 0x1F25), version)
                  for version in range(7, 41)]


def _nearest(codes, values, max_distance=3):
    """The data of the valid code closest (in bit errors) to any of values."""
    best, best_distance = None, max_distance + 1
    for value in values:
        for code, data in codes:
            distance = bin(code ^ value).count("1")
            if distance < best_distance:
                best, best_distance = data, distance
    return best


def _read_bits(bits, cells):
    value = 0
    for r, c in cells:
        value = (value << 1) | bits[r][c]
    return value


def _format_info(bits):
    """(level, mask) from the two format information copies."""
    dim = len(bits)
    first = [(8, c) for c in range(6)] + [(8, 7), (8, 8), (7, 8)] + [(r, 8) for r in range(5, -1, -1)]
    second = [(r, 8) for r in range(dim - 1, dim - 8, -1)] + [(8, c) for c in range(dim - 8, dim)]
    data = _nearest(_FORMAT_CODES, [_read_bits(bits, first), _read_bits(bits, second)])
    if data is None:
        raise QrError("unreadable format information")
    return _LEVELS[data >> 3], data & 7


def _version_info(bits):
    """Version from the version information blocks (version 7 and up), or None."""
    dim = len(bits)
    top_right = [(r, c) for r in range(5, -1, -1) for c in range(dim - 9, dim - 12, -1)]
    bottom_left = [(r, c) for c in range(5, -1, -1) for r in range(dim - 9, dim - 12, -1)]
    return _nearest(_VERSION_CODES, [_read_bits(bits, top_right), _read_bits(bits, bottom_left)])


_FUNCTION_MASKS = {}


def _function_mask(version):
    """True for modules that hold no data: finders, separators, format and
    version information, timing and alignment patterns."""
    if version in _FUNCTION_MASKS:
        return _FUNCTION_MASKS[version]
    dim = 17 + 4 * version
    mask = [[False] * dim for _ in range(dim)]

    def region(row, col, rows, cols):
        for r in range(row, row + rows):
            for c in range(col, col + cols):
                mask[r][c] = True

    region(0, 0, 9, 9)
    region(0, dim - 8, 9, 8)
    region(dim - 8, 0, 8, 9)
    centers = _ALIGNMENT[version]
    last = len(centers) - 1
    for i, r in enumerate(centers):
        for j, c in enumerate(centers):
            if (i == 0 and j in (0, last)) or (i == last and j == 0):
                continue
            region(r - 2, c - 2, 5, 5)
    region(6, 9, 1, dim - 17)
    region(9, 6, dim - 17, 1)
    if version >= 7:
        region(0, dim - 11, 6, 3)
        region(dim - 11, 0, 3, 6)
    _FUNCTION_MASKS[version] = mask
    return mask


def _read_codewords(bits, version, mask_id):
    """Unmask and read the codewords in their two-column zigzag order."""
    dim = len(bits)
    function = _function_mask(version)
    masked = _MASKS[mask_id]
    codewords = []
    current = count = 0
    upward = True
    col = dim - 1
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(dim - 1, -1, -1) if upward else range(dim)
        for r in rows:
            for c in (col, col - 1):
                if function[r][c]:
                    continue
                current = (current << 1) | (bits[r][c] ^ masked(r, c))
                count += 1
                if count == 8:
                    codewords.append(current)
                    current = count = 0
        upward = not upward
        col -= 2
    return codewords


class _BitReader(object):
    def __init__(self, data):
        self.data = data
        self.pos = 0

    def left(self):
        return len(self.data) * 8 - self.pos

    def read(self, count):
        if count > self.left():
            raise QrError("data ends early")
        value = 0
        for _ in range(count):
            byte = self.data[self.pos >> 3]
            value = (value << 1) | ((byte >> (7 - (self.pos & 7))) & 1)
            self.pos += 1
        return value


_ALPHANUMERIC = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ $%*+-./:"
_ECI_ENCODINGS = {
    1: "latin-1", 3: "latin-1", 4: "iso8859-2", 5: "iso8859-3", 6: "iso8859-4",
    7: "iso8859-5", 8: "iso8859-6", 9: "iso8859-7", 10: "iso8859-8", 11: "iso8859-9",
    13: "iso8859-11", 15: "iso8859-13", 16: "iso8859-14", 17: "iso8859-15",
    18: "iso8859-16", 20: "shift_jis", 21: "cp1250", 22: "cp1251", 23: "cp1252",
    24: "cp1256", 25: "utf-16-be", 26: "utf-8", 27: "ascii", 28: "big5",
    29: "gb18030", 30: "euc-kr",
}


def _decode_text(raw, encoding):
    try:
        return raw.decode(encoding, "replace")
    except LookupError:
        # Kiln OS's Python is built without the CJK codecs (shift_jis, big5,
        # ...); spool labels are ASCII, so the rest of the code still reads.
        return raw.decode("utf-8", "replace")


def _decode_bytes(raw, encoding):
    if encoding:
        return _decode_text(raw, encoding)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _decode_segments(data, version):
    reader = _BitReader(data)
    numeric_bits, alpha_bits, byte_bits, kanji_bits = (
        (10, 9, 8, 8) if version <= 9 else (12, 11, 16, 10))
    parts = []
    encoding = None
    while reader.left() >= 4:
        mode = reader.read(4)
        if mode == 0:
            break
        if mode == 1:
            count = reader.read(numeric_bits)
            digits = []
            while count >= 3:
                value = reader.read(10)
                if value > 999:
                    raise QrError("bad numeric data")
                digits.append("%03d" % value)
                count -= 3
            if count == 2:
                value = reader.read(7)
                if value > 99:
                    raise QrError("bad numeric data")
                digits.append("%02d" % value)
            elif count == 1:
                value = reader.read(4)
                if value > 9:
                    raise QrError("bad numeric data")
                digits.append("%d" % value)
            parts.append("".join(digits))
        elif mode == 2:
            count = reader.read(alpha_bits)
            chars = []
            while count >= 2:
                value = reader.read(11)
                if value >= 45 * 45:
                    raise QrError("bad alphanumeric data")
                chars.append(_ALPHANUMERIC[value // 45] + _ALPHANUMERIC[value % 45])
                count -= 2
            if count:
                value = reader.read(6)
                if value >= 45:
                    raise QrError("bad alphanumeric data")
                chars.append(_ALPHANUMERIC[value])
            parts.append("".join(chars))
        elif mode == 4:
            count = reader.read(byte_bits)
            parts.append(_decode_bytes(bytes(reader.read(8) for _ in range(count)), encoding))
        elif mode == 8:
            count = reader.read(kanji_bits)
            raw = bytearray()
            for _ in range(count):
                value = reader.read(13)
                code = ((value // 0xC0) << 8) | (value % 0xC0)
                code += 0x8140 if code < 0x1F00 else 0xC140
                raw += bytes([code >> 8, code & 0xFF])
            parts.append(_decode_text(bytes(raw), "shift_jis"))
        elif mode == 7:
            first = reader.read(8)
            if first & 0x80 == 0:
                eci = first
            elif first & 0xC0 == 0x80:
                eci = ((first & 0x3F) << 8) | reader.read(8)
            else:
                eci = ((first & 0x1F) << 16) | reader.read(16)
            encoding = _ECI_ENCODINGS.get(eci)
        elif mode == 3:
            reader.read(16)            # structured append header
        elif mode == 5:
            pass                       # FNC1 in first position
        elif mode == 9:
            reader.read(8)             # FNC1 in second position
        else:
            raise QrError("unknown data mode %d" % mode)
    return "".join(parts)


def decode_matrix(bits):
    """Text of a QR code given as rows of modules (1 = dark), without the
    quiet zone. Raises QrError."""
    dim = len(bits)
    if dim < 21 or (dim - 17) % 4 or any(len(row) != dim for row in bits):
        raise QrError("not a QR code size")
    version = (dim - 17) // 4
    if version > MAX_VERSION:
        raise QrError("version %d is larger than this reader supports" % version)
    level, mask_id = _format_info(bits)
    ec_count, groups = _BLOCKS[version][level]
    sizes = []
    for count, data_count in groups:
        sizes += [data_count] * count
    raw = _read_codewords(bits, version, mask_id)
    if len(raw) < sum(sizes) + ec_count * len(sizes):
        raise QrError("too few codewords")
    blocks = [[] for _ in sizes]
    pos = 0
    for i in range(max(sizes)):
        for b, size in enumerate(sizes):
            if i < size:
                blocks[b].append(raw[pos])
                pos += 1
    for _ in range(ec_count):
        for block in blocks:
            block.append(raw[pos])
            pos += 1
    data = []
    for block, size in zip(blocks, sizes):
        data += rs_correct(block, ec_count)[:size]
    return _decode_segments(data, version)


# --- Image: binarization ---------------------------------------------------

# Rows of pixels are handled as big integers with one 16-bit lane per
# pixel ("SIMD within a register"): adding, comparing and shifting whole
# rows then runs in C, a few dozen operations per row instead of a Python
# loop over pixels. Lanes never carry into each other as long as every lane
# result stays in 0..65535, which the callers ensure.

class _Lanes(object):
    """Constants for rows of a given width."""

    def __init__(self, width):
        self.width = width
        self.ones = int.from_bytes(b"\x01\x00" * width, "little")
        self.k255 = self.ones * 255
        self.k256 = self.ones * 256
        self.buffer = bytearray(2 * width)

    def widen(self, row):
        self.buffer[0::2] = row
        return int.from_bytes(self.buffer, "little")

    def narrow(self, value):
        """Low bytes of each lane (lanes must hold 0..255)."""
        return value.to_bytes(2 * self.width, "little")[0::2]

    def min_max(self, a, b):
        """Lane-wise (min, max) of rows whose lanes hold 0..255."""
        diff = a + self.k256 - b                 # 256 + a - b, in 1..511
        mask = ((diff >> 8) & self.ones) * 255   # 255 where a >= b
        delta = diff & mask                      # a - b where a >= b, else 0
        return a - delta, b + delta


def binarize(pixels, width, height, block=8, min_contrast=24):
    """Rows of 1 (dark) and 0 (light), each pixel against a threshold from
    the 5x5 blocks around it. Only blocks with some contrast count: flat
    ones (quiet zone, the inside of large modules) take the threshold of
    the nearest blocks that have it, so they do not drag the threshold of a
    code's edge towards their own level."""
    bw = (width + block - 1) // block
    bh = (height + block - 1) // block
    padded = bw * block
    rows = []
    for y in range(bh * block):
        row = bytes(pixels[min(y, height - 1) * width:(min(y, height - 1) + 1) * width])
        if padded > width:
            row += row[-1:] * (padded - width)
        rows.append(row)
    lanes = _Lanes(padded)
    wide = [lanes.widen(row) for row in rows]
    # Block sums, minimums and maximums: combine the band's rows lane-wise,
    # then fold each run of `block` lanes into its first lane.
    sums, counts = [], []
    area = block * block
    for by in range(bh):
        band = wide[by * block:(by + 1) * block]
        total = low = high = band[0]
        for value in band[1:]:
            total += value
            low, _ = lanes.min_max(low, value)
            _, high = lanes.min_max(high, value)
        shift = 16
        while shift < 16 * block:
            total += total >> shift
            low, _ = lanes.min_max(low, low >> shift)
            _, high = lanes.min_max(high, high >> shift)
            shift *= 2
        data = total.to_bytes(2 * padded, "little")
        totals = map(operator.or_, data[0::2 * block],
                     map(operator.lshift, data[1::2 * block], repeat(8)))
        lows = lanes.narrow(low)[0::block]
        highs = lanes.narrow(high)[0::block]
        flags = [hi - lo > min_contrast for lo, hi in zip(lows, highs)]
        sums.append(list(map(operator.mul, totals, flags)))
        counts.append(flags)
    # 5x5 block neighbourhood totals, as box sums: across, then down.
    upper = operator.itemgetter(*[min(bw, bx + 3) for bx in range(bw)])
    lower = operator.itemgetter(*[max(0, bx - 2) for bx in range(bw)])

    def box(grid):
        prefix = [0] * bw
        running = [prefix]
        for line in grid:
            cumulative = [0]
            cumulative.extend(accumulate(line))
            prefix = list(map(operator.add, prefix,
                              map(operator.sub, upper(cumulative), lower(cumulative))))
            running.append(prefix)
        return [list(map(operator.sub, running[min(bh, by + 3)], running[max(0, by - 2)]))
                for by in range(bh)]

    box_sums, box_counts = box(sums), box(counts)
    thresholds = []
    unknown = []
    for by in range(bh):
        line = [s // (c * area) if c else None for s, c in zip(box_sums[by], box_counts[by])]
        unknown.extend((by, bx) for bx, value in enumerate(line) if value is None)
        thresholds.append(line)
    # Spread known thresholds into flat areas, ring by ring.
    while unknown:
        filled = []
        for by, bx in unknown:
            near = [thresholds[y][x] for y, x in ((by - 1, bx), (by + 1, bx), (by, bx - 1), (by, bx + 1))
                    if 0 <= y < bh and 0 <= x < bw and thresholds[y][x] is not None]
            if near:
                filled.append((by, bx, sum(near) // len(near)))
        if not filled:
            for by, bx in unknown:
                thresholds[by][bx] = 0         # no contrast anywhere: all light
            break
        for by, bx, value in filled:
            thresholds[by][bx] = value
        unknown = [cell for cell in unknown if thresholds[cell[0]][cell[1]] is None]
    # Dark where pixel < threshold: lane 255 + t - p has bit 8 set exactly then.
    out = []
    expanded = bytearray(padded)
    for by in range(bh):
        line = bytes(thresholds[by])
        for k in range(block):
            expanded[k::block] = line
        limit = lanes.widen(expanded) + lanes.k255
        for y in range(by * block, min(height, (by + 1) * block)):
            dark = ((limit - wide[y]) >> 8) & lanes.ones
            out.append(lanes.narrow(dark)[:width])
    return out


# --- Image: finder patterns ------------------------------------------------

_RUNS = re.compile(b"\x01+|\x00+")


def _ratio_ok(counts, tolerance=0.5):
    total = sum(counts)
    if total < 7:
        return False
    module = total / 7.
    variance = module * tolerance
    return (abs(module - counts[0]) < variance and abs(module - counts[1]) < variance
            and abs(3 * module - counts[2]) < 3 * variance
            and abs(module - counts[3]) < variance and abs(module - counts[4]) < variance)


def _cross_check(img, width, height, x, y, dx, dy, max_count, tolerance=0.5):
    """Runs of a 1:1:3:1:1 pattern through pixel (x, y), whose centre run
    must be dark, along (dx, dy). Returns (offset of the centre run's middle
    from the pixel's edge, total length) or None."""
    counts = [0] * 5
    px, py = x, y
    back = 0
    for state, dark in ((2, 1), (1, 0), (0, 1)):
        while 0 <= px < width and 0 <= py < height and img[py][px] == dark:
            counts[state] += 1
            if state == 2:
                back += 1
            elif counts[state] > max_count:
                return None
            px -= dx
            py -= dy
        if state > 0 and not (0 <= px < width and 0 <= py < height):
            return None
    px, py = x + dx, y + dy
    forward = 0
    for state, dark in ((2, 1), (3, 0), (4, 1)):
        while 0 <= px < width and 0 <= py < height and img[py][px] == dark:
            counts[state] += 1
            if state == 2:
                forward += 1
            elif counts[state] > max_count:
                return None
            px += dx
            py += dy
        if state < 4 and not (0 <= px < width and 0 <= py < height):
            return None
    if not _ratio_ok(counts, tolerance):
        return None
    # The centre run covers pixels -(back - 1) .. forward along the line.
    return (forward - back + 1) / 2. + 0.5, sum(counts)


class _Finder(object):
    """A finder pattern centre, with its module size across (mx) and down
    (my): a label lying on the bed is foreshortened, so they can differ."""

    def __init__(self, x, y, mx, my):
        self.x, self.y, self.mx, self.my, self.count = x, y, mx, my, 1

    @property
    def module(self):
        return (self.mx + self.my) / 2.

    def module_towards(self, other):
        """Rough module size along the line to another finder."""
        dx, dy = other.x - self.x, other.y - self.y
        length = (dx * dx + dy * dy) ** 0.5 or 1.
        return ((self.mx * dx / length) ** 2 + (self.my * dy / length) ** 2) ** 0.5

    def near(self, x, y, module):
        if abs(self.x - x) <= module and abs(self.y - y) <= module:
            difference = abs(module - self.module)
            return difference <= 1 or difference <= self.module
        return False

    def merge(self, x, y, mx, my):
        n = self.count
        self.x = (self.x * n + x) / (n + 1)
        self.y = (self.y * n + y) / (n + 1)
        self.mx = (self.mx * n + mx) / (n + 1)
        self.my = (self.my * n + my) / (n + 1)
        self.count = n + 1


def find_finders(img, width, height, row_step=2):
    finders = []
    for y in range(0, height, row_step):
        row = img[y]
        starts, lengths = [], []
        for match in _RUNS.finditer(row):
            starts.append(match.start())
            lengths.append(match.end() - match.start())
        if len(lengths) < 5:
            continue
        for i in range(0 if row[0] else 1, len(lengths) - 4, 2):
            counts = lengths[i:i + 5]
            if not _ratio_ok(counts):
                continue
            total = sum(counts)
            cx = starts[i + 2] + counts[2] / 2.
            # Down may be foreshortened against across (a label lying on the
            # bed, seen from a low camera), up to MAX_SQUASH either way.
            vertical = _cross_check(img, width, height, int(cx), y, 0, 1, counts[2] * 2)
            if vertical is None or not total / MAX_SQUASH <= vertical[1] <= total * MAX_SQUASH:
                continue
            cy = y + vertical[0]
            horizontal = _cross_check(img, width, height, int(cx), int(cy), 1, 0, counts[2] * 2)
            if horizontal is None or 5 * abs(horizontal[1] - total) >= 2 * total:
                continue
            cx = int(cx) + horizontal[0]
            if _cross_check(img, width, height, int(cx), int(cy), 1, 1, width, 0.75) is None:
                continue
            mx, my = horizontal[1] / 7., vertical[1] / 7.
            for finder in finders:
                if finder.near(cx, cy, (mx + my) / 2.):
                    finder.merge(cx, cy, mx, my)
                    break
            else:
                finders.append(_Finder(cx, cy, mx, my))
    return finders


def _distance2(a, b):
    return (a.x - b.x) ** 2 + (a.y - b.y) ** 2


def _chord(img, width, height, finder, other):
    """Length in pixels of the finder's 1:1:3:1:1 pattern along the line to
    another finder, or None. The line between two finder centres runs along
    one of the code's axes or diagonals, so this chord is the same number
    of the code's own units for every finder of a code, whatever the
    rotation, tilt or foreshortening."""
    dx, dy = other.x - finder.x, other.y - finder.y
    length = (dx * dx + dy * dy) ** 0.5
    if not length:
        return None
    ux, uy = dx / length, dy / length
    counts = [0] * 5
    for sign, states in ((-1, (2, 1, 0)), (1, (2, 3, 4))):
        t = 0. if sign < 0 else 1.
        for state in states:
            dark = 1 if state % 2 == 0 else 0
            while True:
                pixel = _pixel(img, width, height, finder.x + sign * t * ux, finder.y + sign * t * uy)
                if pixel != dark:
                    break
                counts[state] += 1
                t += 1.
            if pixel < 0 and state not in (0, 4):
                return None
    if not _ratio_ok(counts, 0.75):
        return None
    return float(sum(counts))


class _Geometry(object):
    """Finder distances in units of their chords, cached per pair."""

    def __init__(self, img, width, height):
        self.img, self.width, self.height = img, width, height
        self.chords = {}

    def chord(self, finder, other):
        key = (id(finder), id(other))
        if key not in self.chords:
            chord = _chord(self.img, self.width, self.height, finder, other)
            if chord is None:
                chord = 7 * finder.module_towards(other)
            self.chords[key] = chord
        return self.chords[key]

    def apart(self, a, b):
        """Distance between two finders in sevenths of their chords: dim - 7
        for any two finders of one code (chords along a diagonal and the
        diagonal itself are both sqrt(2) longer)."""
        return 7 * (_distance2(a, b) ** 0.5) / ((self.chord(a, b) + self.chord(b, a)) / 2.)


def _ordered_triples(geometry, finders, limit=8):
    """Likely (top-left, top-right, bottom-left) finder triples, best first.
    The three finders of a code are equally far apart in chord units. Which
    one is the top-left corner is tried every way, nearest a right angle in
    the picture first."""
    confirmed = [f for f in finders if f.count >= 2]
    pool = sorted(confirmed if len(confirmed) >= 3 else finders,
                  key=lambda f: -f.count)[:limit]
    scored = []
    for i in range(len(pool)):
        for j in range(i + 1, len(pool)):
            for k in range(j + 1, len(pool)):
                trio = (pool[i], pool[j], pool[k])
                sizes_ok = True
                for axis in ("mx", "my"):
                    sizes = [getattr(f, axis) for f in trio]
                    if max(sizes) > 1.8 * min(sizes):
                        sizes_ok = False
                if not sizes_ok:
                    continue
                sides = [geometry.apart(trio[1], trio[2]), geometry.apart(trio[0], trio[2]),
                         geometry.apart(trio[0], trio[1])]
                mean = sum(sides) / 3.
                # Finder centres are dim - 7 modules apart: 14 at version 1.
                if not 11 <= mean <= 4 * MAX_VERSION + 15:
                    continue
                spread = (max(sides) - min(sides)) / mean
                if spread > 0.3:
                    continue
                for corner in range(3):
                    tl = trio[corner]
                    p, q = [f for n, f in enumerate(trio) if n != corner]
                    ax, ay, bx, by = p.x - tl.x, p.y - tl.y, q.x - tl.x, q.y - tl.y
                    cross = ax * by - ay * bx
                    cosine = abs(ax * bx + ay * by) / ((ax * ax + ay * ay) * (bx * bx + by * by)) ** 0.5
                    tr, bl = (p, q) if cross > 0 else (q, p)
                    scored.append((spread + cosine, tl, tr, bl))
    scored.sort(key=lambda item: item[0])
    return [item[1:] for item in scored]


# --- Image: grid sampling --------------------------------------------------

def _homography(src, dst):
    """3x3 matrix (as 8 numbers) mapping four src points onto four dst points."""
    rows = []
    for (x, y), (u, v) in zip(src, dst):
        rows.append([x, y, 1., 0., 0., 0., -x * u, -y * u, u])
        rows.append([0., 0., 0., x, y, 1., -x * v, -y * v, v])
    for col in range(8):
        pivot = max(range(col, 8), key=lambda r: abs(rows[r][col]))
        if abs(rows[pivot][col]) < 1e-9:
            raise QrError("degenerate transform")
        rows[col], rows[pivot] = rows[pivot], rows[col]
        for r in range(8):
            if r != col and rows[r][col]:
                factor = rows[r][col] / rows[col][col]
                rows[r] = [a - factor * b for a, b in zip(rows[r], rows[col])]
    return [rows[i][8] / rows[i][i] for i in range(8)]


def _apply(h, x, y):
    w = h[6] * x + h[7] * y + 1.
    return (h[0] * x + h[1] * y + h[2]) / w, (h[3] * x + h[4] * y + h[5]) / w


def _transform(dim, tl, tr, bl, br=None, br_module=None):
    src = [(3.5, 3.5), (dim - 3.5, 3.5), (3.5, dim - 3.5)]
    dst = [(tl.x, tl.y), (tr.x, tr.y), (bl.x, bl.y)]
    if br is None:
        src.append((dim - 3.5, dim - 3.5))
        dst.append((tr.x + bl.x - tl.x, tr.y + bl.y - tl.y))
    else:
        src.append((br_module, br_module))
        dst.append(br)
    return _homography(src, dst)


def _sample(img, width, height, h, dim):
    bits = []
    for r in range(dim):
        row = []
        for c in range(dim):
            x, y = _apply(h, c + 0.5, r + 0.5)
            xi, yi = int(x), int(y)
            if not (0 <= xi < width and 0 <= yi < height):
                raise QrError("code extends past the image")
            row.append(img[yi][xi])
        bits.append(row)
    return bits


def _pixel(img, width, height, x, y):
    xi, yi = int(x), int(y)
    if 0 <= xi < width and 0 <= yi < height:
        return img[yi][xi]
    return -1


def _timing_score(img, width, height, h, dim):
    good = total = 0
    for i in range(8, dim - 8):
        expected = 1 if i % 2 == 0 else 0
        for c, r in ((i, 6), (6, i)):
            x, y = _apply(h, c + 0.5, r + 0.5)
            good += _pixel(img, width, height, x, y) == expected
            total += 1
    return good / float(total) if total else 0.


def _find_alignment(img, width, height, dim, tl, tr, bl, h, radius_modules=4.):
    """Centre of the bottom-right alignment pattern within radius_modules of
    where the finder patterns put it, by matching its 5x5 module template."""
    span = float(dim - 7)
    ux, uy = (tr.x - tl.x) / span, (tr.y - tl.y) / span
    vx, vy = (bl.x - tl.x) / span, (bl.y - tl.y) / span
    module = ((ux * ux + uy * uy) ** 0.5 + (vx * vx + vy * vy) ** 0.5) / 2.
    ex, ey = _apply(h, dim - 6.5, dim - 6.5)
    cells = [(dc * ux + dr * vx, dc * uy + dr * vy, 0 if max(abs(dr), abs(dc)) == 1 else 1)
             for dr in range(-2, 3) for dc in range(-2, 3)]
    radius = max(4., radius_modules * module)
    step = max(1., module / 3.)
    steps = int(radius / step)
    best, best_score = [], 22
    for iy in range(-steps, steps + 1):
        for ix in range(-steps, steps + 1):
            cx, cy = ex + ix * step, ey + iy * step
            score = 0
            for ox, oy, expected in cells:
                if _pixel(img, width, height, cx + ox, cy + oy) == expected:
                    score += 1
            if score > best_score:
                best, best_score = [(cx, cy)], score
            elif score == best_score:
                best.append((cx, cy))
    if not best:
        return None
    # Average the best positions, nearest the estimate first.
    best.sort(key=lambda p: (p[0] - ex) ** 2 + (p[1] - ey) ** 2)
    best = best[:9]
    return sum(p[0] for p in best) / len(best), sum(p[1] for p in best) / len(best)


def _dimensions(geometry, tl, tr, bl):
    """Likely symbol sizes, nearest the estimate first."""
    estimate = (geometry.apart(tl, tr) + geometry.apart(tl, bl)) / 2. + 7
    nearest = sorted(range(21, 17 + 4 * MAX_VERSION + 1, 4), key=lambda d: abs(d - estimate))
    return nearest[:3]


def _decode_grid(img, width, height, h, dim):
    bits = _sample(img, width, height, h, dim)
    try:
        return decode_matrix(bits)
    except (QrError, IndexError, KeyError):
        mirrored = [list(row) for row in zip(*bits)]
        return decode_matrix(mirrored)


def _transforms(img, width, height, dim, tl, tr, bl):
    """Grid transforms to try for one size: through the alignment pattern
    when there is one and it is found, then the finders alone."""
    h = _transform(dim, tl, tr, bl)
    if dim > 21:
        for radius in (4., 9.):
            align = _find_alignment(img, width, height, dim, tl, tr, bl, h, radius)
            if align is not None:
                yield _transform(dim, tl, tr, bl, align, dim - 6.5)
                break
    yield h


def _decode_triple(img, width, height, geometry, tl, tr, bl):
    dims = []
    for dim in _dimensions(geometry, tl, tr, bl):
        h = _transform(dim, tl, tr, bl)
        dims.append((-_timing_score(img, width, height, h, dim), dim))
    queue = [dim for _, dim in sorted(dims)]
    tried = set()
    while queue:
        dim = queue.pop(0)
        if dim in tried:
            continue
        tried.add(dim)
        for h in _transforms(img, width, height, dim, tl, tr, bl):
            try:
                return _decode_grid(img, width, height, h, dim)
            except (QrError, IndexError, KeyError):
                pass
            if dim >= 45:
                # Version 7 and up state their version: try that size next.
                try:
                    stated = _version_info(_sample(img, width, height, h, dim))
                except QrError:
                    stated = None
                if stated and stated <= MAX_VERSION:
                    queue.insert(0, 17 + 4 * stated)
    return None


def _small_pairs(finders, width, height, limit=2):
    """Boxes around pairs of small finders the right distance apart for one
    code. At low resolution the third finder of a small code is often
    missed; the box spans the pair's distance on every side, since the code
    may lie on either side of the line between them."""
    tiny = sorted((f for f in finders if min(f.mx, f.my) < SMALL_MODULE),
                  key=lambda f: -f.count)[:8]
    boxes = []
    for i in range(len(tiny)):
        for j in range(i + 1, len(tiny)):
            a, b = tiny[i], tiny[j]
            distance = _distance2(a, b) ** 0.5
            modules = distance / ((a.module_towards(b) + b.module_towards(a)) / 2.)
            if not 12 <= modules <= 1.5 * (4 * MAX_VERSION + 10):
                continue
            reach = distance + 6 * max(a.module, b.module)
            boxes.append((max(0, min(a.x, b.x) - reach), max(0, min(a.y, b.y) - reach),
                          min(width, max(a.x, b.x) + reach), min(height, max(a.y, b.y) + reach)))
            if len(boxes) >= limit:
                return boxes
    return boxes


def decode_image(pixels, width, height, max_triples=9, small=None):
    """Text of the first QR code found in an 8-bit grayscale image (one byte
    per pixel, rows top to bottom), or None.

    When small is a list, codes that were found but whose modules are too
    small to read at this resolution are added to it as (left, top, right,
    bottom) boxes, so the caller can try again with a sharper crop."""
    img = binarize(pixels, width, height)
    finders = find_finders(img, width, height)
    geometry = _Geometry(img, width, height)
    triples = _ordered_triples(geometry, finders)[:max_triples] if len(finders) >= 3 else []
    for tl, tr, bl in triples:
        try:
            text = _decode_triple(img, width, height, geometry, tl, tr, bl)
        except QrError:
            text = None
        if text is not None:
            return text
        if small is not None and min(min(f.mx, f.my) for f in (tl, tr, bl)) < SMALL_MODULE:
            # The fourth corner sits opposite the top-left finder; finder
            # centres are 3.5 modules in from the code's edges.
            br = (tr.x + bl.x - tl.x, tr.y + bl.y - tl.y)
            xs = [tl.x, tr.x, bl.x, br[0]]
            ys = [tl.y, tr.y, bl.y, br[1]]
            margin = 6 * max(f.module for f in (tl, tr, bl))
            small.append((max(0, min(xs) - margin), max(0, min(ys) - margin),
                          min(width, max(xs) + margin), min(height, max(ys) + margin)))
    if small is not None:
        small.extend(_small_pairs(finders, width, height))
    return None
