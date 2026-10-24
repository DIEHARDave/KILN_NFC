"""QR code reader: Reed-Solomon, symbol decoding against reference codes,
and finding codes in rendered camera-like images."""

import codecs
import io
import json
import math
import os
import random
import sys
import unittest

import helpers  # noqa: F401  (puts the plugin on sys.path)

from kilnnfc import camera, qr
from kilnnfc.decoders import decode
from kilnnfc.decoders.ndef import text_records
from kilnnfc.tag import KIND_QR, TECH_CAMERA, Tag

VECTORS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vectors", "qr", "codes.json")


def has_codec(name):
    try:
        codecs.lookup(name)
        return True
    except LookupError:
        return False


def load_codes():
    with open(VECTORS, encoding="utf-8") as handle:
        codes = json.load(handle)
    for code in codes:
        code["bits"] = [[int(ch) for ch in row] for row in code["rows"]]
    return codes


def render(bits, width, height, module, angle=0., tilt=0., noise=0, dark=40, light=200,
           gradient=0, squash=1., seed=1):
    """Grayscale image of a code: module pixels per module, rotated by angle
    degrees, squashed vertically (a label lying flat, seen from low) and
    tilted in perspective, with a quiet zone of light paper."""
    rnd = random.Random(seed)
    dim = len(bits)
    cos, sin = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    out = bytearray(width * height)
    for y in range(height):
        for x in range(width):
            dx, dy = x + 0.5 - width / 2., y + 0.5 - height / 2.
            depth = 1. / (1 - tilt * dy / height)
            dx, dy = dx * depth, dy * depth * squash
            u = int(math.floor((cos * dx + sin * dy) / module + dim / 2.))
            v = int(math.floor((-sin * dx + cos * dy) / module + dim / 2.))
            value = light + gradient * (x / float(width) - 0.5)
            if 0 <= u < dim and 0 <= v < dim and bits[v][u]:
                value = dark
            if noise:
                value += rnd.uniform(-noise, noise)
            out[y * width + x] = max(0, min(255, int(value)))
    return bytes(out)


class ReedSolomonTest(unittest.TestCase):
    def test_corrects_up_to_half_the_ec_codewords(self):
        code = load_codes()[1]          # version 2-M: one block, 16 EC codewords
        block = qr._read_codewords(code["bits"], 2, code["mask"])[:44]
        rnd = random.Random(5)
        for errors in range(9):
            damaged = list(block)
            for pos in rnd.sample(range(len(block)), errors):
                damaged[pos] ^= rnd.randrange(1, 256)
            self.assertEqual(qr.rs_correct(damaged, 16), block)

    def test_too_many_errors(self):
        code = load_codes()[1]
        block = qr._read_codewords(code["bits"], 2, code["mask"])[:44]
        damaged = [value ^ 0x5A for value in block[:12]] + block[12:]
        with self.assertRaises(qr.QrError):
            qr.rs_correct(damaged, 16)


class MatrixTest(unittest.TestCase):
    def codes(self):
        """The reference codes this Python can decode: Kiln OS's has no
        shift_jis codec for the Kanji code."""
        return [code for code in load_codes()
                if has_codec("shift_jis")
                or not any(0x3000 <= ord(ch) <= 0x9FFF for ch in code["text"])]

    def test_reference_codes(self):
        for code in self.codes():
            self.assertEqual(qr.decode_matrix(code["bits"]), code["text"],
                             "version %d-%s" % (code["version"], code["error"]))

    def test_damaged_modules(self):
        rnd = random.Random(7)
        for code in self.codes():
            bits = [list(row) for row in code["bits"]]
            dim = len(bits)
            for _ in range(3):
                r, c = rnd.randrange(9, dim - 9), rnd.randrange(9, dim - 9)
                bits[r][c] ^= 1
            self.assertEqual(qr.decode_matrix(bits), code["text"])

    def test_mirrored_code_is_not_a_plain_read(self):
        code = load_codes()[3]
        mirrored = [list(row) for row in zip(*code["bits"])]
        with self.assertRaises(qr.QrError):
            qr.decode_matrix(mirrored)

    def test_version_info(self):
        for code in load_codes():
            if code["version"] >= 7:
                self.assertEqual(qr._version_info(code["bits"]), code["version"])


class ImageTest(unittest.TestCase):
    def setUp(self):
        self.codes = load_codes()

    def check(self, code, width=240, height=220, **kw):
        pixels = render(code["bits"], width, height, **kw)
        self.assertEqual(qr.decode_image(pixels, width, height), code["text"], kw)

    def test_straight(self):
        self.check(self.codes[1], module=4)

    def test_rotated_noisy(self):
        self.check(self.codes[2], module=4, angle=150, noise=20)

    def test_perspective(self):
        self.check(self.codes[3], module=4, angle=-12, tilt=0.5)

    def test_low_contrast_and_uneven_light(self):
        self.check(self.codes[1], module=4, dark=90, light=150, gradient=60, noise=8)

    def test_mirrored(self):
        code = dict(self.codes[1])
        code["bits"] = [list(row) for row in zip(*code["bits"])]
        self.check(code, module=4)

    def test_lying_flat(self):
        # Rotated, then squashed 2.5:1: neither axis of the picture is one
        # of the code's.
        self.check(self.codes[1], width=260, height=160, module=5, angle=20, squash=2.5)

    def test_larger_version(self):
        self.check(self.codes[7], width=280, height=280, module=4, angle=4)

    def test_nothing_there(self):
        pixels = render([[0]], 200, 150, module=4, noise=30)
        self.assertIsNone(qr.decode_image(pixels, 200, 150))

    def test_small_code_reported_for_a_sharper_look(self):
        # At 2 pixels per module the code is located but not read.
        pixels = render(self.codes[1]["bits"], 160, 140, module=2.)
        small = []
        text = qr.decode_image(pixels, 160, 140, small=small)
        if text is None:
            self.assertTrue(small)
            left, top, right, bottom = small[0]
            self.assertTrue(left < 80 < right and top < 70 < bottom)


