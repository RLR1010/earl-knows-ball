# Original-Article Social Card — Template Review Set

Saved 2026-09-04 for review + iterate-one-at-a-time. Rendered 1600×900 via the same
Playwright machinery as the game writeup cards.

## Layout
```
original-card-templates/
  mlb/  single-01-mlb.png · multi-mlb.png   (emerald / field-green scheme)
  nba/  single-01-nba.png · multi-nba.png   (navy + player-orange scheme)
  nfl/  single-01-nfl.png · multi-nfl.png   (graphite + red scheme)
  all/  single-01-all.png                  (violet cross-sport scheme)
```
- **single-01-\*** = the workhorse 1-team "spotlight" layout (most original articles
  are single-team: MLB 42, NBA 46, NFL 24 of published). Team logo on a WHITE circle
  sits under the headline on the LEFT; Earl rendered smaller on the RIGHT.
- **multi-\*** = team-band layout (daily_picks / multi-team recaps). Team logos on
  WHITE circles across a raised band; Earl smaller on the right beside/near the band.
- `all/` currently shows the sport/general editorial look (no single team).

## Recent changes baked in (Rich 2026-09-04)
1. **Earl is smaller** (~400×600 right-side figure, was oversized).
2. **Earl not over-brightened** — used a gentler exposure lift (`earl_photo.png`)
   because the previous boosted version looked washed out.
3. **Team logos raised higher** off the bottom edge.
4. **Team logos on WHITE circles** on every template so they stand out regardless of bg.

## Color schemes (per sport brand)
| family | scheme |
|---|---|
| MLB  | deep emerald + bright mint accent |
| NBA  | navy fields + player-orange accent |
| NFL  | graphite/steel + signal-red accent |
| all  | violet + lavender accent |

## How to review / iterate
Each is a standalone PNG here. Source/template generator: `/tmp/mock/render_set.py`
(dev box) — reach out to rebuild any single card after tweaks. Note: no vision
model was available when these rendered (09-04), so structure was verified
geometrically (Earl size/position, circle band placement, per-sport bg color), not
by eye — please give each a visual pass and we'll tune one at a time.
