"""HKDF-SHA256 and AES-128-ECB in pure Python, for the vendor formats whose
MIFARE keys or payloads are derived or encrypted (Bambu, Snapmaker,
Creality). Kiln's Python has no cryptography package; these are only run
on a few blocks per scan, so speed does not matter."""

import hashlib
import hmac


def hkdf_sha256(ikm, salt, info, length):
    """RFC 5869 HKDF with SHA-256."""
    prk = hmac.new(salt or b"\x00" * 32, ikm, hashlib.sha256).digest()
    okm = b""
    block = b""
    counter = 1
    while len(okm) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        okm += block
        counter += 1
    return okm[:length]


def key_matches(key, expected_sha256):
    """True when key's SHA-256 is the published hash of the vendor's key.
    Keys are not shipped; users paste them into their config, and this
    catches typos before they turn into failed authentications."""
    return hashlib.sha256(key).hexdigest().lower() == expected_sha256.lower()


# --- AES-128 ---------------------------------------------------------------

def _build_sbox():
    sbox = [0] * 256
    p = q = 1
    while True:
        # p *= 3 in GF(2^8); q /= 3
        p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09
        x = q ^ (((q << 1) | (q >> 7)) & 0xFF) ^ (((q << 2) | (q >> 6)) & 0xFF) \
            ^ (((q << 3) | (q >> 5)) & 0xFF) ^ (((q << 4) | (q >> 4)) & 0xFF)
        sbox[p] = x ^ 0x63
        if p == 1:
            break
    sbox[0] = 0x63
    return sbox


_SBOX = _build_sbox()
_INV_SBOX = [0] * 256
for _i, _v in enumerate(_SBOX):
    _INV_SBOX[_v] = _i
_RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]


def _xtime(a):
    return ((a << 1) ^ 0x1B) & 0xFF if a & 0x80 else a << 1


def _mul(a, b):
    result = 0
    while b:
        if b & 1:
            result ^= a
        a = _xtime(a)
        b >>= 1
    return result


def _expand_key(key):
    if len(key) != 16:
        raise ValueError("AES-128 needs a 16-byte key")
    words = [list(key[i:i + 4]) for i in range(0, 16, 4)]
    for i in range(4, 44):
        word = list(words[i - 1])
        if i % 4 == 0:
            word = word[1:] + word[:1]
            word = [_SBOX[b] for b in word]
            word[0] ^= _RCON[i // 4 - 1]
        words.append([a ^ b for a, b in zip(words[i - 4], word)])
    return [sum(words[r * 4:r * 4 + 4], []) for r in range(11)]


def _add_round_key(state, round_key):
    return [s ^ k for s, k in zip(state, round_key)]


def _shift_rows(s, inverse=False):
    out = list(s)
    for row in range(1, 4):
        for col in range(4):
            src = (col + row) % 4 if not inverse else (col - row) % 4
            out[col * 4 + row] = s[src * 4 + row]
    return out


def _mix_columns(s, inverse=False):
    out = []
    coeffs = (14, 11, 13, 9) if inverse else (2, 3, 1, 1)
    for col in range(4):
        column = s[col * 4:col * 4 + 4]
        for row in range(4):
            out.append(_mul(column[0], coeffs[(0 - row) % 4])
                       ^ _mul(column[1], coeffs[(1 - row) % 4])
                       ^ _mul(column[2], coeffs[(2 - row) % 4])
                       ^ _mul(column[3], coeffs[(3 - row) % 4]))
    return out


def aes128_encrypt_block(key, block):
    keys = _expand_key(key)
    state = _add_round_key(list(block), keys[0])
    for rnd in range(1, 11):
        state = [_SBOX[b] for b in state]
        state = _shift_rows(state)
        if rnd != 10:
            state = _mix_columns(state)
        state = _add_round_key(state, keys[rnd])
    return bytes(state)


def aes128_decrypt_block(key, block):
    keys = _expand_key(key)
    state = _add_round_key(list(block), keys[10])
    for rnd in range(9, -1, -1):
        state = _shift_rows(state, inverse=True)
        state = [_INV_SBOX[b] for b in state]
        state = _add_round_key(state, keys[rnd])
        if rnd != 0:
            state = _mix_columns(state, inverse=True)
    return bytes(state)


def aes128_ecb(key, data, decrypt=False):
    if len(data) % 16:
        raise ValueError("AES-ECB data must be a multiple of 16 bytes")
    fn = aes128_decrypt_block if decrypt else aes128_encrypt_block
    return b"".join(fn(key, data[i:i + 16]) for i in range(0, len(data), 16))