class ScanTextTest(unittest.TestCase):
    def decode(self, text):
        tag = Tag(b"", TECH_CAMERA, KIND_QR, text.encode("utf-8"), reader="camera")
        return decode(tag)[0]

    def test_records(self):
        self.assertTrue(text_records("web+spoolman:s-12")[0].is_uri())
        self.assertTrue(text_records("https://x/spool/show/3")[0].is_uri())
        self.assertTrue(text_records("PLA:FF0000")[0].is_text())
        self.assertEqual(text_records('{"a": 1}')[0].mime, "application/json")

    def test_spoolman_label(self):
        filament = self.decode("web+spoolman:s-12")
        self.assertEqual((filament.fmt, filament.spool_id), ("Spoolman", 12))
        self.assertEqual(self.decode("http://printer:7912/spool/show/8").spool_id, 8)

    def test_openspool_json(self):
        filament = self.decode('{"protocol":"openspool","version":"1.0","type":"PETG",'
                               '"color_hex":"00AAFF","brand":"Generic"}')
        self.assertEqual((filament.fmt, filament.material, filament.color),
                         ("OpenSpool", "PETG", "00AAFF"))

    def test_plain_text(self):
        filament = self.decode("PLA:FF0000")
        self.assertEqual((filament.material, filament.color), ("PLA", "FF0000"))


class CameraTest(unittest.TestCase):
    def test_jpeg_size(self):
        header = (b"\xff\xd8" + b"\xff\xe0\x00\x04ab"
                  + b"\xff\xc0\x00\x11\x08" + (1536).to_bytes(2, "big") + (2048).to_bytes(2, "big")
                  + b"\x03" + b"\x00" * 9)
        self.assertEqual(camera.jpeg_size(header), (2048, 1536))

    def test_not_a_jpeg(self):
        with self.assertRaises(camera.CameraError):
            camera.jpeg_size(b"400: Not Found!")

    def jpeg(self, width, height):
        return (b"\xff\xd8\xff\xc0\x00\x11\x08" + height.to_bytes(2, "big")
                + width.to_bytes(2, "big") + b"\x03" + b"\x00" * 9)

    def test_decoder_choice(self):
        original = camera.PillowDecoder.available
        camera.PillowDecoder.available = staticmethod(lambda: False)
        try:
            with self.assertRaises(camera.CameraError):
                camera.make_decoder("pillow")
            with self.assertRaises(camera.CameraError):
                camera.make_decoder("auto", "/nonexistent/ffmpeg")
            self.assertEqual(camera.make_decoder("ffmpeg", sys.executable).name, "ffmpeg")
        finally:
            camera.PillowDecoder.available = original


@unittest.skipUnless(camera.PillowDecoder.available(), "needs Pillow with JPEG support")
class PillowTest(unittest.TestCase):
    def setUp(self):
        from PIL import Image
        self.Image = Image
        self.codes = load_codes()

    def jpeg_of(self, pixels, width, height):
        out = io.BytesIO()
        self.Image.frombytes("L", (width, height), pixels).save(out, "JPEG", quality=90)
        return out.getvalue()

    def test_scaling_and_crop(self):
        decoder = camera.make_decoder("auto")
        self.assertEqual(decoder.name, "pillow")
        pixels = bytes(range(256)) * (2048 * 1536 // 256)
        jpeg = self.jpeg_of(pixels, 2048, 1536)
        _, width, height = decoder.gray(jpeg, 1024)
        self.assertEqual((width, height), (1024, 768))
        _, width, height = decoder.gray(jpeg, 1280)
        self.assertEqual((width, height), (2048, 1536))
        pixels, width, height = decoder.gray(jpeg, crop=(101, 51, 133, 85))
        self.assertEqual((width, height, len(pixels)), (32, 34, 32 * 34))

    def test_reads_code_from_jpeg(self):
        code = self.codes[0]
        pixels = render(code["bits"], 640, 360, module=6.)
        text = camera.decode_jpeg(self.jpeg_of(pixels, 640, 360), camera.make_decoder("pillow"))
        self.assertEqual(text, code["text"])

    def test_bad_jpeg(self):
        with self.assertRaises(camera.CameraError):
            # A frame header and nothing else: no image data to decode.
            camera.make_decoder("pillow").gray(bytes.fromhex("ffd8ffc000110801e0028003") + bytes(9))


if __name__ == "__main__":
    unittest.main()
