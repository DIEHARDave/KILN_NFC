"""QR codes from the printer camera.

The plugin runs this as a child process (python3 -m kilnnfc.camera) at a
low priority, so a slow decode never holds Klipper's interpreter and a
hung download or decoder cannot block it. It fetches camera snapshots,
turns each JPEG into a grayscale frame, and looks for a QR code until it
finds one or the time is up. The result is one JSON line on stdout:
{"text": ...} or {"error": ...}.

JPEG decoders:
  pillow  Pillow, which Kiln OS ships for its Python with libjpeg. Big
          frames are decoded at 1/2, 1/4 or 1/8 size directly (draft
          mode), which is much faster than decoding and then scaling.
  ffmpeg  an ffmpeg on PATH with the mjpeg demuxer and rawvideo output,
          for a system without Pillow.

Also usable by hand, e.g. with a saved snapshot:
    PYTHONPATH=python python3 -m kilnnfc.camera --image label.jpg
"""

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

from . import qr

# Kiln's S64ad5x-camera starts one ustreamer per camera from port 8090;
# ustreamer answers mjpg-streamer's snapshot URL.
DEFAULT_URL = "http://127.0.0.1:8090/?action=snapshot"
# Kiln's 1280x720 camera frames are then decoded at half size (draft
# mode): about 1 s per frame on the AD5X instead of 5 s at full size. Codes
# too small to read at that size get a second look from a full-resolution
# crop.
DEFAULT_WIDTH = 640
DECODERS = ("auto", "pillow", "ffmpeg")
MAX_SAVED_FRAMES = 20


class CameraError(Exception):
    pass


def find_ffmpeg(configured=""):
    """The ffmpeg to use: the configured one, else one on PATH."""
    if configured:
        return configured if os.access(configured, os.X_OK) else None
    return shutil.which("ffmpeg")


def jpeg_size(data):
    """(width, height) from a JPEG's frame header."""
    if data[:2] != b"\xff\xd8":
        raise CameraError("the camera did not send a JPEG image")
    pos = 2
    while pos + 9 < len(data):
        if data[pos] != 0xFF:
            break
        marker = data[pos + 1]
        if marker == 0xFF:
            pos += 1
            continue
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height = int.from_bytes(data[pos + 5:pos + 7], "big")
            width = int.from_bytes(data[pos + 7:pos + 9], "big")
            return width, height
        pos += 2 + int.from_bytes(data[pos + 2:pos + 4], "big")
    raise CameraError("no frame header in the camera's JPEG")


def _crop_box(crop, width, height):
    """Integer (left, top, right, bottom) inside the frame, even-aligned so
    chroma subsampling cannot shift it."""
    left, top = max(0, int(crop[0])) & ~1, max(0, int(crop[1])) & ~1
    right = left + ((min(width, int(crop[2])) - left) & ~1)
    bottom = top + ((min(height, int(crop[3])) - top) & ~1)
    if right - left < 16 or bottom - top < 16:
        raise CameraError("crop box too small")
    return left, top, right, bottom


