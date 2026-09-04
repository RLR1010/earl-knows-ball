-- Add a "preview_image" to public.original_articles.
--
-- preview_image: absolute URL (or site-relative path) to the generated 16:9
-- social card used as the og:image / Twitter summary-large-image when an
-- article link is shared. Draft articles and non-matchup articles with no
-- generated card leave it NULL, in which case the page renders no og:image.
--
-- Backwards compatible: existing rows default to NULL (no card until one is
-- generated).

ALTER TABLE public.original_articles
    ADD COLUMN IF NOT EXISTS preview_image TEXT;
