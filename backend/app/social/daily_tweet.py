"""Daily X tweet task — Earl Knows Ball.

Scheduled task (registered as a row in `task_config` so it shows under /admin/tasks
and runs on the COMPUTE box). Each time it fires it sends AT MOST ONE tweet, working
toward a target of 6/day:

  * 3 fresh game-preview writeups, and
  * 3 original articles.

Rules honored here:
  - A game preview is only ever tweeted while its game is STILL SCHEDULED (we never
    tweet a preview after the game has been played / is in progress).
  - Only game previews WRITTEN that same day (Central) are eligible — each day's
    3 writeups come from that morning's fresh previews, not backlog.
  - Every source is stamped `x_posted_at` so nothing is ever tweeted twice.
  - Variety: avoid repeating the same sport repeatedly within a day, and appear
    once per unique team/market in a day when alternatives exist.

A tweet = {social_caption}\\n\\n{public_url}. No attached image: X auto-scrapes the
card from the page <head> og:image.

Usage:
  python -m app.social.daily_tweet --pick            # print the next single tweet it would send (dry-run, NO post)
  python -m app.social.daily_tweet --plan            # print the whole day's 6 (what all slots would be), NO post
  python -m app.social.daily_tweet --send            # actually send the next single tweet (real post)
The task_config entry invokes --send.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import create_engine, text

from app.core.config import settings

logger = logging.getLogger("daily_tweet")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

PUBLIC_BASE = "https://earlknowsball.com"
SPORTS = ["mlb", "nfl", "nba"]
# how many writeup + original tweets we want per day
WRITEUP_TARGET = 3
ORIGINAL_TARGET = 3
MAX_DAY = WRITEUP_TARGET + ORIGINAL_TARGET
MAX_TWEET_LEN = 280
CENTRAL = timezone(timedelta(hours=-5))  # CDT (America/Chicago, no DST change in our window)


def _eng():
    return create_engine(settings.database_url.replace("+asyncpg", "+psycopg2"))


def _now_central() -> datetime:
    # settings/sys are in Central; use local naive->aware carefully
    return datetime.now(CENTRAL)


def _start_of_today_central() -> datetime:
    now = _now_central()
    return datetime.combine(now.date(), time.min, tzinfo=CENTRAL)


# --------------------------------------------------------------------------
# today's already-sent writeups & originals (dedup via x_posted_at col)
# --------------------------------------------------------------------------

_WRITEUP_TABLES = ["mlb.game_writeups", "nfl.game_writeups", "nba.game_writeups"]


def _today_sent() -> tuple[list[int], set[str]]:
    """Return (list of Sport+id already tweeted today, set of team names used today)."""
    eng = _eng()
    day_start = _start_of_today_central()
    used: set[str] = set()
    out_w: set[tuple[str, int]] = set()
    with eng.connect() as c:
        for wt in _WRITEUP_TABLES:
            sch, tab = wt.split(".")
            rows = c.execute(
                text(f"SELECT gw.game_id, gw.id FROM {sch}.{tab} gw "
                     "WHERE gw.x_posted_at IS NOT NULL AND gw.x_posted_at >= :d"),
                {"d": day_start}).fetchall()
            for game_id, wid in rows:
                out_w.add((sch, wid))
        orow = c.execute(
            text("SELECT id, sport FROM public.original_articles "
                 "WHERE x_posted_at IS NOT NULL AND x_posted_at >= :d"),
            {"d": day_start}).fetchall()
    return list(out_w), used


# --------------------------------------------------------------------------
# candidate queries
# --------------------------------------------------------------------------


def _fresh_writeup_candidates() -> list[dict]:
    """Published writeups created today (Central) whose game is STILL SCHEDULED and
    that have a caption + card and have NOT yet been tweeted."""
    eng = _eng()
    day_start = _start_of_today_central()
    out: list[dict] = []
    with eng.connect() as c:
        for sch in SPORTS:
            q = text(
                f"SELECT gw.id, gw.game_id, gw.title, gw.slug, gw.social_caption, "
                f"gw.preview_image, th.abbreviation AS home_abbr, ta.abbreviation AS away_abbr, "
                f"g.date AS game_date "
                f"FROM {sch}.game_writeups gw "
                f"JOIN {sch}.games g ON g.id = gw.game_id "
                f"LEFT JOIN {sch}.teams th ON th.id = g.home_team_id "
                f"LEFT JOIN {sch}.teams ta ON ta.id = g.away_team_id "
                f"WHERE gw.status = 'published' "
                f"AND gw.x_posted_at IS NULL "
                f"AND g.status = 'SCHEDULED' "
                f"AND gw.created_at >= :d "
                f"AND gw.social_caption IS NOT NULL AND length(trim(gw.social_caption)) > 0 "
                f"AND gw.preview_image IS NOT NULL AND length(trim(gw.preview_image)) > 0 "
                f"ORDER BY g.date")
            for r in c.execute(q, {"d": day_start}):
                out.append({
                    "kind": "writeup", "sport": sch,
                    "id": r.id, "game_id": r.game_id, "source_title": r.title,
                    "slug": r.slug, "caption": r.social_caption,
                    "card_image_ref": r.preview_image,
                    "teams": [x for x in (r.home_abbr, r.away_abbr) if x],
                })
    return out


def _original_candidates() -> list[dict]:
    """Published, PUBLIC original articles that have caption + card and are not yet
    tweeted, newest first. RULE: only public content may be tweeted — premium-gated
    articles (visibility='premium', e.g. the paid daily-picks) are NEVER eligible.
    Excludes the 'all'/'general' sport if a sported article is available (variety);
    we'll handle ordering at pick time."""
    eng = _eng()
    with eng.connect() as c:
        q = text(
            "SELECT id, sport, title, slug, social_caption, preview_image, teams "
            "FROM public.original_articles "
            "WHERE status='published' AND visibility='public' AND x_posted_at IS NULL "
            "AND social_caption IS NOT NULL AND length(trim(social_caption)) > 0 "
            "AND preview_image IS NOT NULL AND length(trim(preview_image)) > 0 "
            "ORDER BY coalesce(published_at, updated_at, created_at) DESC")
        out = []
        for r in c.execute(q):
            try:
                teams = json.loads(r.teams) if r.teams else []
            except (TypeError, json.JSONDecodeError):
                teams = []
            if isinstance(teams, list):
                teams = [str(t) for t in teams]
            out.append({
                "kind": "original", "sport": (r.sport if r.sport != "all" else "all"),
                "id": r.id, "source_title": r.title, "slug": r.slug,
                "caption": r.social_caption, "card_image_ref": r.preview_image,
                "teams": teams,
            })
    return out


