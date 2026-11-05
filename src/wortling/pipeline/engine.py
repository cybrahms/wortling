from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import unicodedata
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from wordfreq import zipf_frequency

from wortling.anki.reader import effective_status
from wortling.config import Settings
from wortling.db import Database, now
from wortling.lemma import lemma_key, sense_key
from wortling.llm.client import LLMClient
from wortling.pipeline.schemas import Extracted, GeneratedCards, Judgments, Normalized
from wortling.sources import fetch_source

CLOZE = re.compile(r"\{\{c1::(.*?)(?:::(.*?))?\}\}", re.DOTALL)


def validate_cloze(text: str, sentence: str) -> None:
    if not CLOZE.search(text) or re.search(r"\{\{c(?!1::)\d+::", text):
        raise ValueError("Exactly cloze number c1 is required")
    plain = CLOZE.sub(lambda match: match[1], text)
    if "{{" in plain or "}}" in plain or plain not in sentence:
        raise ValueError("Generated cloze rewrote the original sentence")


# German gender-inclusive writing joins a stem to its ending instead of inflecting
# it: Kurier*in, Lehrer:in, Kolleg_innen, Leser/innen. Those separators are inside
# the word, so a stem that stops there leaves part of the source word unclozed -
# the style guide names Kurier*in by name. A hyphen stays a boundary: E-Mail and
# Ein-Euro-Laden are compounds whose parts are legitimately closable.
WORD_JOINERS = frozenset("*:_/")


def _inside_word(sentence: str, index: int, step: int) -> bool:
    """True when the character at `index` continues the word the match abuts.

    `step` is -1 for the character before a match and +1 for the one after it.

    A joiner is inside a word only when the far side of it is too: "*" in
    "Kurier*in" continues the word because "i" follows, while the ":" in
    "Sie sagte:" does not because a space does. Reading the joiner alone made a
    colon at a clause end refuse a surface that was already the whole word, and
    that is the one refusal the model cannot repair - it is told to quote the
    whole word, and it already did. A run of joiners is read through, so
    "Kolleg*_innen" holds; the character beyond the run decides.
    """
    if not 0 <= index < len(sentence):
        return False
    if sentence[index].isalnum():
        return True
    if sentence[index] not in WORD_JOINERS:
        return False
    beyond = index + step
    while 0 <= beyond < len(sentence) and sentence[beyond] in WORD_JOINERS:
        beyond += step
    return 0 <= beyond < len(sentence) and sentence[beyond].isalnum()


def whole_word_at(sentence: str, part: str, position: int) -> int:
    """Index of `part` at or after `position` that is not inside a longer word.

    A short surface is a substring of a longer word often enough to matter: "ein"
    in "eine" turned "Bitte reichen Sie eine ... ein" into "{{c1::ein}}e", and
    "Kurier" in "Kurier*in" would leave the inclusive ending outside the cloze.
    The style guide says not to leave part of an inflected or inclusive word
    unclozed, so the occurrence has to be word-bounded.
    """
    start = position
    while (found := sentence.find(part, start)) >= 0:
        end = found + len(part)
        if not (_inside_word(sentence, found - 1, -1) or _inside_word(sentence, end, 1)):
            return found
        start = found + 1
    return -1


def locate_surface(sentence: str, part: str, position: int) -> tuple[int, str]:
    """(index, "") when `part` sits there as a whole word, else (-1, reason).

    One place decides both where a surface is and why it is not usable there, so
    the extraction validator can hand the reason to the model (which retries in
    the same call) rather than the same sentence being rediscovered later as a
    failed capture.
    """
    found = whole_word_at(sentence, part, position)
    if found >= 0:
        return found, ""
    if whole_word_at(sentence, part, 0) >= 0:
        return -1, (
            f"Surface {part!r} already appeared before the previous part; "
            "quote the parts in the order they are printed"
        )
    if sentence.find(part) >= 0:
        return -1, (
            f"Surface {part!r} only occurs inside a longer word; "
            "quote the whole source word instead of part of it"
        )
    return -1, f"Surface {part!r} is absent from the sentence"


def build_cloze(sentence: str, surface: str, gloss: str) -> str:
    """Cloze the selected authentic context without cropping its grammar.

    Context selection belongs to normalization, where complete meanings can be
    considered. A word window here loses governing verbs and invalidates the
    full-context translation. Labels remain valid contexts with English hints.
    """
    spans = []
    position = 0
    for part in re.split(r"\s*(?:…|\.\.\.)\s*", surface):
        part = part.strip()
        found, problem = locate_surface(sentence, part, position) if part else (-1, "")
        if found < 0:
            # Last-ditch assertion: validate_extraction normally catches this while
            # the model can still be told, so reaching here means the pipeline lost
            # its own guard.
            raise ValueError(problem or "Cloze target is absent from the original sentence")
        spans.append((found, found + len(part)))
        position = found + len(part)
    hint = gloss.replace("::", ":").replace("{{", "").replace("}}", "")
    result = sentence
    for index, (left, right) in reversed(list(enumerate(spans))):
        target = sentence[left:right]
        cloze = "{{c1::" + target + ("::" + hint if index == 0 else "") + "}}"
        result = result[:left] + cloze + result[right:]
    validate_cloze(result, sentence)
    return result


