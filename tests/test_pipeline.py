import json
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from wortling.api import create_app
from wortling.db import Database, now
from wortling.llm.client import LLMClient
from wortling.pipeline.engine import Pipeline, validate_cloze
from wortling.pipeline.schemas import Normalized


class RecordedClient:
    """Deterministic provider recording; dynamic item IDs are protocol metadata."""

    def __init__(self, text="Die Sitzung beginnt morgen."):
        self.text = text
        self.stages = []

    def call(self, stage, schema, data, user, run_id, **kwargs):
        self.stages.append(stage)
        if stage == "normalize":
            value = {
                "mostly_german": True,
                "reason": "German source",
                "source_text": self.text,
                "sentences": [self.text],
            }
        elif stage == "extract":
            value = {
                "candidates": [
                    {
                        "sentence_id": 0,
                        "lemma": "die Sitzung",
                        "surface": "Sitzung",
                        "pos": "noun",
                        "sense_gloss": "meeting",
                        "sense_key": "meeting",
                        "is_phrase": False,
                        "cefr_estimate": "B1",
                        "domain": "school",
                    }
                ]
            }
        elif stage == "judge":
            value = {
                "judgments": [
                    {"item_id": item["item_id"], "keep": True, "reason": "Useful school vocabulary"}
                    for item in data["items"]
                ]
            }
        else:
            text = self.text.replace("Sitzung", "{{c1::Sitzung::meeting}}")
            value = {
                "cards": [
                    {
                        "item_id": item["item_id"],
                        "text": text,
                        "lemma": "die Sitzung, -en",
                        "forms": "",
                        "gloss": "meeting",
                        "sentence_en": "The meeting starts tomorrow.",
                    }
                    for item in data["items"]
                ]
            }
        return schema.model_validate(value)


def capture(settings, text):
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v1/captures",
            headers={"Authorization": "Bearer " + "a" * 48},
            json={"text": text, "client_ts": now()},
        )
        assert response.status_code == 202
        return response.json()["id"]


def test_pipeline_dedupes_encounters_and_records_stages(settings):
    first = capture(settings, "Die Sitzung beginnt morgen.")
    db = Database(settings.data_dir)
    client = RecordedClient()
    pipeline = Pipeline(settings, db, client)
    pipeline.process(first)
    other = capture(settings, "Die Sitzung ist wichtig.")
    client.text = "Die Sitzung ist wichtig."
    pipeline.process(other)
    pipeline.process(other)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM encounters").fetchone()[0] == 2
        card = conn.execute("SELECT * FROM cards").fetchone()
        assert card["status"] == "pending"
        assert "Die Sitzung ist wichtig." in json.loads(card["fields_json"])["Examples"]
        assert {row[0] for row in conn.execute("SELECT DISTINCT stage FROM decisions")} == {
            "normalize",
            "extract",
            "known",
            "judge",
            "generate",
            "dedupe",
        }


@pytest.mark.parametrize(
    "status,explicit,outcome,promotion",
    [
        ("known", True, "known_but_looked_up", 0),
        ("queued", True, "queued", 1),
        ("learning", False, "learning", 0),
        ("suspended", False, "suspended", 0),
    ],
)
def test_known_filter(settings, status, explicit, outcome, promotion):
    text = "die Sitzung" if explicit else "Die Sitzung beginnt morgen."
    capture_id = capture(settings, text)
    db = Database(settings.data_dir)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO known_index VALUES (?,?,?,?,?,?,?)",
            ("test", "sitzung", status, "Test", 123, None, now()),
        )
    client = RecordedClient(text)
    Pipeline(settings, db, client).process(capture_id)
    assert client.stages == ["normalize", "extract"]
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 0
        assert (
            conn.execute("SELECT outcome FROM decisions WHERE stage='known'").fetchone()[0]
            == outcome
        )
        assert conn.execute("SELECT COUNT(*) FROM promotions").fetchone()[0] == promotion


