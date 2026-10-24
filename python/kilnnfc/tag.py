"""What a reader produced (Tag) and what a decoder understood (Filament)."""

# Tag technologies, as far as the reader can tell.
TECH_ISO14443A = "iso14443a"
TECH_ISO15693 = "iso15693"
TECH_CAMERA = "camera"                  # a QR code, read through the camera

# Tag memory layouts the decoders understand.
KIND_TYPE2 = "type2"                    # MIFARE Ultralight / NTAG21x
KIND_MIFARE_CLASSIC = "mifare_classic"  # MIFARE Classic 1K / 4K / Mini
KIND_TYPE4 = "type4"                    # ISO-DEP (DESFire, ...) with NDEF
KIND_TYPE5 = "type5"                    # ISO 15693 / NFC-V (ICODE SLIX2, ...)
KIND_UNKNOWN = "unknown"
KIND_QR = "qr"                          # QR code; memory holds its text (UTF-8)

MIFARE_BLOCK = 16


def mifare_sector_layout(memory_size):
    """[(first_block, block_count)] for each MIFARE Classic sector: 4 blocks
    per sector for the first 32 sectors, 16 per sector after that (4K)."""
    sectors = []
    block = 0
    total = memory_size // MIFARE_BLOCK
    while block < total:
        count = 4 if block < 128 else 16
        sectors.append((block, count))
        block += count
    return sectors


class Tag(object):
    """A tag as read: its identity and a dump of its memory.

    memory starts at the tag's first page/block, so decoder offsets match
    the tag's datasheet:
      type2           pages 0..n, 4 bytes each (page 4 is the first user page)
      type5           blocks 0..n (block size in block_size)
      mifare_classic  every block of every sector; sectors that could not be
                      authenticated are zero-filled and missing from
                      sectors_read, and sector trailers (keys and access
                      bits, no data) are not read and stay zero
      type4           empty; the NDEF file is in ndef
      qr              the QR code's text as UTF-8; there is no UID
    """

    def __init__(self, uid, tech, kind, memory=b"", atqa=b"", sak=None,
                 block_size=4, reader=""):
        self.uid = bytes(uid)
        self.tech = tech
        self.kind = kind
        self.memory = bytes(memory)
        self.atqa = bytes(atqa)
        self.sak = sak
        self.block_size = block_size
        self.reader = reader
        # NDEF message bytes, when the reader read an NDEF file directly
        # (Type 4) instead of a memory image.
        self.ndef = None
        # MIFARE Classic: sector numbers that were read, and per sector the
        # name of the key set that opened it ("Bambu", "default", ...).
        self.sectors_read = set()
        self.sector_keys = {}
        # True when the tag left the reader before it was fully read.
        self.incomplete = False

    @property
    def uid_hex(self):
        return self.uid.hex().upper()

    def describe(self):
        if self.kind == KIND_QR:
            return "QR code, %d bytes" % len(self.memory)
        parts = [self.kind, self.tech, "UID " + self.uid_hex]
        if self.sak is not None:
            parts.append("SAK %02X" % self.sak)
        if self.kind == KIND_MIFARE_CLASSIC:
            total = len(mifare_sector_layout(len(self.memory)))
            parts.append("%d/%d sectors" % (len(self.sectors_read), total))
        elif self.memory:
            parts.append("%d bytes" % len(self.memory))
        return ", ".join(parts)


class Filament(object):
    """Filament data decoded from a tag. Every field but fmt is optional;
    a tag that only names a Spoolman spool has nothing but spool_id."""

    FIELDS = (
        "material", "modifiers", "brand", "name", "color", "alpha",
        "extra_colors", "diameter", "weight", "nozzle_min", "nozzle_max",
        "bed_temp", "dry_temp", "dry_time", "spool_id", "filament_id",
        "external_id", "manufactured",
    )

    def __init__(self, fmt, **fields):
        self.fmt = fmt                # decoder that produced it ("Bambu", ...)
        self.material = None          # base material: "PLA", "PETG-CF", ...
        self.modifiers = []           # "Silk", "Matte", "HF", ...
        self.brand = None
        self.name = None              # product/color name from the tag
        self.color = None             # "RRGGBB"
        self.alpha = 0xFF
        self.extra_colors = []        # further "RRGGBB" for multicolor
        self.diameter = None          # mm
        self.weight = None            # g of filament on a full spool
        self.nozzle_min = None        # C
        self.nozzle_max = None
        self.bed_temp = None
        self.dry_temp = None
        self.dry_time = None          # h
        self.spool_id = None          # Spoolman spool, if the tag names one
        self.filament_id = None       # Spoolman filament, if the tag names one
        self.external_id = None       # vendor identity (TigerTag product, ...)
        self.manufactured = None      # ISO 8601 date
        for key, value in fields.items():
            if key not in self.FIELDS:
                raise TypeError("unknown Filament field %r" % key)
            setattr(self, key, value)
        if self.color is not None:
            self.color = normalize_color(self.color)
        self.extra_colors = [c for c in (normalize_color(c) for c in self.extra_colors) if c]

    @property
    def nozzle_temp(self):
        """One printing temperature: the middle of the range."""
        if self.nozzle_min and self.nozzle_max:
            return int(round((self.nozzle_min + self.nozzle_max) / 2.))
        return int(self.nozzle_max or self.nozzle_min or 0) or None

    def title(self):
        """Short name for prompts: brand, material and modifiers."""
        words = [self.brand, self.material] + list(self.modifiers)
        title = " ".join(str(w) for w in words if w)
        if self.name and self.name.lower() not in title.lower():
            title = (title + " " + self.name).strip()
        return title or self.fmt

    def as_dict(self):
        data = {"format": self.fmt}
        for key in self.FIELDS:
            value = getattr(self, key)
            if value not in (None, [], ""):
                data[key] = value
        return data


def normalize_color(value):
    """'RRGGBB' from '#rgb', 'RRGGBB', 'RRGGBBAA' or an int 0xRRGGBB, or None."""
    if value is None:
        return None
    if isinstance(value, int):
        return "%06X" % (value & 0xFFFFFF)
    text = str(value).strip().lstrip("#").upper()
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) == 8:
        text = text[:6]
    if len(text) != 6:
        return None
    try:
        int(text, 16)
    except ValueError:
        return None
    return text