def _public_url(item: dict) -> str:
    if item["kind"] == "original":
        sport = f"{item['sport']}/"
        return f"{PUBLIC_BASE}/{sport}articles/{item['slug']}"
    return f"{PUBLIC_BASE}/{item['sport']}/articles/previews/{item['slug']}"


X_URL_COUNT = 23  # X/t.co counts any URL in a tweet as 23 chars for the 280 limit


def _smart_caption_trim(caption: str, limit: int) -> str:
    """Trim a caption to <= ``limit`` chars, breaking at a clean sentence/word
    boundary rather than mid-word, adding an ellipsis when truncated."""
    if len(caption) <= limit:
        return caption
    head = caption[:limit]
    # cut at the last sentence end before the limit, else last space
    m = max(head.rfind(s) for s in (". ", "\n", "? ", "! "))
    if m > limit // 2:
        cut = m + 1
    else:
        sp = head.rfind(" ")
        cut = sp if sp > limit // 2 else limit
    return (head[:cut].rstrip(" ,;.") + "…").strip()[:limit]


def _tweet_text(item: dict) -> str:
    caption = (item["caption"] or "").strip()
    url = _public_url(item)
    # X counts the (shortened) URL as a fixed 23 chars + the "\n\n" separator.
    caption_cap = MAX_TWEET_LEN - X_URL_COUNT - 2
    return f"{_smart_caption_trim(caption, caption_cap)}\n\n{url}"


# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------


