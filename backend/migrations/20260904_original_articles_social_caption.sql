-- Editable social caption for public.original_articles.
--
-- social_caption: the short 1-2 sentence shareable hook that pairs with the
-- social card / og image (an X/Twitter-ish post body) when an article is
-- shared to social. Mirrors the social_caption column already stored on
-- mlb/nfl/nba.game_writeups. Drafted deterministically at card-generation
-- time from title/summary/card_accent (may be re-edited by an editor in the
-- admin). Backwards compatible: existing rows default to NULL (frontends treat
-- NULL/empty as "no caption drafted yet").

ALTER TABLE public.original_articles
    ADD COLUMN IF NOT EXISTS social_caption TEXT;