class PillowDecoder(object):
    name = "pillow"

    def __init__(self):
        from PIL import Image
        self.Image = Image

    @staticmethod
    def available():
        try:
            from PIL import features
        except ImportError:
            return False
        try:
            return bool(features.check("jpg"))
        except Exception:
            return False

    def gray(self, jpeg, max_width=DEFAULT_WIDTH, crop=None):
        """(pixels, width, height): the JPEG as 8-bit grayscale, scaled down
        by 2, 4 or 8 while it stays at least max_width wide, or the crop box
        (left, top, right, bottom) of it at full resolution."""
        width, height = jpeg_size(jpeg)
        try:
            image = self.Image.open(io.BytesIO(jpeg))
            if crop is None:
                # draft picks the largest libjpeg scale that keeps the
                # image at least this big.
                image.draft("L", (max_width, max(1, height * max_width // width)))
                image = image.convert("L")
            else:
                image = image.convert("L")
                image = image.crop(_crop_box(crop, image.size[0], image.size[1]))
            return image.tobytes(), image.size[0], image.size[1]
        except CameraError:
            raise
        except Exception as exc:
            raise CameraError("could not decode the camera image: %s" % exc)


class FfmpegDecoder(object):
    name = "ffmpeg"

    def __init__(self, path):
        self.path = path

    def gray(self, jpeg, max_width=DEFAULT_WIDTH, crop=None):
        """Like PillowDecoder.gray, scaled to exactly max_width. ffmpeg's
        -lowres decodes big frames at 1/2 or 1/4 size directly."""
        width, height = jpeg_size(jpeg)
        lowres = 0
        if crop is not None:
            left, top, right, bottom = _crop_box(crop, width, height)
            out_width, out_height = right - left, bottom - top
            video_filter = "crop=%d:%d:%d:%d" % (out_width, out_height, left, top)
        else:
            while lowres < 2 and (width >> (lowres + 1)) >= max_width:
                lowres += 1
            out_width = min(max_width, width)
            out_height = max(1, height * out_width // width)
            video_filter = "scale=%d:%d" % (out_width, out_height)
        command = [self.path, "-hide_banner", "-loglevel", "error",
                   "-lowres", str(lowres), "-f", "mjpeg", "-i", "pipe:0",
                   "-frames:v", "1", "-vf", video_filter,
                   "-pix_fmt", "gray", "-f", "rawvideo", "pipe:1"]
        try:
            result = subprocess.run(command, input=jpeg, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CameraError("ffmpeg failed: %s" % exc)
        if len(result.stdout) != out_width * out_height:
            message = result.stderr.decode("utf-8", "replace").strip().splitlines()
            raise CameraError("ffmpeg could not convert the image%s"
                              % (": " + message[-1] if message else ""))
        return result.stdout, out_width, out_height


def make_decoder(kind="auto", ffmpeg=""):
    """The JPEG decoder to use: Pillow when it can read JPEG, otherwise
    ffmpeg."""
    if kind in ("auto", "pillow") and PillowDecoder.available():
        return PillowDecoder()
    if kind == "pillow":
        raise CameraError("Pillow with JPEG support is not installed")
    path = find_ffmpeg(ffmpeg)
    if path is None:
        raise CameraError("no JPEG decoder: neither Pillow nor ffmpeg")
    return FfmpegDecoder(path)


def decode_jpeg(jpeg, decoder, max_width=DEFAULT_WIDTH):
    """Text of a QR code in a JPEG, or None. The frame is decoded scaled
    down first; a code whose modules came out too small to read is tried
    again from a full-resolution crop around it."""
    pixels, width, height = decoder.gray(jpeg, max_width)
    small = []
    text = qr.decode_image(pixels, width, height, small=small)
    if text is not None or not small:
        return text
    scale = jpeg_size(jpeg)[0] / float(width)
    if scale < 1.5:
        return None
    for box in small[:3]:
        crop = [value * scale for value in box]
        text = qr.decode_image(*decoder.gray(jpeg, crop=crop))
        if text is not None:
            return text
    return None


def snapshot(url, timeout=5.):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.read()
    except Exception as exc:
        raise CameraError("no camera image from %s: %s" % (url, exc))


def _save_frames(directory):
    """A function that stores each frame of this scan in directory (emptied
    first), for looking at why a label was not read."""
    try:
        os.makedirs(directory, exist_ok=True)
        for name in os.listdir(directory):
            if name.startswith("frame") and name.endswith(".jpg"):
                os.remove(os.path.join(directory, name))
    except OSError:
        return None

    def save(number, jpeg):
        if number <= MAX_SAVED_FRAMES:
            try:
                with open(os.path.join(directory, "frame%02d.jpg" % number), "wb") as handle:
                    handle.write(jpeg)
            except OSError:
                pass
    return save


def scan(url, decoder, seconds, max_width=DEFAULT_WIDTH, log=None, save_dir=None):
    """Text of the first QR code the camera sees within seconds."""
    deadline = time.time() + seconds
    frames = 0
    save = _save_frames(save_dir) if save_dir else None
    while True:
        started = time.time()
        jpeg = snapshot(url)
        if save:
            save(frames + 1, jpeg)
        fetched = time.time()
        text = decode_jpeg(jpeg, decoder, max_width)
        frames += 1
        if log:
            log("frame %d: fetch %.2fs, decode %.2fs%s"
                % (frames, fetched - started, time.time() - fetched,
                   ", found" if text is not None else ""))
        if text is not None:
            return text, frames
        if time.time() >= deadline:
            return None, frames


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read a QR code from the printer camera.")
    parser.add_argument("--url", default=DEFAULT_URL, help="camera snapshot URL")
    parser.add_argument("--image", help="decode this JPEG file instead of the camera")
    parser.add_argument("--decoder", choices=DECODERS, default="auto", help="JPEG decoder")
    parser.add_argument("--ffmpeg", default="", help="path to ffmpeg")
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH,
                        help="scale frames down towards this width")
    parser.add_argument("--time", type=float, default=15., help="seconds to keep looking")
    parser.add_argument("--nice", type=int, default=0, help="lower this process's priority")
    parser.add_argument("--verbose", action="store_true", help="timings on stderr")
    parser.add_argument("--save-dir", help="keep this scan's camera frames in this directory")
    args = parser.parse_args(argv)
    if args.nice:
        try:
            os.nice(args.nice)
        except OSError:
            pass
    log = (lambda message: print(message, file=sys.stderr)) if args.verbose else None
    result = {}
    try:
        decoder = make_decoder(args.decoder, args.ffmpeg)
        if log:
            log("decoder: %s" % decoder.name)
        if args.image:
            with open(args.image, "rb") as handle:
                text, frames = decode_jpeg(handle.read(), decoder, args.width), 1
        else:
            text, frames = scan(args.url, decoder, args.time, args.width, log, args.save_dir)
        result["frames"] = frames
        if text is None:
            result["error"] = "no QR code found"
        else:
            result["text"] = text
    except CameraError as exc:
        result["error"] = str(exc)
    print(json.dumps(result))
    return 0 if "text" in result else 1


if __name__ == "__main__":
    sys.exit(main())