def validate_extraction(extracted: Extracted, sentences: list[str]) -> None:
    """Refuse a surface the cloze builder could not quote, while the model can fix it.

    This runs as the extract call's validator, so a refusal is retried inside that
    call with the reason attached and the model gets the chance to quote the whole
    word. Catching the same defect later, in build_cloze, would cost the whole
    capture instead of one retry.
    """
    for item in extracted.candidates:
        if item.sentence_id >= len(sentences):
            raise ValueError("Extract referenced a missing sentence")
        sentence = sentences[item.sentence_id]
        position = 0
        for segment in re.split(r"\s*(?:…|\.\.\.)\s*", item.surface):
            segment = segment.strip()
            found, problem = locate_surface(sentence, segment, position) if segment else (-1, "")
            if found < 0:
                raise ValueError(
                    f"{problem or f'Surface {item.surface!r} is absent'} "
                    f"(sentence {item.sentence_id}). Use the original spelling, whole "
                    "words, and the split parts in the order they are printed."
                )
            position = found + len(segment)


# A card quotes one span. Long enough to read on a phone, short enough that the
# card shows the clause the word came from instead of a paragraph.
SPAN_MAX_CHARS = 100
SPAN_MAX_WORDS = 15

# Sentence punctuation, but not every period: "z. B.", "Nr. 3" and "d. h." are
# inside a sentence, and splitting there produces "Wir liefern z." -- worse than
# the paragraph it replaced. A boundary after . ! ? also requires the next word to
# start a sentence; ; : and dashes need no such evidence.
_CLAUSE_SPLIT = re.compile(
    r"(?<=\s)(?=(?:und|oder|aber|denn|sondern|weil|dass|daß|wenn|obwohl|damit|während|"
    r"bevor|nachdem|sobald|sofern|sowie|dennoch|trotzdem|deshalb|deswegen)\b)",
    re.IGNORECASE,
)
_ABBREVIATIONS = frozenset(
    """nr ca str tel bzw usw ggf inkl exkl max min mio mrd std dr abs art
    z b d h u a s o vgl evtl ggfs""".split()
)
_WORD = re.compile(r"\w", re.UNICODE)
_WORD_TOKEN = re.compile(r"[\wäöüÄÖÜß]+")

# Words that must not end a candidate: a span that stops after "zur" or "und"
# teaches a phrase that does not exist. Closed-class only -- a span ending in
# "nicht" or "ist" reads fine and is left alone.
_TRAILING_FUNCTION = frozenset(
    """der die das den dem des ein eine einen einem einer eines
    zu zur zum auf in im ins an am ans mit für von vom bei beim nach aus über
    unter vor seit durch gegen ohne um bis als wie und oder aber denn sondern
    weil dass wenn obwohl damit während bevor nachdem sobald sofern
    er sie es ich du wir ihr man sich""".split()
)


def _split_at(text: str, pattern: re.Pattern[str]) -> list[str]:
    cuts = [0] + [match.end() for match in pattern.finditer(text)] + [len(text)]
    return [
        text[left:right]
        for left, right in zip(cuts[:-1], cuts[1:], strict=True)
        if text[left:right].strip()
    ]


def _closes_a_sentence(text: str, end: int) -> bool:
    """Whether the punctuation run ending at `end` closes a sentence."""
    punctuation = text[end - 1]
    if punctuation in "…;:–—":
        return True
    token = re.search(r"(\S+)$", text[: end - 1])
    if token is None:
        return True
    word = token.group(1).strip("\"'„“”()[]").lower()
    if len(word) == 1 or word in _ABBREVIATIONS:
        return False
    following = text[end:].lstrip()
    return bool(following) and following[:1].isupper()


def _split_sentences(text: str) -> list[str]:
    cuts: list[int] = []
    for match in re.finditer(r"[.!?…;:–—]+\s+", text):
        if _closes_a_sentence(text, match.start() + len(match.group(0).rstrip())):
            cuts.append(match.end())
    boundaries = [0, *cuts, len(text)]
    return [
        text[left:right]
        for left, right in zip(boundaries[:-1], boundaries[1:], strict=True)
        if text[left:right].strip()
    ]


def _split_lines(text: str) -> list[str]:
    """A printed line break before a capitalised word starts a new span.

    OCR captures wrap mid-sentence, so a newline is not a boundary on its own --
    but when the next line begins a sentence, joining it to the previous line is
    how two unrelated sentences ended up in one card. A line continuing after a
    comma, colon or dash is left joined.
    """
    pieces, start = [], 0
    for match in re.finditer(r"\n\s*", text):
        before = text[: match.start()].rstrip()
        after = text[match.end() :].lstrip()
        if not before or before[-1] in ",:;–—-":
            continue
        if not after or not after[:1].isupper():
            continue
        pieces.append(text[start : match.start()])
        start = match.end()
    pieces.append(text[start:])
    return [piece for piece in pieces if piece.strip()]


