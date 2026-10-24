"""The card template and CSS are the style guide's rules 11-18, applied in place."""

from anki.collection import Collection

from wortling.anki.writer import AFMT, CSS, FIELDS, NOTE_TYPE, QFMT, ensure_note_type

OLD_QFMT = "{{cloze:Text}}"
OLD_AFMT = (
    "{{cloze:Text}}<hr><b>{{Lemma}}</b><br>{{Forms}}<br>{{Gloss}}"
    "<br>{{SentenceEN}}<hr>{{Source}}<br>{{Examples}}"
)
OLD_CSS = ".card { font-family: sans-serif; font-size: 24px; text-align: center; } "


def note_type_named(collection: Collection) -> list[str]:
    return [name for name in collection.models.all_names() if name == NOTE_TYPE]


def install_old_model(collection: Collection):
    model = collection.models.new(NOTE_TYPE)
    model["type"] = 1
    for name in FIELDS:
        collection.models.add_field(model, collection.models.new_field(name))
    template = collection.models.new_template("Context")
    template["qfmt"] = OLD_QFMT
    template["afmt"] = OLD_AFMT
    collection.models.add_template(model, template)
    model["css"] = OLD_CSS
    collection.models.add(model)
    return collection.models.by_name(NOTE_TYPE)


def test_existing_note_type_is_updated_in_place(tmp_path):
    collection = Collection(str(tmp_path / "collection.anki2"))
    try:
        model = install_old_model(collection)
        model_id = model["id"]
        note = collection.new_note(model)
        for name in FIELDS:
            note[name] = "value for " + name
        collection.add_note(note, collection.decks.id("Default"))

        updated = ensure_note_type(collection)

        assert updated["id"] == model_id
        assert [field["name"] for field in updated["flds"]] == FIELDS
        assert len(updated["tmpls"]) == 1
        assert updated["tmpls"][0]["qfmt"] == QFMT
        assert updated["tmpls"][0]["afmt"] == AFMT
        assert updated["css"] == CSS
        assert note_type_named(collection) == [NOTE_TYPE]
        assert collection.get_note(note.id).note_type()["id"] == model_id
    finally:
        collection.close()


def test_second_pass_changes_nothing(tmp_path):
    collection = Collection(str(tmp_path / "collection.anki2"))
    try:
        ensure_note_type(collection)
        first = collection.models.by_name(NOTE_TYPE)
        again = ensure_note_type(collection)
        assert again["id"] == first["id"]
        assert again["tmpls"][0]["qfmt"] == QFMT
        assert note_type_named(collection) == [NOTE_TYPE]
    finally:
        collection.close()


def test_front_shows_only_the_sentence_and_the_back_has_no_br_stack():
    assert "{{Lemma}}" not in QFMT
    assert "{{cloze:Text}}" in QFMT
    # Structure comes from block elements with CSS margins, not <br> chains.
    assert "<br>" not in AFMT
    for field in ["Forms", "SentenceEN", "Source", "Examples"]:
        assert "{{#" + field + "}}" in AFMT and "{{/" + field + "}}" in AFMT
    assert "max-width: 44rem" in CSS
    assert "@media (max-width: 600px)" in CSS
    assert "#2277bb" not in CSS