def _pick(w_sent: int, o_sent: int, writeups: list[dict], originals: list[dict],
          used_teams: set[str], used_sports_today: set[str]) -> dict | None:
    """Choose the single next item to send now toward the 3+3 target with variety."""
    # No more room today
    if w_sent >= WRITEUP_TARGET and o_sent >= ORIGINAL_TARGET:
        return None
    if w_sent + o_sent >= MAX_DAY:
        return None

    def first_avail(pool: list[dict], pref_sport: str | None) -> dict | None:
        # order pool to avoid a sport already used today and teams already used
        def rank(it):
            sp = it["sport"]
            sport_penalty = 1 if sp in used_sports_today else 0
            team_penalty = sum(1 for t in it["teams"] if t in used_teams)
            same_pref = 0 if (pref_sport and sp == pref_sport) else (1 if pref_sport else 0)
            return (same_pref, team_penalty, sport_penalty)
        ordered = sorted(pool, key=rank)
        return ordered[0] if ordered else None

    # Prefer a class that still has room and has a candidate.
    w_ok = w_sent < WRITEUP_TARGET and bool(writeups)
    o_ok = o_sent < ORIGINAL_TARGET and bool(originals)

    # if one class is out of target room but the other still has room, use the other
    if w_ok and o_ok:
        # even out toward target: send the less-filled class first (ties -> writeup)
        return first_avail(writeups, None) if w_sent <= o_sent else first_avail(originals, None)
    if w_ok:
        return first_avail(writeups, None)
    if o_ok:
        return first_avail(originals, None)
    # both target classes are done; fall back to any remaining (rare) up to MAX_DAY
    if writeups or originals:
        return first_avail(writeups + originals, None)
    return None


# --------------------------------------------------------------------------
# send
# --------------------------------------------------------------------------


def _ensure_fresh_oauth2() -> tuple[Optional[str], bool]:
    """Return (access_token, stale) for posting, ALWAYS refreshing through the refresh path
    so the stored OAuth2 token stays current (mirrors social_x/x_oauth.get_live_token but sync,
    because this task is a plain subprocess). The X admin flow stores the user-context token in
    public.x_account; the offline.access refresh token renews it without re-consent."""
    from app.social import x_oauth as _OA
    cid, csec = settings.x_client_id, settings.x_client_secret
    if not cid or not csec:
        logger.error("X OAuth2 client creds missing from settings; cannot refresh token")
        return None, True
    eng = _eng()
    row = None
    with eng.connect() as c:
        row = c.execute(text(
            "SELECT oauth2_access_token AS at, oauth2_refresh_token AS rt,"
            " oauth2_expires_at AS ex, (oauth2_refresh_token IS NOT NULL) AS has_refresh"
            " FROM public.x_account WHERE platform='x'"
        )).mappings().first()
    if not row or not row["at"]:
        logger.error("No X OAuth2 token stored — run X admin 'Connect to X' once to authorize")
        return None, True
    access = row["at"]
    now = _now_utc()
    exp = row["ex"]
    slack = timedelta(seconds=getattr(_OA, "EXPIRY_SLACK", 60))
    needs = (exp is None) or (exp <= now) or (exp <= now + slack)
    if not needs:
        return access, False
    if not row["has_refresh"] or not row["rt"]:
        logger.warning("X access token expired and no refresh token stored — needs re-auth")
        return access, True
    try:
        fresh = _OA.refresh_access_token(cid, csec, row["rt"])  # sync
    except Exception as exc:  # noqa: BLE001
        logger.error("X OAuth2 refresh failed (capping stored token as stale): %s", exc)
        return access, True
    access = fresh.get("access_token") or access
    _persist_oauth2(eng, fresh)
    return access, False


