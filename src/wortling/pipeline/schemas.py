import re
from typing import Literal

from pydantic import Field, model_validator

from wortling.config import StrictModel
from wortling.lemma import lemma_key

CEFR = Literal["A1", "A2", "B1", "B2", "C1", "C2"]


class Normalized(StrictModel):
    mostly_german: bool
    reason: str
    source_text: str
    sentences: list[str] = Field(max_length=150)


class Candidate(StrictModel):
    sentence_id: int = Field(
        ge=0,
        description="Copy the supplied sentence_id exactly; never renumber retained sentences.",
    )
    lemma: str = Field(
        min_length=1,
        max_length=200,
        description="German dictionary form: noun WITH der/die/das (die Rechnung), "
        "verb infinitive (erhalten, not erhältst), reflexive sich + infinitive + preposition. "
        "Never copy an inflected surface or an article-free lookup key as the lemma.",
    )
    surface: str = Field(
        min_length=1,
        max_length=200,
        description="Exact source spelling and case, including inflection; split verb parts use ….",
    )
    pos: Literal["noun", "verb", "adjective", "adverb", "phrase"]
    sense_gloss: str = Field(min_length=1, max_length=200)
    sense_key: str = Field(min_length=1, max_length=100)
    is_phrase: bool = Field(
        description="False for individual nouns (even WITH their required article), "
        "compound nouns, adjectives and single verbs. True only for multiword lexical "
        "expressions such as zur Verfügung stehen or sich freuen auf."
    )
    cefr_estimate: CEFR
    domain: str = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def dictionary_lemma(self):
        if (
            self.is_phrase
            and not self.lemma.startswith("sich ")
            and len(lemma_key(self.lemma).split()) < 2
        ):
            raise ValueError(
                "Phrase lemma must retain the whole expression, not its base word; "
                "use schief laufen (pos phrase), not laufen. Single lexical verbs "
                "such as anrufen use is_phrase=false."
            )
        if "…" in self.lemma or "..." in self.lemma:
            raise ValueError("Lemma must be dictionary form, never a split surface with ellipsis")
        if self.pos == "noun" and not re.match(
            r"^(?:der|die|das)(?:/(?:der|die|das))*\s+", self.lemma
        ):
            raise ValueError("Noun lemma needs its dictionary article")
        if self.pos == "verb":
            words = self.lemma.removeprefix("sich ").split()
            if not words or not (
                words[0] in {"sein", "tun"} or re.search(r"(?:en|eln|ern)$", words[0])
            ):
                raise ValueError("Verb lemma must start with its infinitive after optional sich")
            if ("…" in self.surface or "..." in self.surface) and not self.lemma.startswith(
                "sich "
            ):
                if len(words) != 1:
                    raise ValueError(
                        "Separable verb lemma must reattach the prefix, e.g. mitbringen"
                    )
                prefix = re.split(r"\s*(?:…|\.\.\.)\s*", self.surface)[-1].strip()
                prefixes = {
                    "ab",
                    "an",
                    "auf",
                    "aus",
                    "bei",
                    "ein",
                    "fest",
                    "fort",
                    "her",
                    "hin",
                    "los",
                    "mit",
                    "nach",
                    "vor",
                    "weg",
                    "weiter",
                    "wieder",
                    "zu",
                    "zurück",
                    "zusammen",
                    "dazu",
                    "daran",
                    "davon",
                    "auseinander",
                }
                if prefix in prefixes and not self.lemma.startswith(prefix):
                    raise ValueError(f"Separable prefix {prefix!r} must be reattached in the lemma")
        return self


class Extracted(StrictModel):
    candidates: list[Candidate] = Field(max_length=25)


class Judgment(StrictModel):
    item_id: str
    keep: bool
    reason: str


class Judgments(StrictModel):
    judgments: list[Judgment] = Field(max_length=25)


class Generated(StrictModel):
    item_id: str
    text: str
    lemma: str
    forms: str
    gloss: str
    sentence_en: str


class GeneratedCards(StrictModel):
    cards: list[Generated] = Field(max_length=25)
