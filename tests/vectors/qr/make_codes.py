"""Regenerate codes.json: QR code module matrices made with segno
(pip install segno), the reference encoder for test_qr. Not needed to run
the tests.

    python make_codes.py > codes.json
"""

import json

import segno

CASES = [
    # (text, version, error level, mask, mode)
    ("web+spoolman:s-1", 1, "l", 0, None),
    ("web+spoolman:s-23", 2, "m", 1, None),
    ("web+spoolman:s-456", 2, "q", 2, None),
    ("https://spoolman.local:7912/spool/show/17", 3, "m", 3, None),
    ("https://spoolman.local:7912/spool/show/17", 4, "h", 4, None),
    ('{"protocol":"openspool","version":"1.0","type":"PETG","color_hex":"00AAFF",'
     '"brand":"Generic","min_temp":"230","max_temp":"250"}', 5, "q", 5, None),
    ("PLA Silk;C0C0C0;Generic " * 4, 6, "m", 6, None),
    ("PETG:00FF00 " * 10, 7, "q", 7, None),
    ("OpenSpool and Spoolman labels " * 5, 8, "h", 0, None),
    ("x" * 180, 9, "m", 1, None),
    ("y" * 213, 10, "m", 2, None),
    ("z" * 100, 10, "h", 3, None),
    ("0123456789012345678901234567890", 2, "l", 4, "numeric"),
    ("SPOOL:42 FILAMENT:7", 1, "q", 5, "alphanumeric"),
    ("Grüße, Spule 5 €", 2, "m", 6, "byte"),
    ("点茗", 1, "h", 7, "kanji"),
]


def main():
    codes = []
    for text, version, error, mask, mode in CASES:
        while True:
            # Trim the text until it fits the version under test.
            try:
                code = segno.make_qr(text, version=version, error=error, mask=mask, mode=mode,
                                     boost_error=False)
                break
            except segno.DataOverflowError:
                text = text[:-1]
        codes.append({
            "text": text, "version": version, "error": error.upper(), "mask": mask,
            "rows": ["".join("1" if module else "0" for module in row) for row in code.matrix],
        })
    print(json.dumps(codes, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
