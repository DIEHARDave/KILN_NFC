import glob
import json
import os
import struct
import unittest

from helpers import (long_mime_record, mime_record, text_record, tlv, type2_tag,
                     uri_record)

from kilnnfc import decoders
from kilnnfc.decoders import cbor, crypto, ndef, vendors
from kilnnfc.tag import Tag

HERE = os.path.dirname(os.path.abspath(__file__))


class CryptoTest(unittest.TestCase):
    def test_aes_fips197(self):
        key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        plain = bytes.fromhex("00112233445566778899aabbccddeeff")
        cipher = crypto.aes128_encrypt_block(key, plain)
        self.assertEqual(cipher.hex(), "69c4e0d86a7b0430d8cdb78070b4c55a")
        self.assertEqual(crypto.aes128_decrypt_block(key, cipher), plain)
        self.assertEqual(crypto.aes128_ecb(key, crypto.aes128_ecb(key, plain * 3), True), plain * 3)

    def test_hkdf_rfc5869(self):
        okm = crypto.hkdf_sha256(b"\x0b" * 22, bytes.fromhex("000102030405060708090a0b0c"),
                                 bytes.fromhex("f0f1f2f3f4f5f6f7f8f9"), 42)
        self.assertEqual(okm.hex(), "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db0"
                                    "2d56ecc4c5bf34007208d5b887185865")


class CborTest(unittest.TestCase):
    def test_types(self):
        self.assertEqual(cbor.decode(bytes.fromhex("1903e8"))[0], 1000)
        self.assertEqual(cbor.decode(bytes.fromhex("20"))[0], -1)
        self.assertEqual(cbor.decode(bytes.fromhex("f93e00"))[0], 1.5)
        self.assertEqual(cbor.decode(bytes.fromhex("6449455446"))[0], "IETF")
        self.assertEqual(cbor.decode(bytes.fromhex("bf61610161629f0203ffff"))[0],
                         {"a": 1, "b": [2, 3]})
        value, end = cbor.decode(bytes.fromhex("a201020304ff"))
        self.assertEqual((value, end), ({1: 2, 3: 4}, 5))


class NdefTest(unittest.TestCase):
    def test_null_tlvs_and_records(self):
        message = text_record("hi", last=False) + uri_record(4, "example.com", first=False)
        tag = type2_tag()
        memory = bytearray(tag.memory)
        data = b"\x00\x00" + b"\x01\x03\xa0\x0c\x34" + tlv(message)
        memory[16:16 + len(data)] = data
        tag.memory = bytes(memory)
        records = ndef.tag_records(tag)
        self.assertEqual([r.text() for r in records[:1]], ["hi"])
        self.assertEqual(records[1].uri(), "https://example.com")

    def test_long_tlv(self):
        message = long_mime_record("application/x-test", b"x" * 400)
        tag = type2_tag(message, pages=231)
        records = ndef.tag_records(tag)
        self.assertEqual(len(records[0].payload), 400)

    def test_type5_8_byte_cc(self):
        message = text_record("PLA:FF0000")
        memory = bytes([0xE2, 0x40, 0x00, 0x01, 0x00, 0x00, 0x04, 0x00]) + tlv(message)
        tag = Tag(b"\xe0" * 8, "iso15693", "type5", memory.ljust(64, b"\x00"))
        filament, _ = decoders.decode(tag)
        self.assertEqual((filament.material, filament.color), ("PLA", "FF0000"))

    def test_mifare_classic_mad(self):
        memory = bytearray(1024)
        mad = bytearray(32)
        mad[2:4] = (0x03E1).to_bytes(2, "little")      # sector 1 = NDEF
        mad[4:6] = (0x03E1).to_bytes(2, "little")      # sector 2 = NDEF
        memory[16:48] = mad
        area = tlv(text_record("SPOOL:7"))
        memory[64:64 + len(area)] = area
        tag = Tag(b"\x01\x02\x03\x04", "iso14443a", "mifare_classic", bytes(memory), sak=0x08)
        tag.sectors_read = {0, 1, 2}
        filament, _ = decoders.decode(tag)
        self.assertEqual(filament.spool_id, 7)