def _fits(text: str, max_chars: int, max_words: int) -> bool:
    return len(text) <= max_chars and len(text.split()) <= max_words


def _cut_at_word_boundaries(text: str, max_chars: int, max_words: int) -> list[str]:
    """Fill the cap greedily, cutting only between words. Last resort for a clause.

    A greedy fill that stops exactly at the cap can end on a preposition or an
    article ("... steht die Sendungsverfolgung nicht zur" / "Verfügung."), which
    is a phrase no one can learn from. So the end is walked back over
    closed-class words before the cut is taken, and the remainder starts with
    them instead.
    """
    words = list(re.finditer(r"\S+", text))
    pieces: list[str] = []
    start = 0
    while start < len(words):
        index = start + 1
        while index < len(words) and _fits(
            text[words[start].start() : words[index].end()], max_chars, max_words
        ):
            index += 1
        end = index
        while (
            end - start > 2
            and words[end - 1].group(0).strip(".,;:!?").lower() in _TRAILING_FUNCTION
        ):
            end -= 1
        if end == start + 1:
            end = index  # keep the greedy cut rather than emit one word at a time
        pieces.append(text[words[start].start() : words[end - 1].end()])
        start = end
    return pieces


def segment_candidates(
    text: str, max_chars: int = SPAN_MAX_CHARS, max_words: int = SPAN_MAX_WORDS
) -> list[str]:
    """Cut one verbatim span into candidates short enough to quote on a card.

    A span that already fits is returned untouched: splitting it would only take
    a correct sentence apart, which is the defect this function exists to fix.

    Otherwise four turns of the same screw, each applied only where the previous
    one left an oversized piece: a closing sentence punctuation mark, a printed
    line break before a new sentence, a clause boundary, and finally a cut at the
    cap between two words. Every piece is a contiguous, word-bounded slice of
    `text`, so nothing is rewritten, repaired or reordered -- a token the source
    truncated stays truncated, and is never stitched back into a word.
    """
    if _fits(text, max_chars, max_words):
        return [text.strip()]

    pieces: list[str] = []
    for sentence in _split_sentences(text):
        if _fits(sentence, max_chars, max_words):
            pieces.append(sentence)
            continue
        for line in _split_lines(sentence):
            if _fits(line, max_chars, max_words):
                pieces.append(line)
                continue
            for clause in _split_at(line, _CLAUSE_SPLIT):
                if _fits(clause, max_chars, max_words):
                    pieces.append(clause)
                else:
                    pieces.extend(_cut_at_word_boundaries(clause, max_chars, max_words))
    return [piece.strip() for piece in pieces if piece.strip()]


def cuts_mid_word(source: str, span: str) -> bool:
    """True when every occurrence of `span` in `source` begins or ends inside a word.

    `span` must already be known to be a verbatim substring. This is the check
    that stops 'Sendu' from being quoted as German when the source says
    'Sendung': a span sliced by character offset looks fine and is the reason
    the prompt alone is not trusted.
    """
    start = 0
    while True:
        index = source.find(span, start)
        if index < 0:
            return True
        end = index + len(span)
        left_ok = index == 0 or not _WORD.match(source[index - 1])
        right_ok = end == len(source) or not _WORD.match(source[end])
        if left_ok and right_ok:
            return False
        start = index + 1


def shortest_span(sentences: list[str], surface: str) -> int | None:
    """Index of the shortest candidate that contains the whole surface, or None.

    Extraction names a sentence_id, and a model that was given both a clause and
    the paragraph containing it will sometimes pick the paragraph. Rebinding to
    the shortest candidate that still holds the surface makes 'the card quotes
    the clause the word came from' a property of the code rather than a hope.

    This is the one place the model's read is discarded for a heuristic. When the
    same surface occurs in two spans, the shorter one wins even if the gloss was
    written for the other occurrence; the lemma and sense are unchanged, so the
    card is still correct, but the quoted context may be the second sighting
    rather than the one that produced the gloss.
    """
    parts = [part.strip() for part in re.split(r"\s*(?:…|\.\.\.)\s*", surface) if part.strip()]
    best: int | None = None
    for index, sentence in enumerate(sentences):
        position = 0
        for part in parts:
            found = sentence.find(part, position)
            if found < 0:
                break
            position = found + len(part)
        else:
            if best is None or len(sentence) < len(sentences[best]):
                best = index
    return best
