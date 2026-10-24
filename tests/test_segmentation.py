"""Long captures must reach a card as spans a person can read.

The specimen is Justin's own capture (2026-10-24): three parcel-delivery
sentences run together, one of them cut mid-word by the OCR. A card quoting that
blob cannot be studied -- it is too long to read on a phone, it starts and stops
inside sentences, and it does not show which clause the word came from.
"""

import pytest
from fastapi.testclient import TestClient

from wortling.api import create_app
from wortling.db import Database, now
from wortling.pipeline.engine import (
    SPAN_MAX_CHARS,
    SPAN_MAX_WORDS,
    Pipeline,
    best_span,
    cuts_mid_word,
    looks_like_a_sentence,
    preserve_short_source,
    segment_candidates,
    validate_normalized,
)
from wortling.pipeline.schemas import Extracted, GeneratedCards, Normalized

# Verbatim, including the line break the screenshot produced and the OCR
# truncation in "Sendu". The newline matters: assembling these lines with a space
# hides the defect where two unrelated sentences are joined into one span.
RUN_ON = (
    "Wähle diese Option für Kauf auf Rechnung. erhältst Statusmeldungen zu deiner Sendu\n"
    "Kauf auf Rechnung steht die Sendungsverfolgung nicht zur Verfügung."
)

ABBREVIATED_SENTENCES = [
    "Wir liefern z. B. morgen.",
    "Die Sendung Nr. 3 ist unterwegs.",
    "Bitte rufen Sie uns an, d. h. vor 18 Uhr.",
]


def test_run_on_capture_is_segmented_into_card_sized_spans():
    spans = segment_candidates(RUN_ON)

    # Two spans, not three: line 1 carries sentence one plus the OCR fragment that
    # starts lowercase, so nothing can call that a sentence boundary; line 2 is a
    # sentence of its own and is no longer glued to it.
    assert len(spans) == 2, spans
    for span in spans:
        assert len(span) <= SPAN_MAX_CHARS, (span, len(span))
        assert len(span.split()) <= SPAN_MAX_WORDS, (span, len(span.split()))


def test_every_candidate_is_verbatim_and_word_bounded():
    for span in segment_candidates(RUN_ON):
        assert span in RUN_ON, span
        assert not cuts_mid_word(RUN_ON, span), span


def test_segmentation_invents_no_text():
    """Every character on a card came from the source, in the source's order."""
    spans = segment_candidates(RUN_ON)
    cursor = 0
    for span in spans:
        found = RUN_ON.find(span, cursor)
        assert found >= 0, span
        cursor = found + len(span)


def test_mid_word_span_is_rejected():
    assert cuts_mid_word("zu deiner Sendung", "Sendu")
    assert not cuts_mid_word("zu deiner Sendung", "Sendung")

    with pytest.raises(ValueError, match="mid-word"):
        validate_normalized(
            Normalized(
                mostly_german=True,
                reason="German",
                source_text="zu deiner Sendung",
                sentences=["zu deiner Sendu"],
            ),
            "zu deiner Sendung",
            False,
        )


def test_word_from_a_middle_clause_gets_that_clause():
    spans = segment_candidates(RUN_ON)

    index = best_span(spans, "Sendungsverfolgung")

    assert index is not None
    assert spans[index] != RUN_ON
    assert "Sendungsverfolgung" in spans[index]
    assert len(spans[index]) < len(RUN_ON)
    # The first clause's word still resolves to the first clause.
    assert "Rechnung" in spans[best_span(spans, "Rechnung")]


def test_a_normal_sentence_is_left_whole():
    """Below the cap nothing is split: do not shorten a sentence by rewriting it."""
    for sentence in [
        "Bitte bringen Sie Ihre Unterlagen mit.",
        "Bitte bringen Sie\nIhre Unterlagen mit.",
    ]:
        assert segment_candidates(sentence) == [sentence]


@pytest.mark.parametrize("sentence", ABBREVIATED_SENTENCES)
def test_an_abbreviation_is_not_a_sentence_end(sentence):
    """z. B. and Nr. are inside a sentence; splitting there reads as broken German."""
    assert segment_candidates(sentence) == [sentence]


def test_an_abbreviation_inside_a_long_span_is_still_not_a_boundary():
    """The same guard holds when the span is too long and really is split."""
    long_sentence = (
        "Die Sendung Nr. 3 ist unterwegs und wir liefern z. B. am Donnerstag zwischen "
        "acht und achtzehn Uhr an die angegebene Adresse in der Nachbarschaft."
    )
    assert len(long_sentence) > SPAN_MAX_CHARS

    spans = segment_candidates(long_sentence)

    assert len(spans) > 1
    assert all(len(span) <= SPAN_MAX_CHARS for span in spans)
    assert not any(span.endswith(("z.", "B.", "Nr.", "d.")) for span in spans)