class OpenFormatTest(unittest.TestCase):
    def test_openprinttag_vectors(self):
        expected = {
            "01_data.bin": ("PLA", "Prusament", "3D3E3D", 1012, 205, 225),
            "02_data.bin": ("PETG", "Prusament", "24292A", 1050, 240, 260),
            "sample_tag.bin": ("PLA", "Prusament", "3D3E3D", 1012, 205, 220),
        }
        for name, (material, brand, color, weight, nmin, nmax) in expected.items():
            with open(os.path.join(HERE, "vectors", "openprinttag", name), "rb") as handle:
                tag = Tag(b"\xe0\x04" + b"\x00" * 6, "iso15693", "type5", handle.read())
            filament, _ = decoders.decode(tag)
            self.assertEqual(filament.fmt, "OpenPrintTag", name)
            self.assertEqual((filament.material, filament.brand, filament.color,
                              filament.weight, filament.nozzle_min, filament.nozzle_max),
                             (material, brand, color, weight, nmin, nmax), name)

    def test_openprinttag_consumed_weight(self):
        with open(os.path.join(HERE, "vectors", "openprinttag", "03_data.bin"), "rb") as handle:
            tag = Tag(b"\xe0" * 8, "iso15693", "type5", handle.read())
        self.assertEqual(decoders.decode(tag)[0].weight, 1050 - 444)

    def test_all_vectors_parse(self):
        for path in glob.glob(os.path.join(HERE, "vectors", "openprinttag", "*.bin")):
            with open(path, "rb") as handle:
                tag = Tag(b"\xe0" * 8, "iso15693", "type5", handle.read())
            self.assertEqual(decoders.decode(tag)[0].fmt, "OpenPrintTag")

    def test_openspool(self):
        payload = json.dumps({
            "protocol": "openspool", "version": "1.0", "type": "PETG", "subtype": "Rapid",
            "color_hex": "#AFAFAF", "additional_color_hexes": ["EEFFEE"], "alpha": "80",
            "brand": "Elegoo", "min_temp": "230", "max_temp": "260", "weight": 1000,
        }).encode()
        filament, _ = decoders.decode(type2_tag(mime_record("application/json", payload)))
        self.assertEqual(filament.fmt, "OpenSpool")
        self.assertEqual((filament.material, filament.modifiers, filament.color, filament.alpha,
                          filament.extra_colors, filament.nozzle_temp),
                         ("PETG", ["Rapid"], "AFAFAF", 0x80, ["EEFFEE"], 245))

    def test_opentag3d_v2(self):
        payload = bytearray(0xD8)
        payload[0:2] = (2001).to_bytes(2, "big")
        payload[0x02:0x05] = b"PLA"
        payload[0x07:0x0B] = b"Silk"
        payload[0x0C:0x14] = b"Polymake"
        payload[0x3C:0x40] = b"\x11\x22\x33\xff"
        payload[0x8C:0x8E] = (1750).to_bytes(2, "big")
        payload[0x90] = 42           # 210 C
        payload[0x9E:0xA0] = (1000).to_bytes(2, "big")
        filament, _ = decoders.decode(type2_tag(mime_record("application/opentag3d", bytes(payload))))
        self.assertEqual((filament.fmt, filament.material, filament.modifiers, filament.brand,
                          filament.color, filament.nozzle_max, filament.weight, filament.diameter),
                         ("OpenTag3D", "PLA", ["Silk"], "Polymake", "112233", 210, 1000, 1.75))

    def test_spoolman_references(self):
        filament, _ = decoders.decode(type2_tag(text_record("SPOOL:23\nFILAMENT:14\n")))
        self.assertEqual((filament.fmt, filament.spool_id, filament.filament_id), ("Spoolman", 23, 14))
        filament, _ = decoders.decode(type2_tag(uri_record(0, "web+spoolman:s-5")))
        self.assertEqual(filament.spool_id, 5)
        filament, _ = decoders.decode(type2_tag(uri_record(3, "spoolman.local:7912/spool/show/9")))
        self.assertEqual(filament.spool_id, 9)

    def test_reference_merges_into_richer_format(self):
        payload = json.dumps({"protocol": "openspool", "type": "PLA", "color_hex": "FF0000"}).encode()
        message = mime_record("application/json", payload, last=False) + \
            text_record("SPOOL:3", first=False)
        filament, _ = decoders.decode(type2_tag(message))
        self.assertEqual((filament.fmt, filament.spool_id), ("OpenSpool", 3))

    def test_text(self):
        for text, expected in (("PLA:FF0000", ("PLA", "FF0000")),
                               ("PETG #00ff00", ("PETG", "00FF00")),
                               ("PLA Silk;123456;Sunlu", ("PLA", "123456"))):
            filament, _ = decoders.decode(type2_tag(text_record(text)))
            self.assertEqual((filament.material, filament.color), expected, text)
        filament, _ = decoders.decode(type2_tag(text_record("PLA Silk;123456;Sunlu")))
        self.assertEqual((filament.modifiers, filament.brand), (["Silk"], "Sunlu"))

    def test_spoolease(self):
        filament, _ = decoders.decode(type2_tag(uri_record(
            4, "tag.spoolease.io/S1?M=PLA-S&MS=Matte&B=Bambu&CC=112233FF;445566FF&NN=190&NX=230")))
        self.assertEqual((filament.fmt, filament.material, filament.color, filament.extra_colors,
                          filament.nozzle_min), ("SpoolEase", "PLA", "112233", ["445566"], 190))

    def test_unknown_tag(self):
        filament, records = decoders.decode(type2_tag(b"\x00" * 10))
        self.assertIsNone(filament)