def test_explicit_lookup_skips_judge(settings):
    capture_id = capture(settings, "die Sitzung")
    client = RecordedClient("die Sitzung")
    Pipeline(settings, Database(settings.data_dir), client).process(capture_id)
    assert client.stages == ["normalize", "extract", "generate"]


@pytest.mark.parametrize(
    "text",
    [
        "Ich {{c2::rufe}} an.",
        "Ich rufe an.",
        "Ich {{c1::telefonierte}}.",
        "<script>{{c1::rufe}}</script>",
    ],
)
def test_rewritten_or_invalid_cloze_rejected(text):
    with pytest.raises(ValueError):
        validate_cloze(text, "Ich rufe morgen an.")


def test_split_cloze_and_original_trim():
    validate_cloze("Ich {{c1::rufe::call}} morgen {{c1::an}}.", "Ich rufe morgen an.")
    validate_cloze("{{c1::rufe}} morgen an.", "Ich rufe morgen an.")


def test_llm_retries_schema_failure_once_and_logs_tokens(settings):
    db = Database(settings.data_dir)
    db.initialize(settings)
    user = settings.users[0]
    run_id = str(uuid.uuid4())
    capture_id = capture(settings, "die Sitzung")
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO processing_runs(id,capture_id,prompt_version,status,started_at) "
            "VALUES (?,?,?,'running',?)",
            (run_id, capture_id, "v1", now()),
        )
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        content = (
            "wrong"
            if len(requests) == 1
            else json.dumps(
                {
                    "mostly_german": True,
                    "reason": "German",
                    "source_text": "die Sitzung",
                    "sentences": ["die Sitzung"],
                }
            )
        )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10},
            },
        )

    client = LLMClient(
        settings, db, transport=httpx.MockTransport(handler), token_provider=lambda: "x"
    )
    result = client.call("normalize", Normalized, {"text": "die Sitzung"}, user, run_id)
    assert result.mostly_german
    assert len(requests) == 2
    assert requests[0]["response_format"]["json_schema"]["strict"] is True
    with db.connect() as conn:
        assert [row[0] for row in conn.execute("SELECT outcome FROM llm_calls ORDER BY id")] == [
            "schema_failure",
            "ok",
        ]
        assert conn.execute("SELECT SUM(tokens_in) FROM llm_calls").fetchone()[0] == 40


def test_cloze_builder_preserves_printed_lines_and_separable_parts():
    from wortling.pipeline.engine import build_cloze

    sentence = "bitte bringen Sie zum Elternabend\nIhre schriftliche Einverständniserklärung mit."
    result = build_cloze(sentence, "bringen … mit", "bring along")
    assert result == (
        "bitte {{c1::bringen::bring along}} Sie zum Elternabend\n"
        "Ihre schriftliche Einverständniserklärung {{c1::mit}}."
    )
    validate_cloze(result, sentence)


def test_cloze_builder_preserves_selected_context_even_over_25_words():
    from wortling.pipeline.engine import build_cloze

    sentence = " ".join([f"Wort{i}" for i in range(60)])
    text = build_cloze(sentence, "Wort30", "word thirty")
    from wortling.pipeline.engine import CLOZE

    assert CLOZE.sub(lambda match: match[1], text) == sentence
    assert "{{c1::Wort30::word thirty}}" in text
    validate_cloze(text, sentence)


def test_long_request_keeps_modal_and_full_translation_context():
    from wortling.pipeline.engine import CLOZE, build_cloze

    sentence = (
        "wir sind gerade im Urlaub und wollten euch fragen, ob ihr bitte die "
        "Pakete vor unserer Haustür für uns entgegennehmen und kurz bei euch "
        "aufbewahren könntet."
    )
    text = build_cloze(sentence, "entgegennehmen", "receive on our behalf")
    assert CLOZE.sub(lambda match: match[1], text) == sentence
    assert text.endswith("aufbewahren könntet.")


