ALTER TABLE jobs ADD COLUMN explicit_lookup INTEGER NOT NULL DEFAULT 0 CHECK(explicit_lookup IN (0,1));
