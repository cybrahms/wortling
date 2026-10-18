ALTER TABLE cards ADD COLUMN created_at TEXT NOT NULL DEFAULT '';
-- Historical creation times were not recorded. Prefer the first encounter,
-- then the first Anki write, then the last known card update.
UPDATE cards SET created_at=COALESCE(
    (SELECT MIN(c.received_at) FROM encounters e
     JOIN captures c ON c.id=e.capture_id WHERE e.card_id=cards.id),
    added_at, updated_at
);
CREATE TRIGGER cards_created_at
AFTER INSERT ON cards WHEN NEW.created_at=''
BEGIN
    UPDATE cards SET created_at=NEW.updated_at WHERE id=NEW.id;
END;