def test_a_span_never_crosses_a_line_that_starts_a_new_sentence():
    spans = segment_candidates(RUN_ON)

    assert not any("\n" in span for span in spans), spans
    assert "Kauf auf Rechnung steht die Sendungsverfolgung nicht zur Verfügung." in spans


def test_segmented_run_on_is_not_appended_back_by_preserve_short_source():
    """The line-coverage test must not undo the segmentation it inspects."""
    value = Normalized(
        mostly_german=True,
        reason="German",
        source_text=RUN_ON,
        sentences=segment_candidates(RUN_ON),
    )

    preserve_short_source(value)

    assert RUN_ON not in value.sentences
    assert value.sentences == segment_candidates(RUN_ON)


def test_a_line_with_an_uncovered_word_is_still_kept():
    """The original job of preserve_short_source: menu labels are learning material."""
    value = Normalized(
        mostly_german=True,
        reason="German app screen",
        source_text="Die Sendung kommt morgen.\nAbschicken",
        sentences=["Die Sendung kommt morgen."],
    )

    preserve_short_source(value)

    assert value.sentences == ["Die Sendung kommt morgen.", "Abschicken"]


def test_pipeline_hands_extraction_clauses_not_the_paragraph(settings):
    payloads = []

    class RecordingPipelineClient:
        def call(self, stage, schema, data, *args, **kwargs):
            payloads.append((stage, data))
            if stage == "normalize":
                return Normalized(
                    mostly_german=True,
                    reason="German parcel notice",
                    source_text=RUN_ON,
                    sentences=[RUN_ON],
                )
            if stage == "extract":
                # The payload is the span list, either bare strings or
                # {"sentence_id", "text"} rows depending on the branch this test
                # runs on; sentence_id is the index either way. Pick the longest
                # span carrying the word, the way a model that ignored the cap would.
                texts = [row["text"] if isinstance(row, dict) else row for row in data["sentences"]]
                index = max(
                    (number for number, text in enumerate(texts) if "Sendungsverfolgung" in text),
                    key=lambda number: len(texts[number]),
                )
                return Extracted.model_validate(
                    {
                        "candidates": [
                            {
                                "sentence_id": index,
                                "lemma": "die Sendungsverfolgung",
                                "surface": "Sendungsverfolgung",
                                "pos": "noun",
                                "sense_gloss": "parcel tracking",
                                "sense_key": "parcel-tracking",
                                "is_phrase": False,
                                "cefr_estimate": "B1",
                                "domain": "transit",
                            }
                        ]
                    }
                )
            if stage == "judge":
                return schema.model_validate(
                    {
                        "judgments": [
                            {"item_id": item["item_id"], "keep": True, "reason": "useful"}
                            for item in data["items"]
                        ]
                    }
                )
            return GeneratedCards.model_validate(
                {
                    "cards": [
                        {
                            "item_id": row["item_id"],
                            "text": "unused draft",
                            "lemma": "die Sendungsverfolgung, -en",
                            "forms": "",
                            "gloss": "parcel tracking",
                            "sentence_en": "Parcel tracking is not available.",
                        }
                        for row in data["items"]
                    ]
                }
            )

    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v1/captures",
            headers={"Authorization": "Bearer " + "a" * 48},
            json={"text": RUN_ON, "client_ts": now()},
        )
        assert response.status_code == 202
        capture_id = response.json()["id"]

    db = Database(settings.data_dir)
    Pipeline(settings, db, RecordingPipelineClient()).process(capture_id)

    extract_payload = next(data for stage, data in payloads if stage == "extract")
    offered = [
        row["text"] if isinstance(row, dict) else row for row in extract_payload["sentences"]
    ]
    assert RUN_ON not in offered, "the run-on reached extraction intact"
    assert all(len(text) <= SPAN_MAX_CHARS for text in offered)

    with db.connect() as conn:
        stored = conn.execute("SELECT sentence FROM items").fetchone()[0]
    assert stored != RUN_ON
    assert "Sendungsverfolgung" in stored
    assert len(stored) <= SPAN_MAX_CHARS


def test_a_sentence_under_the_ceiling_is_never_amputated():
    """Length is a ceiling, not a target: context outranks brevity."""
    sentence = (
        "Die Stadtbibliothek schließt am Freitag früher, weil die Räume für eine "
        "Lesung hergerichtet werden."
    )
    assert len(sentence) < SPAN_MAX_CHARS
    assert len(sentence.split()) <= SPAN_MAX_WORDS
    assert segment_candidates(sentence) == [sentence]


