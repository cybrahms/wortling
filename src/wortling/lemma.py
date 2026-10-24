"""Conservative lexical normalization; preserve umlauts, ß's casefold and prepositions."""

import html
import re
import unicodedata

# Scaffolding German, never the thing a cloze card tests: articles, pronouns,
# possessives, prepositions, conjunctions, auxiliaries, negation and particles.
FUNCTION_WORDS = frozenset(
    """
    der die das den dem des ein eine einen einem eines einer
    ich du er sie es wir ihr mich dich sich uns euch mir dir ihm ihnen ihn
    mein meine meinen meinem meiner dein deine deinen deinem deiner
    sein seine seinen seinem seiner ihr ihre ihren ihrem ihrer
    unser unsere unseren unserem unserer euer eure euren eurem eurer
    in an auf zu mit nach von bei aus unter vor hinter neben zwischen durch
    gegen ohne um seit bis ab trotz waehrend wegen statt fuer am im ins ans beim vom
    zum zur durchs fuers ums
    und oder aber denn sondern weil dass wenn als ob wie obwohl sobald falls damit
    bevor nachdem seitdem
    bin bist ist sind seid war waren habe hast hat haben hatte hatten habt
    werde wirst wird werden wurde wurden wuerden
    nicht kein keine keinen keinem keiner nichts
    doch mal ja auch noch nur schon sehr so dann da hier dort
    """.split()
)


def is_function_word(surface: str) -> bool:
    """True for a surface that is grammar scaffolding rather than vocabulary."""
    word = surface.strip().strip(".,;:!?\"'„“”«»()[]…").casefold()
    if not word:
        return False
    return word in FUNCTION_WORDS or (
        word.replace("ü", "ue").replace("ö", "oe").replace("ä", "ae").replace("ß", "ss")
        in FUNCTION_WORDS
    )


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