def test_whole_label_keeps_a_usable_english_question():
    from wortling.pipeline.engine import build_cloze

    assert build_cloze("Trittbrett", "Trittbrett", "scooter standing platform") == (
        "{{c1::Trittbrett::scooter standing platform}}"
    )


def test_noun_plural_is_not_repeated_in_supporting_forms():
    from wortling.pipeline.engine import supporting_forms

    assert supporting_forms("Plural: die Beeinträchtigungen", "noun") == ""
    assert supporting_forms("Nom. die Bezahlung, Dat. der Bezahlung", "noun") == ""
    assert supporting_forms("—", "adjective") == ""
    assert supporting_forms("bat · hat gebeten", "phrase") == "bat · hat gebeten"


def test_generate_validator_rejects_missing_noun_plural():
    from wortling.pipeline.engine import validate_generated
    from wortling.pipeline.schemas import GeneratedCards

    value = GeneratedCards.model_validate(
        {
            "cards": [
                {
                    "item_id": "1",
                    "text": "unused draft",
                    "lemma": "die Rückmeldung",
                    "forms": "",
                    "gloss": "feedback",
                    "sentence_en": "feedback",
                }
            ]
        }
    )
    with pytest.raises(ValueError, match="plural"):
        validate_generated(value, [{"item_id": "1", "pos": "noun"}])


def test_split_surface_is_not_accepted_as_dictionary_lemma():
    from pydantic import ValidationError

    from wortling.pipeline.schemas import Candidate

    data = {
        "sentence_id": 0,
        "lemma": "bringen … mit",
        "surface": "bringen … mit",
        "pos": "verb",
        "sense_gloss": "bring along",
        "sense_key": "bring_along",
        "is_phrase": False,
        "cefr_estimate": "A2",
        "domain": "school",
    }
    with pytest.raises(ValidationError, match="dictionary form"):
        Candidate.model_validate(data)
    data["lemma"] = "bringen"
    with pytest.raises(ValidationError, match="reattached"):
        Candidate.model_validate(data)
    data["lemma"] = "mitbringen"
    assert Candidate.model_validate(data).lemma == "mitbringen"


def test_generated_dictionary_word_cannot_change_the_dedupe_key():
    from wortling.pipeline.engine import validate_generated
    from wortling.pipeline.schemas import GeneratedCards

    value = GeneratedCards.model_validate(
        {
            "cards": [
                {
                    "item_id": "1",
                    "text": "unused draft",
                    "lemma": "die Besprechung, -en",
                    "forms": "",
                    "gloss": "meeting",
                    "sentence_en": "meeting",
                }
            ]
        }
    )
    with pytest.raises(ValueError, match="extracted dictionary lemma"):
        validate_generated(value, [{"item_id": "1", "pos": "noun", "lemma_key": "sitzung"}])


SCOOTER_LABELS = [
    "Überfüllter Parkplatz",
    "App-Fehler",
    "Niedriger Batteriestand",
    "Trittbrett",
    "Parken verweigert",
    "Kosten",
    "Verbotszone",
    "Beschleunigung",
    "Bremsen",
    "Telefonhalterung",
    "Reifendruck niedrig",
    "Räder",
    "Zone mit reduzierter Geschwindigkeit",
    "GPS-Standort",
    "Fahrradwege",
    "Sonstiges",
    "Abschicken",
]