# The characters a model rewrites without meaning to. It reproduces the words of
# a pasted text and re-keys the punctuation around them: a non-breaking space
# becomes a space (this capture), a curly quote becomes straight, an en dash
# becomes a hyphen. None of those change what the letter says, so none of them
# may cost a capture. NFKC handles the compatibility cases (the "ﬁ" ligature,
# the non-breaking space); the table handles the rest and drops the characters
# that are invisible when printed.
_FOLDED = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u2032": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u201f": '"',
        "\u2033": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
        "\u00ad": None,
        "\u200b": None,
        "\u200c": None,
        "\u200d": None,
        "\ufeff": None,
    }
)


def fidelity_form(text: str | None) -> str:
    """The words of a text, for the quote checks and for nothing else.

    The rule is that a sentence is the source's sentence, not that it reproduces
    its bytes. So both sides are folded: NFKC, the punctuation a model re-keys,
    and every run of Unicode whitespace or line ending down to one space. A
    paraphrase still differs here, in words or in order, which is why the fold is
    safe; only the characters that carry no meaning are forgiven.
    """
    folded = unicodedata.normalize("NFKC", text or "").translate(_FOLDED)
    return " ".join(folded.split())


def source_span(source: str, sentence: str) -> tuple[int, int] | None:
    """Where in the source this sentence is, or None when it is not there.

    A sentence that matches in fidelity form is quoted back from the source
    itself rather than from the model's rendering of it: a card that prints a
    straight quote where the letter printed a curly one is a small lie about the
    source, and the tolerance above is only safe if the original characters are
    what get stored.
    """
    wanted = fidelity_form(sentence).split()
    if not wanted:
        return None
    words = [(match.start(), match.end()) for match in re.finditer(r"\S+", source)]
    forms = [fidelity_form(source[start:end]) for start, end in words]
    width = len(wanted)
    for index in range(len(forms) - width + 1):
        if forms[index : index + width] == wanted:
            return words[index][0], words[index + width - 1][1]
    return None


def quoted_sentence(source: str, sentence: str) -> str | None:
    """The source's own characters for this sentence, or None if it is absent.

    Byte-identical sentences come back unchanged; so do sentences that differ
    only in the characters fidelity_form folds. Anything else is absent, and the
    caller refuses it rather than passing the model's paraphrase through.
    """
    if sentence in source:
        return sentence
    span = source_span(source, sentence)
    return source[span[0] : span[1]] if span else None


def validate_normalized(value: Normalized, source_text: str | None, image: bool) -> None:
    if not image and fidelity_form(value.source_text) not in {"", fidelity_form(source_text)}:
        raise ValueError("Normalize must not rewrite supplied source text")
    source = (
        ((source_text + "\n") if source_text else "") + value.source_text if image else source_text
    )
    for sentence in value.sentences:
        # The quoted slice is the source's own characters, so the mid-word check
        # runs on it rather than on the model's rendering: a sentence that folds a
        # curly quote or a non-breaking space is verbatim, and comparing its edges
        # with the source would read that folding as a cut inside a word.
        quoted = quoted_sentence(source or "", sentence)
        if quoted is None:
            raise ValueError(
                "Normalize must quote sentences from the source; "
                f"{sentence!r} is not one of them (same words, same order)"
            )
        # Quoting the source is not enough on its own: a span that begins or ends
        # inside a word is a substring of the source too ("Sendu" for "Sendung"),
        # and a card must never show one, so both checks stand rather than one
        # replacing the other.
        if cuts_mid_word(source or "", quoted):
            raise ValueError(
                "Normalize must not cut mid-word: "
                f"{sentence[:60]!r} starts or ends inside a word of the source"
            )


def preserve_short_source(value: Normalized) -> None:
    """Short captures are learning material in full, including menu labels.

    Retain complete model-selected sentences (including split verbs), and append
    any source line that contributes a word no selected sentence covers. Long
    articles retain their bounded selection.

    Coverage is by word rather than by whole line on purpose: a segmented
    run-on is no longer equal to its source line, and an equality test would
    append the paragraph straight back and undo the segmentation.
    """
    lines = [line.strip() for line in value.source_text.splitlines() if line.strip()]
    if len(lines) > 30 or len(value.source_text.split()) > 1000:
        return
    covered = {
        token.lower() for sentence in value.sentences for token in _WORD_TOKEN.findall(sentence)
    }
    for line in lines:
        tokens = {token.lower() for token in _WORD_TOKEN.findall(line)}
        if tokens and tokens <= covered:
            continue
        value.sentences.append(line)
        covered |= tokens


def validate_judgments(value: Judgments, candidates: list[dict]) -> None:
    expected = {item["item_id"] for item in candidates}
    actual = {judgment.item_id for judgment in value.judgments}
    if expected != actual or len(value.judgments) != len(expected):
        raise ValueError("Judge must return every candidate ID exactly once")


