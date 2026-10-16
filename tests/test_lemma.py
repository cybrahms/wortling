import pytest

from wortling.lemma import lemma_key


@pytest.mark.parametrize(
    "text,expected",
    [
        ("die Sitzung, -en", "sitzung"),
        ("Der <b>Baum</b>, ¨-e", "baum"),
        ("sich kümmern um", "kümmern um"),
        ("rückgängig machen", "rückgängig machen"),
        ("anrufen", "anrufen"),
        ("größer", "grösser"),
        ("die Straße", "strasse"),
        ("die Änderung, -en", "änderung"),
        ("die Nebenkostenabrechnung", "nebenkostenabrechnung"),
        ("das Essen", "essen"),
        ("&nbsp;die Sitzung (Pl. Sitzungen)", "sitzung"),
    ],
)
def test_lemma(text, expected):
    assert lemma_key(text) == expected
