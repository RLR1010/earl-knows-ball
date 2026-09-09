-- Feature: Game writeup slug aliases (old canonical slug -> current writeup).
--
-- When a game writeup's canonical slug changes (e.g. a manual regeneration with
-- force_new_title that produces a fresh title -> fresh slug), any previously
-- published canonical slug becomes stale and would otherwise 404 / break public
-- links. These tables keep one or more historical aliases per writeup so backend
-- content routes can still resolve the article by an old slug (200) and the
-- frontend can next.redirect() 301 to the live canonical slug.
--
-- Each sport (mlb/nfl/nba) has its OWN table, mirroring how game_writeups live in
-- per-sport schemas. Idempotent (CREATE TABLE IF NOT EXISTS) so it is safe to
-- run more than once against dev.
--
-- game_writeup_id references the canonical row id in <schema>.game_writeups.
-- old_slug is the previously-published canonical slug that must keep working.

CREATE TABLE IF NOT EXISTS mlb.game_writeup_slug_aliases (
    game_writeup_id BIGINT NOT NULL,
    old_slug        TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (game_writeup_id, old_slug)
);

CREATE TABLE IF NOT EXISTS nfl.game_writeup_slug_aliases (
    game_writeup_id BIGINT NOT NULL,
    old_slug        TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (game_writeup_id, old_slug)
);

CREATE TABLE IF NOT EXISTS nba.game_writeup_slug_aliases (
    game_writeup_id BIGINT NOT NULL,
    old_slug        TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (game_writeup_id, old_slug)
);

CREATE INDEX IF NOT EXISTS idx_mlb_game_writeup_slug_aliases_old_slug
    ON mlb.game_writeup_slug_aliases (old_slug);
CREATE INDEX IF NOT EXISTS idx_nfl_game_writeup_slug_aliases_old_slug
    ON nfl.game_writeup_slug_aliases (old_slug);
CREATE INDEX IF NOT EXISTS idx_nba_game_writeup_slug_aliases_old_slug
    ON nba.game_writeup_slug_aliases (old_slug);
