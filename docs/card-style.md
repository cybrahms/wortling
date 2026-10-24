# Wortling card style

Wortling teaches useful unfamiliar German from the learner's real encounters:
messages, signs, letters, menus, interface labels, forms, pages and photographs.
This guide records the learner-approved design from 2026-10-24. It supersedes
the sentence-only proposal and automatic shortening rules. It does not change
the capture archive, vocabulary eligibility, note schema or review scheduling.

## One clear recall task

Test one lexical item or expression per card. Sentence-context cards retain
enough authentic German to make the meaning clear. Short labels remain valid
vocabulary cards: their English hint supplies the production question. Do not
remove hints globally or leave whole-label clozes with no usable cue. Multiple
discontinuous c1 spans for a separable verb still produce one review card.

There is no requirement for a finite verb, subject or minimum number of content
words. No blanket rejection of UI, shop or form text is allowed. Vocabulary
selection uses relevance and the learner's known index, not the visual shape of
its source. A known base verb does not imply a separable verb or idiom is known.

## Preserve coherent authentic context

Keep good sentences, including the full Beeinträchtigungen apology. Select a
coherent sentence or meaningful phrase rather than an unrelated paragraph.
There is no automatic 100/140-character or 15/25-word crop. Never remove a modal
needed by the target, split a verb particle away, or treat printed wrapping and
German ordinal punctuation as sentence boundaries. Context and correct meaning
outrank brevity. Do not substitute a different occurrence after writing its gloss.

For genuinely damaged or ambiguous text, review that occurrence individually.
Do not guess missing source words or turn the defect into a ban on the word.

## Front and back

Front: original context and cloze with its concise English hint.

Back, in order:

1. Original context with the revealed target.
2. Dictionary form and short contextual gloss, immediately readable.
3. Compact useful forms. Noun article/plural occurs once in the lemma; do not
   repeat it in Forms. Verbs show Präteritum and perfect with auxiliary. Omit
   placeholders, regular case tables and redundant grammar commentary.
4. Full English translation of the displayed context, visible by default in
   smaller secondary text. It must not require expanding a disclosure.
5. Small source/date; other captured encounters may be collapsed.

Use left-aligned, phone-readable type, bounded line width and spacing between
blocks. The sentence, answer, translation and metadata should not all compete
at the same visual size. Support light and dark appearance.

## Model examples

- Trittbrett: keep the word; the original scooter screen establishes relevance.
- Niedriger Batteriestand: a useful label, not a failed sentence.
- bitten um: bat, hat gebeten; um is a preposition, not a separable prefix.
- flattern ... ein: extract einflattern, not flattern.
- entgegennehmen ... aufbewahren könntet: keep the governing modal in context.
- Kurier*in: do not leave part of the inflected/inclusive source word unclozed.

## Changes to an existing collection

Back up and sync before editing. Update owned notes in place, preserve note/card
IDs, reviews and scheduling, and do not recreate a note deleted by the learner.
Do not delete vocabulary as part of a layout migration. Revisions to damaged
examples must retain their original capture links and full source archive.

The four versioned stage prompts implement these rules. Regression tests cover
long-context preservation, useful label clozes, schema-preserving template updates,
visible translations and unchanged scheduling through incremental sync.

References: [Anki templates](https://docs.ankiweb.net/templates/intro.html),
[Anki styling](https://docs.ankiweb.net/templates/styling.html),
[Twenty Rules](https://www.super-memory.org/archive/articles/20rules.htm).
The exact typography is a product choice; these sources are not a universal
prescription for one font size or a ban on sentence length.