def _persist_oauth2(eng, fresh: dict) -> None:
    """Sync mirror of x_oauth.save_tokens upsert into public.x_account (canonical row)."""
    from app.social import x_oauth as _OA
    scope = fresh.get("scope") or getattr(_OA, "SCOPE_STR",
        "tweet.read tweet.write users.read follows.read offline.access")
    exp = None
    # X returns expires_in (seconds) on refresh; use it to stamp a fresh expiry.
    # (Some responses also carry expires_at as an int epoch — ignore to stay simple/robust.)
    if fresh.get("expires_in"):
        try:
            exp = datetime.now(timezone.utc) + timedelta(
                seconds=int(fresh["expires_in"]))
        except (TypeError, ValueError):
            exp = None
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO public.x_account (platform, oauth2_access_token, oauth2_refresh_token,"
            "  oauth2_token_type, oauth2_scope, oauth2_expires_at, oauth2_connected_at)"
            " VALUES ('x', :at, :rt, 'bearer', :sc, :ex, now())"
            " ON CONFLICT (platform) DO UPDATE SET"
            "  oauth2_access_token=EXCLUDED.oauth2_access_token,"
            "  oauth2_refresh_token=EXCLUDED.oauth2_refresh_token,"
            "  oauth2_token_type='bearer',"
            "  oauth2_scope=EXCLUDED.oauth2_scope,"
            "  oauth2_expires_at=EXCLUDED.oauth2_expires_at,"
            "  oauth2_connected_at=now()"),
            {"at": fresh.get("access_token"), "rt": fresh.get("refresh_token"),
             "sc": scope, "ex": exp})


def _now_utc():
    return datetime.now(timezone.utc)


def _send_one(item: dict, dry_run: bool) -> bool:
    """Send one tweet using the established X posting path. Returns True if posted."""
    from app.social import x_client as X

    body = _tweet_text(item)
    # X counts the shortened URL as X_URL_COUNT chars, so validate on the X-counted
    # length (raw body length can exceed 280 purely because the full URL is long).
    caption, _, _url = body.partition("\n\n")
    x_length = len(caption) + 2 + X_URL_COUNT
    if x_length > MAX_TWEET_LEN:
        logger.warning("tweet X-counted too long (%s caption=%s) for %s/%s",
                       x_length, len(caption), item["kind"], item["id"])
        return False
    logger.info("SENDING %s/%s (x_len=%s caps=%s): %r", item["kind"], item["id"],
                x_length, len(caption), body)
    if dry_run:
        return True  # just logged
    at, stale = _ensure_fresh_oauth2()
    if not at or stale:
        logger.error("cannot POST %s/%s: no current X OAuth2 token (stale=%s) — refresh failed",
                     item["kind"], item["id"], stale)
        return False
    res = X.create_post(body, oauth2_client_id=settings.x_client_id,
                        oauth2_client_secret=settings.x_client_secret,
                        oauth2_access_token=at)
    if not res or not res.get("ok"):
        logger.error("X create_post failed for %s/%s: %s", item["kind"], item["id"], res)
        return False
    eng = _eng()
    with eng.begin() as c:
        cid = c.execute(
            text("""
                INSERT INTO public.x_post_candidates
                  (content_type, sport, source_ref, draft_text, card_image_ref, status,
                   schedule_for, posted_at, created_at, updated_at)
                VALUES (:ct, :sport, :src, :txt, :card, 'sent', NOW(), NOW(), NOW(), NOW())
                RETURNING id"""),
            {"ct": f"{item['kind']}_{item['sport']}", "sport": item["sport"],
             "src": str(item["id"]), "txt": body, "card": item["card_image_ref"]}).scalar()
        c.execute(
            text("INSERT INTO public.x_sent_posts (candidate_id, x_tweet_id, text, link_url) "
                 "VALUES (:cid, :tid, :txt, :link)"),
            {"cid": cid, "tid": str(res.get("tweet_id", "")), "txt": body,
             "link": _public_url(item)})
        # Stamp the SOURCE row so this item is never re-picked (same tx as the send record).
        src_tab = "public.original_articles" if item["kind"] == "original" \
            else f"{item['sport']}.game_writeups"
        c.execute(text(f"UPDATE {src_tab} SET x_posted_at = NOW() WHERE id = :id"),
                  {"id": item["id"]})
    logger.info("POSTED %s/%s ok tweet_id=%s", item["kind"], item["id"], res.get("tweet_id"))
    return True


# --------------------------------------------------------------------------