class VendorTest(unittest.TestCase):
    def test_tigertag(self):
        user = bytearray(96)
        struct.pack_into(">I", user, 0, 0xBC0FCB97)
        struct.pack_into(">I", user, 4, 1234)
        struct.pack_into(">H", user, 8, 9691)        # EVA in the bundled table
        user[13] = 56                                 # 1.75 mm
        user[16:20] = b"\x10\x20\x30\xff"
        user[20:23] = (1000).to_bytes(3, "big")
        user[23] = 21                                 # grams
        struct.pack_into(">H", user, 24, 200)
        struct.pack_into(">H", user, 26, 220)
        filament, _ = decoders.decode(type2_tag(user=bytes(user)))
        self.assertEqual((filament.fmt, filament.material, filament.color, filament.weight,
                          filament.diameter, filament.nozzle_min),
                         ("TigerTag", "EVA", "102030", 1000, 1.75, 200))

    def test_anycubic(self):
        mem = bytearray(0x90)
        mem[0x10:0x14] = b"\x7b\x00\x65\x00"
        mem[0x14:0x1E] = b"HPL19-102\x00"
        mem[0x28:0x2A] = b"AC"
        mem[0x3C:0x44] = b"PLA High"
        mem[0x50:0x54] = bytes([0xFF, 0x33, 0x22, 0x11])   # A B G R
        struct.pack_into("<HH", mem, 0x60, 190, 230)
        struct.pack_into("<HHHH", mem, 0x74, 50, 60, 175, 330)
        tag = type2_tag(user=bytes(mem[16:]))
        filament, _ = decoders.decode(tag)
        self.assertEqual((filament.fmt, filament.material, filament.color, filament.weight,
                          filament.bed_temp), ("Anycubic", "PLA", "112233", 1000, 60))

    def _mfc(self, sector1, keys_name):
        memory = bytearray(1024)
        memory[64:64 + len(sector1)] = sector1
        tag = Tag(b"\xde\xad\xbe\xef", "iso14443a", "mifare_classic", bytes(memory), sak=0x08)
        tag.sectors_read = set(range(16))
        tag.sector_keys = {s: keys_name for s in range(16)}
        return tag

    def test_qidi(self):
        filament, _ = decoders.decode(self._mfc(bytes([0x04, 0x12, 0x01]), "default"))
        self.assertEqual((filament.fmt, filament.material, filament.modifiers, filament.color),
                         ("Qidi", "PLA", ["Silk"], "FF362D"))

    def test_creality_plaintext(self):
        # batch, YY, M (hex), DD, supplier, material code, '0' + RRGGBB,
        # length in m, serial, reserve
        text = b"AB1" + b"24" + b"8" + b"15" + b"0276" + b"01001" + b"0FF8800" + b"0330" \
            + b"000001" + b"0" * 14
        self.assertEqual(len(text), 48)
        filament, _ = decoders.decode(self._mfc(text, "Creality"))
        self.assertEqual((filament.fmt, filament.material, filament.modifiers, filament.color,
                          filament.weight, filament.manufactured),
                         ("Creality", "PLA", ["Hyper"], "FF8800", 1000, "2024-08-15"))

    def test_creality_encrypted_with_built_in_key(self):
        text = b"AB1" + b"24" + b"8" + b"15" + b"0276" + b"06002" + b"0123456" + b"0165" \
            + b"000002" + b"0" * 14
        cipher = crypto.aes128_ecb(vendors.CREALITY_DEFAULT_AES_KEY, text)
        filament, _ = decoders.decode(self._mfc(cipher, "Creality"))
        self.assertEqual((filament.material, filament.color, filament.weight),
                         ("PETG", "123456", 500))

    def test_built_in_keys_match_published_hashes(self):
        for key, digest in ((vendors.BAMBU_DEFAULT_KEY, vendors.BAMBU_KEY_SHA256),
                            (vendors.CREALITY_DEFAULT_KEY, vendors.CREALITY_KEY_SHA256),
                            (vendors.CREALITY_DEFAULT_AES_KEY, vendors.CREALITY_AES_KEY_SHA256),
                            (vendors.SNAPMAKER_DEFAULT_KEY, vendors.SNAPMAKER_KEY_SHA256)):
            self.assertTrue(crypto.key_matches(key, digest), key)

    def test_bambu(self):
        memory = bytearray(1024)
        memory[24:29] = b"GFA00"
        memory[32:35] = b"PLA"
        memory[64:74] = b"PLA Basic\x00"
        memory[80:84] = b"\xaa\xbb\xcc\xff"
        struct.pack_into("<H", memory, 84, 1000)
        struct.pack_into("<f", memory, 88, 1.75)
        struct.pack_into("<HHHHHH", memory, 96, 55, 8, 1, 60, 230, 190)
        memory[192:208] = b"2024_08_15_10_30"
        tag = Tag(b"\x01\x02\x03\x04", "iso14443a", "mifare_classic", bytes(memory), sak=0x08)
        tag.sectors_read = set(range(16))
        tag.sector_keys = {s: "Bambu" for s in range(16)}
        filament, _ = decoders.decode(tag)
        self.assertEqual((filament.fmt, filament.material, filament.modifiers, filament.color,
                          filament.weight, filament.nozzle_min, filament.nozzle_max,
                          filament.external_id, filament.manufactured),
                         ("Bambu", "PLA", ["Basic"], "AABBCC", 1000, 190, 230, "GFA00", "2024-08-15"))

    def test_vendor_formats_require_their_keys(self):
        # A Bambu-looking dump that opened with the factory key is not Bambu.
        tag = self._mfc(b"\x00" * 48, "default")
        self.assertIsNone(vendors.decode_bambu(tag, [], {}))

    def test_snapmaker_default_key_matches_published_hash(self):
        self.assertTrue(crypto.key_matches(vendors.SNAPMAKER_DEFAULT_KEY,
                                           vendors.SNAPMAKER_KEY_SHA256))
        keyset = [k for k in decoders.mifare_keysets(b"\xaa\xbb\xcc\xdd", {})
                  if k.name == "Snapmaker"][0]
        # Key A comes from the 25-byte salt A, key B from the full salt B.
        self.assertEqual(keyset.keys_a[0], crypto.hkdf_sha256(
            b"\xaa\xbb\xcc\xdd", b"Snapmaker_qwertyuiop[,.;]", b"key_a_0", 6))
        self.assertEqual(keyset.keys_b[15], crypto.hkdf_sha256(
            b"\xaa\xbb\xcc\xdd", b"Snapmaker_qwertyuiop[,.;]_1q2w3e", b"key_b_15", 6))

    def test_keysets(self):
        # The published vendor keys are built in.
        self.assertEqual([k.name for k in decoders.mifare_keysets(b"\x01\x02\x03\x04", {})],
                         ["Bambu", "Snapmaker", "Creality"])
        keysets = decoders.mifare_keysets(b"\x01\x02\x03\x04", {
            "bambu_key": b"\x11" * 16, "creality_key": b"\x22" * 16,
            "snapmaker_key": b"\x33" * 32, "extra_mifare_keys": [b"\x44" * 6]})
        self.assertEqual([k.name for k in keysets], ["Bambu", "Snapmaker", "Creality", "custom"])
        self.assertEqual(len(keysets[0].keys_a), 16)
        self.assertEqual(len(keysets[0].keys_a[0]), 6)


if __name__ == "__main__":
    unittest.main()