def validate_generated(generated: GeneratedCards, items: list[dict]) -> None:
    by_id = {card.item_id: card for card in generated.cards}
    if set(by_id) != {item["item_id"] for item in items} or len(by_id) != len(generated.cards):
        raise ValueError("Generate must return every kept item ID exactly once")
    for item in items:
        card = by_id[item["item_id"]]
        if item["pos"] == "noun":
            if not re.match(r"^(?:der|die|das)(?:/(?:der|die|das))*\s+", card.lemma):
                raise ValueError("Noun lemma needs its German article")
            if not re.search(r"[,;(]|Plural|plural", card.lemma):
                raise ValueError(
                    "Noun lemma needs its plural or an explicit no-plural/plural-only note"
                )
        if item["pos"] == "verb" and not re.search(r"\b(?:haben|sein|hat|ist)\b", card.forms):
            raise ValueError("Verb forms need Präteritum, Partizip II and haben/sein auxiliary")
        if lemma_key(card.lemma) != item["lemma_key"]:
            raise ValueError("Generated lemma must match the extracted dictionary lemma")


def supporting_forms(forms: str, pos: str) -> str:
    """Noun plurals are already required in Lemma; don't repeat them below it."""
    if pos == "noun" or forms.strip() in {"", "—", "-", "Adjektiv", "Adverb"}:
        return ""
    return forms.strip()


