"""Conservative lexical normalization; preserve umlauts, ß's casefold and prepositions."""

import html
import re
import unicodedata


def lemma_key(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    value = unicodedata.normalize("NFC", html.unescape(value)).casefold().strip()
    value = re.sub(r"^(?:der|die|das)(?:/(?:der|die|das))*\s+", "", value)
    value = re.sub(r"^sich\s+", "", value)
    # Common dictionary plural annotations; do not split ordinary multiword phrases.
    value = re.sub(r"\s*\([^)]*\)\s*$", "", value)
    value = re.split(r"\s*[,;]\s*", value, maxsplit=1)[0]
    value = re.sub(r"\s+[-–](?:[a-zäöüß]+|\")\s*$", "", value)
    return " ".join(value.split()).strip()


def sense_key(gloss: str) -> str:
    return re.sub(r"[^\w]+", "-", unicodedata.normalize("NFC", gloss).casefold()).strip("-")