def _state() -> tuple[int, int, set[str], set[str], list[dict], list[dict]]:
    writeups = [w for w in _fresh_writeup_candidates()]
    originals = [o for o in _original_candidates()]

    # today's sent counts by kind + sports/teams used
    eng = _eng()
    day_start = _start_of_today_central()
    w_sent = 0
    o_sent = 0
    used_sports: set[str] = set()
    used_teams: set[str] = set()

    with eng.connect() as c:
        for sch in SPORTS:
            rows = c.execute(
                text(f"SELECT th.abbreviation AS h, ta.abbreviation AS a "
                     f"FROM {sch}.game_writeups gw "
                     f"JOIN {sch}.games g ON g.id=gw.game_id "
                     f"LEFT JOIN {sch}.teams th ON th.id=g.home_team_id "
                     f"LEFT JOIN {sch}.teams ta ON ta.id=g.away_team_id "
                     "WHERE gw.x_posted_at IS NOT NULL AND gw.x_posted_at >= :d"),
                {"d": day_start}).fetchall()
            w_sent += len(rows)
            for r in rows:
                used_teams.update(x for x in (r.h, r.a) if x)
                used_sports.add(sch)
        orows = c.execute(text("SELECT sport, teams FROM public.original_articles WHERE x_posted_at IS NOT NULL AND x_posted_at >= :d"), {"d": day_start}).fetchall()
        for r in orows:
            o_sent += 1
            used_sports.add(r.sport)
            if r.teams:
                try:
                    for t in json.loads(r.teams):
                        used_teams.add(str(t))
                except Exception:
                    pass
    return w_sent, o_sent, used_sports, used_teams, writeups, originals


def send_next(dry_run: bool = False) -> dict:
    w_sent, o_sent, u_sports, u_teams, writeups, originals = _state()
    item = _pick(w_sent, o_sent, writeups, originals, u_teams, u_sports)
    if item is None:
        logger.info("daily_tweet: nothing to send (w_sent=%s o_sent=%s, writeups=%s originals=%s)",
                    w_sent, o_sent, len(writeups), len(originals))
        return {"sent": False, "reason": "nothing-eligible-or-day-complete"}
    ok = _send_one(item, dry_run)
    return {"sent": ok, "kind": item["kind"], "sport": item["sport"], "id": item["id"],
            "text": _tweet_text(item)}


def plan_day() -> dict:
    """Show what the full day would send (6 slots), without posting."""
    w_sent, o_sent, u_sports, u_teams, writeups, originals = _state()
    plan = []
    for _ in range(MAX_DAY - (w_sent + o_sent)):
        item = _pick(w_sent, o_sent, writeups, originals, u_teams, u_sports)
        if item is None:
            break
        plan.append({
            "kind": item["kind"], "sport": item["sport"], "id": item["id"],
            "title": item["source_title"], "url": _public_url(item),
            "text": _tweet_text(item),
        })
        if item["kind"] == "writeup":
            w_sent += 1
            writeups = [x for x in writeups if x["id"] != item["id"]]
            u_sports.add(item["sport"])
            u_teams.update(item["teams"])
        else:
            o_sent += 1
            originals = [x for x in originals if x["id"] != item["id"]]
            u_sports.add(item["sport"])
            u_teams.update(item["teams"])
    return {"today_counts": {"writeups_sent": w_sent, "originals_sent": o_sent},
            "pool": {"writeups_avail": len(writeups), "originals_avail": len(originals)},
            "plan": plan}


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--pick", action="store_true", help="dry-run: print next single tweet only")
    g.add_argument("--plan", action="store_true", help="dry-run: print the whole day's plan")
    g.add_argument("--send", action="store_true", help="actually send the next single tweet")
    a = ap.parse_args()
    if a.plan:
        res = plan_day()
        print(json.dumps(res, indent=2, default=str))
    elif a.send:
        res = send_next(dry_run=False)
        print(json.dumps(res, indent=2, default=str))
    else:
        res = send_next(dry_run=True)
        print(json.dumps(res, indent=2, default=str))


if __name__ == "__main__":
    main()