class Pipeline:
    def __init__(self, settings: Settings, db: Database, client: LLMClient | None = None):
        self.settings = settings
        self.db = db
        self.client = client or LLMClient(settings, db)

    def decision(self, conn, run_id, item_id, stage, outcome, reason):
        llm_stage = stage in self.settings.llm.models
        conn.execute(
            "INSERT INTO decisions(run_id,item_id,stage,outcome,reason,model,prompt_version,"
            "created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                run_id,
                item_id,
                stage,
                outcome,
                reason,
                self.settings.llm.models[stage] if llm_stage else None,
                self.settings.llm.prompt_versions.get(stage, "code.v1"),
                now(),
            ),
        )

    def claim(self) -> str | None:
        users = [user.id for user in self.settings.users if user.processing_enabled]
        if not users:
            return None
        timestamp = now()
        lease = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT j.capture_id FROM jobs j JOIN captures c ON c.id=j.capture_id "
                f"WHERE c.user_id IN ({','.join('?' for _ in users)}) "
                "AND ((j.status='pending' AND next_attempt_at<=?) "
                "OR (j.status='running' AND lease_until<?)) "
                "ORDER BY c.received_at LIMIT 1",
                (*users, timestamp, timestamp),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE jobs SET status='running',lease_until=?,attempts=attempts+1 "
                "WHERE capture_id=?",
                (lease, row[0]),
            )
            return row[0]

    def run_once(self) -> bool:
        capture_id = self.claim()
        if capture_id is None:
            return False
        try:
            self.process(capture_id)
        except Exception as exc:
            # Store a bounded error; provider wrappers never include credentials or response bodies.
            error = str(exc)[:1000]
            with self.db.connect() as conn:
                attempts = conn.execute(
                    "SELECT attempts FROM jobs WHERE capture_id=?", (capture_id,)
                ).fetchone()[0]
                status = "failed" if attempts >= 3 else "pending"
                retry = (datetime.now(UTC) + timedelta(minutes=2**attempts)).isoformat()
                conn.execute(
                    "UPDATE jobs SET status=?,error=?,lease_until=NULL,next_attempt_at=? "
                    "WHERE capture_id=?",
                    (status, error, retry, capture_id),
                )
                conn.execute(
                    "UPDATE captures SET status='error',error=? WHERE id=?", (error, capture_id)
                )
                conn.execute(
                    "UPDATE processing_runs SET status='error',error=?,finished_at=? "
                    "WHERE capture_id=? AND status='running'",
                    (error, now(), capture_id),
                )
                if status == "failed":
                    user_id = conn.execute(
                        "SELECT user_id FROM captures WHERE id=?", (capture_id,)
                    ).fetchone()[0]
                    conn.execute(
                        "INSERT INTO alerts(user_id,kind,message,created_at,capture_id) "
                        "VALUES (?,?,?,?,?)",
                        (user_id, "capture_failed", error, now(), capture_id),
                    )
        return True

    def process(self, capture_id: str) -> str:
        with self.db.connect() as conn:
            capture = dict(
                conn.execute("SELECT * FROM captures WHERE id=?", (capture_id,)).fetchone()
            )
        user = self.settings.learner(capture["user_id"])
        with self.db.connect() as conn:
            intent = conn.execute(
                "SELECT reprocess,explicit_lookup FROM jobs WHERE capture_id=?", (capture_id,)
            ).fetchone()
        reprocessing = capture["status"] == "reprocess_pending" or bool(intent and intent[0])
        if reprocessing:
            with self.db.connect() as conn:
                conn.execute(
                    "UPDATE cards SET status='dropped',updated_at=? "
                    "WHERE user_id=? AND anki_note_id IS NULL "
                    "AND status IN ('pending','blocked') AND id IN "
                    "(SELECT card_id FROM encounters WHERE capture_id=?)",
                    (now(), user.id, capture_id),
                )
        capture_day = (
            datetime.fromisoformat(capture["received_at"])
            .astimezone(ZoneInfo(user.timezone))
            .date()
            .isoformat()
        )
        run_id = str(uuid.uuid4())
        versions = json.dumps(self.settings.llm.prompt_versions, sort_keys=True)
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO processing_runs(id,capture_id,prompt_version,status,started_at) "
                "VALUES (?,?,?,'running',?)",
                (run_id, capture_id, versions, now()),
            )
        image = None
        if capture["image_path"]:
            image = (
                capture["image_mime"],
                base64.b64encode((self.db.data_dir / capture["image_path"]).read_bytes()).decode(),
            )
        source_text = capture["text"] if (capture["text"] or "").strip() else None
        if not source_text and not image and capture["url"]:
            with self.db.connect() as conn:
                prior = conn.execute(
                    "SELECT source_text FROM capture_sources WHERE capture_id=?", (capture_id,)
                ).fetchone()
            if prior:
                source_text = prior[0]
            else:
                source = fetch_source(capture["url"])
                with self.db.connect() as conn:
                    conn.execute(
                        "INSERT OR IGNORE INTO capture_sources VALUES (?,?,?,?,?,?,?)",
                        (
                            capture_id,
                            source.final_url,
                            source.content_type,
                            source.body,
                            source.text,
                            hashlib.sha256(source.body).hexdigest(),
                            now(),
                        ),
                    )
                    source_text = conn.execute(
                        "SELECT source_text FROM capture_sources WHERE capture_id=?", (capture_id,)
                    ).fetchone()[0]
        normalized = self.client.call(
            "normalize",
            Normalized,
            {"text": source_text, "url": capture["url"]},
            user,
            run_id,
            image=image,
            validator=lambda value: validate_normalized(value, source_text, bool(image)),
        )
        if source_text and image:
            normalized.source_text = source_text + "\n" + normalized.source_text
        if source_text and not image:
            if fidelity_form(normalized.source_text) not in {"", fidelity_form(source_text)}:
                raise ValueError("Normalize rewrote source text")
            normalized.source_text = source_text
        source = normalized.source_text or ""
        kept = []
        for sentence in normalized.sentences:
            quoted = quoted_sentence(source, sentence)
            if quoted is None:
                raise ValueError(
                    f"Normalize emitted a sentence absent from the source: {sentence!r}"
                )
            kept.append(quoted)
        normalized.sentences = kept
        preserve_short_source(normalized)
        # Cut what the model kept into card-sized candidates before anything
        # refers to a sentence_id, so extraction, the stored run and the card all
        # see the same spans. Duplicates are dropped: a retained line can equal a
        # candidate that a neighbouring line already produced.
        normalized.sentences = list(
            dict.fromkeys(
                piece for sentence in normalized.sentences for piece in segment_candidates(sentence)
            )
        )
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE processing_runs SET normalized_json=? WHERE id=?",
                (normalized.model_dump_json(), run_id),
            )
            self.decision(
                conn,
                run_id,
                None,
                "normalize",
                "kept" if normalized.mostly_german else "skipped_not_german",
                normalized.reason,
            )
        if not normalized.mostly_german:
            self.finish(capture_id, run_id, "skipped_not_german", reprocessing=reprocessing)
            return run_id
        requested_lookup = bool(intent and intent["explicit_lookup"])
        explicit = (
            requested_lookup or len(re.findall(r"\b[\wäöüÄÖÜß]+\b", normalized.source_text)) <= 3
        )
        with self.db.connect() as conn:
            existing_senses = [
                {
                    "lemma_key": row["lemma_key"],
                    "sense_key": row["sense_key"],
                    "gloss": json.loads(row["fields_json"])["Gloss"],
                }
                for row in conn.execute(
                    "SELECT lemma_key,sense_key,fields_json FROM cards WHERE user_id=? "
                    "ORDER BY (status='pending') DESC,updated_at DESC LIMIT 1000",
                    (user.id,),
                )
            ]
        extracted = self.client.call(
            "extract",
            Extracted,
            {
                "sentences": [
                    {"sentence_id": index, "text": sentence}
                    for index, sentence in enumerate(normalized.sentences)
                ],
                "existing_senses": existing_senses,
            },
            user,
            run_id,
            validator=lambda value: validate_extraction(value, normalized.sentences),
        )
        validate_extraction(extracted, normalized.sentences)
        candidates = []
        seen = set()
        with self.db.connect() as conn:
            self.decision(
                conn,
                run_id,
                None,
                "extract",
                "completed",
                f"Extracted {len(extracted.candidates)} candidates",
            )
            for item in extracted.candidates:
                if item.sentence_id >= len(normalized.sentences):
                    raise ValueError("Extract referenced a missing sentence")
                best = shortest_span(normalized.sentences, item.surface)
                if best is not None:
                    item.sentence_id = best
                sentence = normalized.sentences[item.sentence_id]
                # Split separable surfaces may contain an ellipsis, but each part must be verbatim.
                segments = [part.strip() for part in re.split(r"\s*(?:…|\.\.\.)\s*", item.surface)]
                position = 0
                for segment in segments:
                    found = sentence.find(segment, position)
                    if not segment or found < 0:
                        raise ValueError("Extract surface is absent from original sentence")
                    position = found + len(segment)
                key = lemma_key(item.lemma)
                sense = sense_key(item.sense_key)
                if not key or not sense or (key, sense) in seen:
                    continue
                seen.add((key, sense))
                item_id = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        item_id,
                        run_id,
                        capture_id,
                        item.sentence_id,
                        sentence,
                        item.lemma,
                        key,
                        sense,
                        item.surface,
                        item.pos,
                        item.sense_gloss,
                        item.cefr_estimate,
                        item.domain,
                        int(item.is_phrase),
                    ),
                )
                candidate = {
                    **item.model_dump(),
                    "item_id": item_id,
                    "lemma_key": key,
                    "sense_key": sense,
                    "sentence": sentence,
                }
                self.decision(conn, run_id, item_id, "extract", "candidate", item.sense_gloss)
                own = conn.execute(
                    "SELECT * FROM cards WHERE user_id=? AND lemma_key=? AND sense_key=?",
                    (user.id, key, sense),
                ).fetchone()
                if own:
                    self.encounter(conn, own, capture_id, sentence)
                    self.decision(
                        conn,
                        run_id,
                        item_id,
                        "known",
                        (
                            "known_but_looked_up"
                            if explicit
                            and conn.execute(
                                "SELECT 1 FROM known_index WHERE user_id=? AND lemma_key=? "
                                "AND status='known' LIMIT 1",
                                (user.id, key),
                            ).fetchone()
                            else "repeat_encounter"
                        ),
                        "Appended source example to existing mined note",
                    )
                    if own["anki_note_id"]:
                        self.promote(conn, user.id, own["anki_note_id"])
                    if not reprocessing:
                        continue
                matches = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT * FROM known_index WHERE user_id=? AND lemma_key=?", (user.id, key)
                    )
                ]
                status = (
                    None
                    if own and reprocessing and own["anki_note_id"]
                    else effective_status(matches)
                )
                if status:
                    outcome = "known_but_looked_up" if explicit and status == "known" else status
                    self.decision(
                        conn,
                        run_id,
                        item_id,
                        "known",
                        outcome,
                        f"Present in learner collection as {status}",
                    )
                    if status == "queued":
                        for match in matches:
                            if match["status"] == "queued":
                                self.promote(conn, user.id, match["note_id"])
                    continue
                self.decision(conn, run_id, item_id, "known", "absent", "Absent from known index")
                candidate["zipf_de"] = zipf_frequency(key, "de")
                candidate["frequency_rank"] = next(
                    (match["rank"] for match in matches if match["rank"] is not None), None
                )
                candidates.append(candidate)
        kept = candidates
        if explicit:
            with self.db.connect() as conn:
                for item in candidates:
                    self.decision(
                        conn,
                        run_id,
                        item["item_id"],
                        "judge",
                        "explicit_lookup",
                        "Learner explicitly requested this vocabulary; skip likely-known judgment"
                        if requested_lookup
                        else "At most three words; skip likely-known judgment",
                    )
        elif candidates:
            judgments = self.client.call(
                "judge",
                Judgments,
                {"profile": user.profile.model_dump(), "items": candidates},
                user,
                run_id,
                validator=lambda value: validate_judgments(value, candidates),
            )
            by_id = {judgment.item_id: judgment for judgment in judgments.judgments}
            expected = {item["item_id"] for item in candidates}
            if set(by_id) != expected or len(judgments.judgments) != len(expected):
                raise ValueError("Judge must return every candidate ID exactly once")
            kept = []
            with self.db.connect() as conn:
                for item in candidates:
                    judgment = by_id[item["item_id"]]
                    self.decision(
                        conn,
                        run_id,
                        item["item_id"],
                        "judge",
                        "kept" if judgment.keep else "dropped",
                        judgment.reason,
                    )
                    if judgment.keep:
                        kept.append(item)
        if kept:
            generated = self.client.call(
                "generate",
                GeneratedCards,
                {"items": kept},
                user,
                run_id,
                validator=lambda value: validate_generated(value, kept),
            )
            validate_generated(generated, kept)
            by_id = {card.item_id: card for card in generated.cards}
            if set(by_id) != {item["item_id"] for item in kept} or len(by_id) != len(
                generated.cards
            ):
                raise ValueError("Generate must return every kept item ID exactly once")
            for item in kept:
                card = by_id[item["item_id"]]
                card.text = build_cloze(item["sentence"], item["surface"], card.gloss)
            with self.db.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                for item in kept:
                    card = by_id[item["item_id"]]
                    fields = {
                        "Key": item["lemma_key"] + ":" + item["sense_key"],
                        "Text": html.escape(card.text),
                        "Lemma": html.escape(card.lemma),
                        "Forms": html.escape(supporting_forms(card.forms, item["pos"])),
                        "Gloss": html.escape(card.gloss),
                        "SentenceEN": html.escape(card.sentence_en),
                        "CEFR": item["cefr_estimate"],
                        "Source": html.escape(item["domain"] + " · " + capture_day),
                        "Examples": "",
                        "CaptureId": capture_id,
                    }
                    self.decision(
                        conn,
                        run_id,
                        item["item_id"],
                        "generate",
                        "completed",
                        "Original context verified; one cloze card",
                    )
                    old = conn.execute(
                        "SELECT * FROM cards WHERE user_id=? AND lemma_key=? AND sense_key=?",
                        (user.id, item["lemma_key"], item["sense_key"]),
                    ).fetchone()
                    if old:
                        self.encounter(conn, old, capture_id, item["sentence"])
                        # Reprocessing updates owned fields without touching scheduling.
                        prior = json.loads(old["fields_json"])
                        fields["Examples"] = prior.get("Examples", "")
                        status = (
                            "updated"
                            if old["anki_note_id"]
                            else "pending"
                            if reprocessing
                            else old["status"]
                        )
                        conn.execute(
                            "UPDATE cards SET fields_json=?,status=?,updated_at=? WHERE id=?",
                            (json.dumps(fields, ensure_ascii=False), status, now(), old["id"]),
                        )
                        outcome = "updated"
                    else:
                        card_id = str(uuid.uuid4())
                        conn.execute(
                            "INSERT INTO cards(id,user_id,lemma_key,sense_key,fields_json,"
                            "status,updated_at) VALUES (?,?,?,?,?,'pending',?)",
                            (
                                card_id,
                                user.id,
                                item["lemma_key"],
                                item["sense_key"],
                                json.dumps(fields, ensure_ascii=False),
                                now(),
                            ),
                        )
                        conn.execute(
                            "INSERT INTO encounters VALUES (?,?,?)",
                            (card_id, capture_id, item["sentence"]),
                        )
                        outcome = "pending"
                    self.decision(
                        conn,
                        run_id,
                        item["item_id"],
                        "dedupe",
                        outcome,
                        "Unique learner + lemma + sense",
                    )
        self.finish(capture_id, run_id, "processed", reprocessing=reprocessing)
        return run_id

    @staticmethod
    def promote(conn, user_id: str, note_id: int):
        conn.execute(
            "INSERT INTO promotions VALUES (?,?,?) ON CONFLICT(user_id,note_id) "
            "DO UPDATE SET requested_at=excluded.requested_at",
            (user_id, note_id, now()),
        )

    @staticmethod
    def encounter(conn, card, capture_id: str, sentence: str):
        added = conn.execute(
            "INSERT OR IGNORE INTO encounters VALUES (?,?,?)", (card["id"], capture_id, sentence)
        ).rowcount
        if not added:
            return
        examples = [
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT sentence FROM encounters WHERE card_id=? "
                "ORDER BY capture_id,sentence",
                (card["id"],),
            )
        ]
        fields = json.loads(card["fields_json"])
        fields["Examples"] = "<br>".join(html.escape(example) for example in examples)
        status = "updated" if card["anki_note_id"] else card["status"]
        conn.execute(
            "UPDATE cards SET fields_json=?,status=?,updated_at=? WHERE id=?",
            (json.dumps(fields, ensure_ascii=False), status, now(), card["id"]),
        )

    def finish(self, capture_id: str, run_id: str, status: str, *, reprocessing: bool = False):
        with self.db.connect() as conn:
            if reprocessing:
                obsolete = conn.execute(
                    "SELECT DISTINCT c.lemma_key,c.sense_key FROM cards c "
                    "JOIN encounters e ON e.card_id=c.id WHERE e.capture_id=? "
                    "AND c.status='dropped' AND c.anki_note_id IS NULL AND NOT EXISTS "
                    "(SELECT 1 FROM items i WHERE i.run_id=? AND i.lemma_key=c.lemma_key "
                    "AND i.sense_key=c.sense_key)",
                    (capture_id, run_id),
                ).fetchall()
                for card in obsolete:
                    self.decision(
                        conn,
                        run_id,
                        None,
                        "dedupe",
                        "obsolete_after_reprocess",
                        f"Pending {card['lemma_key']}:{card['sense_key']} "
                        "is absent from the new extraction",
                    )
            conn.execute(
                "UPDATE processing_runs SET status=?,finished_at=? WHERE id=?",
                (status, now(), run_id),
            )
            conn.execute("UPDATE captures SET status=?,error=NULL WHERE id=?", (status, capture_id))
            conn.execute(
                "UPDATE alerts SET acknowledged_at=? WHERE capture_id=? "
                "AND kind='capture_failed' AND acknowledged_at IS NULL",
                (now(), capture_id),
            )
            conn.execute(
                "UPDATE jobs SET status='done',lease_until=NULL,error=NULL,reprocess=0 "
                "WHERE capture_id=?",
                (capture_id,),
            )
