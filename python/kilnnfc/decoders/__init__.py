"""Tag decoders: turn a read Tag into a Filament.

settings is a dict of optional vendor keys (bytes), already checked against
their published hashes: bambu_key, creality_key, creality_aes_key,
snapmaker_key; plus extra_mifare_keys, a list of 6-byte keys to try."""

import logging

from ..readers.base import KeySet
from . import open_formats, vendors
from .ndef import tag_records

LOGGER = logging.getLogger("kiln_nfc")

# Tried in order; the first decoder that recognises the tag wins. Tags that
# name a Spoolman spool come first, free-form text last.
DECODERS = [
    ("Spoolman", open_formats.decode_spoolman),
    ("OpenPrintTag", open_formats.decode_openprinttag),
    ("OpenTag3D", open_formats.decode_opentag3d),
    ("OpenSpool", open_formats.decode_openspool),
    ("SpoolEase", open_formats.decode_spoolease),
    ("Bambu", vendors.decode_bambu),
    ("Creality", vendors.decode_creality),
    ("Snapmaker", vendors.decode_snapmaker),
    ("TigerTag", vendors.decode_tigertag),
    ("Anycubic", vendors.decode_anycubic),
    ("Elegoo", vendors.decode_elegoo),
    ("Qidi", vendors.decode_qidi),
    ("Text", open_formats.decode_text),
]

KEYSET_PROVIDERS = [
    vendors.bambu_keysets,
    vendors.snapmaker_keysets,
    vendors.creality_keysets,
]

FORMAT_NAMES = [name for name, _ in DECODERS]


def mifare_keysets(uid, settings):
    """Key sets to try on a MIFARE Classic tag, vendor formats first."""
    keysets = []
    for provider in KEYSET_PROVIDERS:
        try:
            keysets.extend(provider(uid, settings))
        except Exception:
            LOGGER.exception("kiln_nfc: key derivation failed in %s", provider.__name__)
    extra = settings.get("extra_mifare_keys") or []
    if extra:
        keysets.append(KeySet("custom", [extra[0]] * 40, [extra[0]] * 40))
        for index, key in enumerate(extra[1:]):
            keysets.append(KeySet("custom%d" % (index + 2), [key] * 40, [key] * 40))
    return keysets


def decode(tag, settings=None, disabled=()):
    """(Filament or None, NDEF records). A Spoolman reference found next to
    other data (e.g. an OpenSpool tag that also carries a spool_id) is
    merged into the richer result."""
    settings = settings or {}
    try:
        records = tag_records(tag)
    except Exception:
        LOGGER.exception("kiln_nfc: NDEF parsing failed")
        records = []
    reference = None
    for name, decoder in DECODERS:
        if name in disabled:
            continue
        try:
            filament = decoder(tag, records, settings)
        except Exception:
            LOGGER.exception("kiln_nfc: %s decoder failed on %s", name, tag.uid_hex)
            continue
        if filament is None:
            continue
        if name == "Spoolman":
            reference = filament
            continue
        if reference is not None:
            filament.spool_id = filament.spool_id or reference.spool_id
            filament.filament_id = filament.filament_id or reference.filament_id
        return filament, records
    return reference, records