def test_a_label_is_not_a_sentence():
    for label in [
        "App-Fehler",
        "Niedriger Batteriestand",
        "Uneingeschränkter Zugriff",
        "Fotoabzüge 10 x 15 cm Express-Aholung (Papier: Glänzend)",
    ]:
        assert not looks_like_a_sentence(label), label
    for sentence in [
        "Ich möchte ein Auto umschreiben",
        "Wähle diese Option für Kauf auf Rechnung.",
        "Der Unterricht fällt morgen aus.",
    ]:
        assert looks_like_a_sentence(sentence), sentence


def test_a_thin_span_prefers_the_sentence_that_holds_it():
    spans = [
        "Auto umschreiben",
        "Ich möchte ein Auto umschreiben, weil sich der Halter ändert.",
    ]
    assert best_span(spans, "umschreiben") == 1


LABELS = ["App-Fehler", "Niedriger Batteriestand", "Uneingeschränkter Zugriff"]


class LabelClient:
    """Normalize keeps the app-screen labels and extraction mines a word from one."""

    def call(self, stage, schema, data, *args, **kwargs):
        if stage == "normalize":
            return schema.model_validate(
                {
                    "mostly_german": True,
                    "reason": "Scooter app labels",
                    "source_text": "\n".join(LABELS),
                    "sentences": LABELS,
                }
            )
        if stage == "extract":
            return schema.model_validate(
                {
                    "candidates": [
                        {
                            "sentence_id": 1,
                            "lemma": "der Batteriestand",
                            "surface": "Batteriestand",
                            "pos": "noun",
                            "sense_gloss": "battery level",
                            "sense_key": "battery-level",
                            "is_phrase": False,
                            "cefr_estimate": "B1",
                            "domain": "transit",
                        }
                    ]
                }
            )
        raise AssertionError(f"a label capture must not reach {stage}")


def test_a_label_only_capture_yields_no_cards_and_says_why(settings):
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v1/captures",
            headers={"Authorization": "Bearer " + "a" * 48},
            json={"text": "\n".join(LABELS), "client_ts": now()},
        )
        assert response.status_code == 202
        capture_id = response.json()["id"]

    db = Database(settings.data_dir)
    Pipeline(settings, db, LabelClient()).process(capture_id)

    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 0
        reasons = [
            row[0]
            for row in conn.execute(
                "SELECT reason FROM decisions WHERE outcome='skipped' ORDER BY id"
            )
        ]
    assert reasons and "no finite verb" in reasons[0]


class FormSentenceClient:
    """The judge rejects a bare form template as too thin."""

    def call(self, stage, schema, data, *args, **kwargs):
        if stage == "normalize":
            return schema.model_validate(
                {
                    "mostly_german": True,
                    "reason": "Form sentence",
                    "source_text": "Ich möchte ein Auto umschreiben.",
                    "sentences": ["Ich möchte ein Auto umschreiben."],
                }
            )
        if stage == "extract":
            return schema.model_validate(
                {
                    "candidates": [
                        {
                            "sentence_id": 0,
                            "lemma": "umschreiben",
                            "surface": "umschreiben",
                            "pos": "verb",
                            "sense_gloss": "to transfer ownership",
                            "sense_key": "transfer-ownership",
                            "is_phrase": False,
                            "cefr_estimate": "B1",
                            "domain": "administration",
                        }
                    ]
                }
            )
        if stage == "judge":
            return schema.model_validate(
                {
                    "judgments": [
                        {
                            "item_id": item["item_id"],
                            "keep": False,
                            "reason": "bare form template, no context for the sense",
                        }
                        for item in data["items"]
                    ]
                }
            )
        raise AssertionError(f"a dropped item must not reach {stage}")


def test_a_thin_form_sentence_is_rejected_as_banal_not_as_long(settings):
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v1/captures",
            headers={"Authorization": "Bearer " + "a" * 48},
            json={"text": "Ich möchte ein Auto umschreiben.", "client_ts": now()},
        )
        assert response.status_code == 202
        capture_id = response.json()["id"]

    db = Database(settings.data_dir)
    Pipeline(settings, db, FormSentenceClient()).process(capture_id)

    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 0
        outcome, reason = conn.execute(
            "SELECT outcome, reason FROM decisions WHERE stage='judge'"
        ).fetchone()
    assert outcome == "dropped"
    assert "template" in reason, reason
