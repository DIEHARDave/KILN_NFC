"""Material names: normalising what tags call a material."""

import re

# Spellings seen on tags -> the usual name.
ALIASES = {
    "PLA+": "PLA", "PLA PLUS": "PLA", "PLA-PLUS": "PLA", "PLA-S": "PLA",
    "PETG+": "PETG", "PET-G": "PETG", "ABS+": "ABS", "ABS-S": "ABS",
    "NYLON": "PA", "PA-S": "PA", "TPU-AMS": "TPU", "TPU 95A": "TPU",
    "COPE": "CPE", "PCTG+": "PCTG",
}

FILLERS = ("CF", "GF")


def normalize(material, modifiers=None):
    """(base material, modifiers) from free text such as 'PLA Silk' or
    'petg-cf'. Fillers stay part of the base ('PETG-CF')."""
    modifiers = list(modifiers or [])
    text = str(material or "").strip().upper()
    if not text:
        return None, modifiers
    if text in ALIASES:
        return ALIASES[text], modifiers
    words = [w for w in re.split(r"[\s_]+", text) if w]
    base = words[0]
    for word in words[1:]:
        if word in FILLERS:
            base += "-" + word
        else:
            # "SILK" -> "Silk", but keep "HF", "95A" and "+" as they are.
            modifiers.append(word.title() if word.isalpha() and len(word) > 2 else word)
    if base.endswith("+"):
        base = base[:-1]
        modifiers.append("+")
    return ALIASES.get(base, base), modifiers
