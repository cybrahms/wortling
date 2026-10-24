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
    cuts_mid_word,
    preserve_short_source,
    segment_candidates,
    shortest_span,
    validate_normalized,
)
from wortling.pipeline.schemas import Extracted, GeneratedCards, Normalized

# Verbatim, including the spacing and the OCR truncation in "Sendu".
RUN_ON = (
    "Wähle diese Option für Kauf auf Rechnung. erhältst Statusmeldungen zu deiner Sendu "
    "Kauf auf Rechnung steht die Sendungsverfolgung nicht zur Verfügung."
)


def test_run_on_capture_is_segmented_into_card_sized_spans():
    spans = segment_candidates(RUN_ON)

    assert len(spans) >= 3, spans
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

    index = shortest_span(spans, "Sendungsverfolgung")

    assert index is not None
    assert spans[index] != RUN_ON
    assert "Sendungsverfolgung" in spans[index]
    assert len(spans[index]) < len(RUN_ON)
    # The first clause's word still resolves to the first clause.
    assert "Rechnung" in spans[shortest_span(spans, "Rechnung")]


def test_a_normal_sentence_is_left_whole():
    """"Do not shorten a sentence by rewriting it" still holds below the cap."""
    sentence = "Bitte bringen Sie Ihre Unterlagen mit."

    assert segment_candidates(sentence) == [sentence]


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
                # The payload is the span list; sentence_id is the index. Pick the
                # longest span carrying the word, the way a model that ignored the
                # cap would.
                index = max(
                    (
                        number
                        for number, text in enumerate(data["sentences"])
                        if "Sendungsverfolgung" in text
                    ),
                    key=lambda number: len(data["sentences"][number]),
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
    offered = extract_payload["sentences"]
    assert RUN_ON not in offered, "the run-on reached extraction intact"
    assert all(len(text) <= SPAN_MAX_CHARS for text in offered)

    with db.connect() as conn:
        stored = conn.execute("SELECT sentence FROM items").fetchone()[0]
    assert stored != RUN_ON
    assert "Sendungsverfolgung" in stored
    assert len(stored) <= SPAN_MAX_CHARS