def test_short_screenshot_labels_reach_extraction_even_if_normalize_omits_them(settings):
    from wortling.pipeline.schemas import Extracted

    transcript = "\n".join(SCOOTER_LABELS)
    capture_id = capture(settings, transcript)

    class SparseNormalizer(RecordedClient):
        def call(self, stage, schema, data, *args, **kwargs):
            if stage == "normalize":
                return Normalized(
                    mostly_german=True,
                    reason="German app screen",
                    source_text=transcript,
                    sentences=[SCOOTER_LABELS[0], SCOOTER_LABELS[2]],
                )
            assert stage == "extract"
            assert [sentence["sentence_id"] for sentence in data["sentences"]] == list(
                range(len(data["sentences"]))
            )
            for label in SCOOTER_LABELS:
                assert any(label in sentence["text"] for sentence in data["sentences"]), label
            assert all(sentence["text"] in transcript for sentence in data["sentences"])
            return Extracted(candidates=[])

    Pipeline(settings, Database(settings.data_dir), SparseNormalizer()).process(capture_id)


def test_short_source_keeps_complete_split_verb_sentence():
    from wortling.pipeline.engine import preserve_short_source

    sentence = "Bitte bringen Sie\nIhre Unterlagen mit."
    value = Normalized(
        mostly_german=True,
        reason="German",
        source_text=sentence + "\nAbschicken",
        sentences=[sentence],
    )
    preserve_short_source(value)
    assert value.sentences == [sentence, "Abschicken"]


def test_phrase_cannot_be_deduped_against_its_base_verb():
    from wortling.pipeline.schemas import Candidate

    data = {
        "sentence_id": 0,
        "lemma": "laufen",
        "surface": "schief gelaufen",
        "pos": "verb",
        "sense_gloss": "go wrong",
        "sense_key": "go-wrong",
        "is_phrase": True,
        "cefr_estimate": "B1",
        "domain": "general",
    }
    with pytest.raises(ValueError, match="whole expression"):
        Candidate.model_validate(data)
    data.update(lemma="schief laufen", pos="phrase")
    assert Candidate.model_validate(data).lemma == "schief laufen"


def test_extraction_ids_include_skipped_metadata_and_senses_are_lookup_keys(settings):
    db = Database(settings.data_dir)
    original = capture(settings, "Die Sitzung beginnt morgen.")
    Pipeline(settings, db, RecordedClient()).process(original)
    source = "12:30\nDie Sitzung beginnt morgen.\nshop.example.invalid"
    second = capture(settings, source)

    class IndexedClient(RecordedClient):
        def call(self, stage, schema, data, *args, **kwargs):
            if stage == "normalize":
                return Normalized(
                    mostly_german=True,
                    reason="German",
                    source_text=source,
                    sentences=source.splitlines(),
                )
            if stage == "extract":
                assert data["sentences"] == [
                    {"sentence_id": 0, "text": "12:30"},
                    {"sentence_id": 1, "text": "Die Sitzung beginnt morgen."},
                    {"sentence_id": 2, "text": "shop.example.invalid"},
                ]
                assert data["existing_senses"] == [
                    {"lemma_key": "sitzung", "sense_key": "meeting", "gloss": "meeting"}
                ]
                value = super().call(stage, schema, data, *args, **kwargs)
                value.candidates[0].sentence_id = 1
                return value
            return super().call(stage, schema, data, *args, **kwargs)

    Pipeline(settings, db, IndexedClient()).process(second)
    with db.connect() as conn:
        assert (
            conn.execute("SELECT status FROM captures WHERE id=?", (second,)).fetchone()[0]
            == "processed"
        )


def test_a_short_surface_quotes_the_whole_word_not_part_of_one():
    """Rule: do not leave part of an inflected word unclozed."""
    from wortling.pipeline.engine import build_cloze

    text = build_cloze("Bitte reichen Sie eine schriftliche Entschuldigung ein.", "ein", "in")
    assert "{{c1::ein::in}}" in text
    assert "{{c1::ein}}e" not in text
    assert "{{c1::ein::in}}." in text


def test_a_surface_that_only_occurs_inside_a_word_is_refused():
    from wortling.pipeline.engine import build_cloze

    with pytest.raises(ValueError, match="inside a longer word"):
        build_cloze("Die Anmeldung ist da.", "Anmeld", "registration")
