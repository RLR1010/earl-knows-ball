-- Card headline accent text for public.original_articles.
--
-- card_accent: an optional short phrase (1-4 words) that is a verbatim
-- contiguous substring of the article's `title`, rendered on the social card /
-- og:image headline in the theme's accent color (e.g. "Thunder Bet on a
-- Top-Heavy Future" -> card_accent = "a Top-Heavy"). The card builder locates
-- it (case-insensitive) and colors only that span; headline stays all-white
-- when NULL or empty so an LLM miss never ships a gimmicky card.
--
-- Populated at generation (the LLM proposes it; server-side validates it's a
-- real title substring and not pure numbers/dates/scores before persisting).
-- Also may be set/overridden by an editor in the admin. Backwards compatible:
-- existing rows default to NULL (no accent) and still render.

ALTER TABLE public.original_articles
    ADD COLUMN IF NOT EXISTS card_accent TEXT;
