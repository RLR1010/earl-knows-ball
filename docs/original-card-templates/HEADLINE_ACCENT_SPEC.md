# Card headline accent-text — design spec

Status: draft for review (2026-09-04). Owner earl-knows-ball.
Related: docs/original-card-templates (portrait-driven card concepts).

## Goal
Make the *article headline* on a social/original-article card optionally render one
short phrase in the card's accent color (e.g. "Thunder Bet on **a Top-Heavy** Future",
"Trusting the Model's **Underdogs**"), decided by the LLM at generation time and
preserved through storage so the card builder just colors it.

## Grounding (what exists today)
- `original_articles` (public) — single cross-sport table:
  `id, sport CHECK IN ('mlb','nfl','nba'), title, summary, content(markdown),
  content_html, instructions, status, reason, word_*, published_at, created/updated`.
  Later metadata adds (via migrations): `slug`, `seo_description`, `teams` (JSONB
  abbrs), `preview_image`, `rejection_history`, `usage`, `section IN
  ('article','daily_picks')`. NOTE schema `sport` has NO 'all' value; the cross-sport
  'all' content on the product is not a native `original_articles.sport` row.
- Writing: `app/routers/original_articles.py` `_write_original_article()` returns a
  whole markdown article beginning with a `# Heading` line. Title is the lead `#`,
  content is the rest. No structured headline/accent concept exists today.
- Card: `app/social/cards.py` `run(sport, ref)` builds game + original-article cards.
  For original articles it **requires >=2 teams** (`teams` JSON) and renders a fixed
  2-cell matchup template. Title text comes from `original_articles.title`; dek comes
  from `summary` (or first para). It writes the card file into
  `original_articles.preview_image`. Our portrait-driven templates live in
  `docs/original-card-templates/portraits/` (concepts), source generators dev-box
  `/tmp/mock/card_portraits.py`.

## Ground rule (the "mark" question)
Yes — the LLM marks the word, and we store the mark, not something the template
guesses at render time.

## Design
1. **Generation** — the article write prompt (returned JSON, alongside title/summary/
   sections) gains one optional field:
   - `accent` : a short phrase (1-3 words) that MUST be a verbatim contiguous
     substring of the final `title`. Empty/null allowed.
   Prompt rule: pick the single most evocative non-factual phrase — prefer
   adjectives/hooks ("a Top-Heavy", "Underdogs"); DO NOT accent numbers, scores,
   dates, or bare team/league names. If there's no clean hook, return empty.

2. **Validation guardrail** (server-side, so a miss never ships a gimmicky card):
   - accepted only if `accent` is a non-empty contiguous substring of `title`;
   - rejected if it is all digits, or a pure number/date/%(score), or is a
     stop-type token (<3 chars); on rejection treat as empty.
   - `accent IS NULL/''` => headline renders all-white (no forced emphasis).

3. **Storage** — add a column (migration) on `original_articles`:
   `card_accent TEXT` (nullable). Written when the article row is saved/updated.
   No need to store the marked title itself; store just the substring and let the
   card builder locate it (case-insensitive) once.

4. **Card building** — the portrait/single-team template splits the title at the
   first case-insensitive occurrence of `card_accent` and emits that substring
   wrapped in the accent-colored `<em>` span. If accent empty or not found, emit
   the plain white headline. (Doc/HTML escaping handled; substring is trusted text,
   escape on insert.)

5. **Preview/footprint** — the accent is carried on the same row the admin preview
   (original-articles, auto-generation) already reloads, so both preview sources show
   it once we swap the card layout + card_accent wiring in together.

## Open questions for Rich
- Do we add `card_accent` at generation only, or also expose a small editor override
  in the admin (admin types the stressed phrase) so non-LLM articles / manual tweaks
  are easy? (Recommended: yes over time; gen-first is fine to start.)
- The current legacy card path requires >=2 teams and uses a matchup template. The
  portrait-driven single-team templates assume 1-2 teams and drop accent naturally.
  Should accent land first in the new portrait card system (recommended) vs also
  back-porting to the old matchup card? (Recommended: new system only.)

## Acceptance
- Migration adds `card_accent`; write path persists it; card split/render hides the
  accent when empty/invalid; headline never renders colored text on invalid accent.
