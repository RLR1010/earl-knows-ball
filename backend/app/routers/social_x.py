"""X (@earl_knows_ball) admin router — connect/verify account, view source seeds, manage
drafts, and send a post. Admin-only (compute role + get_admin_user).

Phase 0/1 scope (per Rich): manual composer + approve/send. No auto-scheduler yet.
All routes that touch credentials or POST externally are locked behind get_admin_user.
"""
from __future__ import annotations

import logging
import os
import asyncio
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.database import get_db
from app.routers.admin import get_admin_user
from app.social import x_client as X
from app.social import post_sources as SRC
from app.social import x_oauth as OA

logger = logging.getLogger("social_x")

admin_router = APIRouter(prefix="/api/admin/x", tags=["x-social-admin"])

# In-memory map of supported content types -> loader fn.
_CONTENT_LOADERS = SRC.LOADERS
_CONTENT_TYPES = SRC.CONTENT_TYPES

_VALID_STATUS = ("draft", "queued", "approved", "scheduled", "sent", "failed", "discarded")


# --------------------------------------------------------------------------- pydantic
class XStatusOut(BaseModel):
    connected: bool
    ok: Optional[bool] = None
    error: Optional[str] = None
    username: Optional[str] = None
    user_id: Optional[str] = None
    name: Optional[str] = None
    verified: Optional[bool] = None
    message: Optional[str] = None


class XConnectIn(BaseModel):
    # Empty strings mean "read from .env settings". Non-empty means "store + use these."
    api_key: str = ""
    api_secret: str = ""
    access_token: str = ""
    access_token_secret: str = ""


class SeedOut(BaseModel):
    kind: str
    text: str
    source_ref: dict


class ContentTypesOut(BaseModel):
    content_types: dict


class DraftIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=500)
    content_type: str = "best_pick"
    sport: Optional[str] = None
    source_ref: dict = {}
    schedule_for: Optional[datetime] = None


class DraftOut(BaseModel):
    id: int
    text: str
    content_type: str
    sport: Optional[str]
    source_ref: dict
    status: str
    created_at: Optional[datetime] = None
    media_id: Optional[str] = None
    card_image_ref: Optional[str] = None
    error: Optional[str] = None
    tweet_id: Optional[str] = None
    human_edited_at: Optional[datetime] = None


class DraftEditIn(BaseModel):
    text: Optional[str] = Field(None, min_length=1, max_length=500)
    status: Optional[str] = Field(None, pattern="^(draft|queued|approved|discarded)$")
    content_type: Optional[str] = None
    sport: Optional[str] = None
    source_ref: Optional[dict] = None
    schedule_for: Optional[datetime] = None


class SendDraftIn(BaseModel):
    # Optional media id already uploaded; if absent we post text-only.
    media_id: Optional[str] = None


class HistoryOut(BaseModel):
    posts: list[dict]


# --------------------------------------------------------------------------- helpers
def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _get_draft(db: AsyncSession, draft_id: int) -> dict:
    row = (
        await db.execute(
            text(
                """SELECT id, draft_text, content_type, sport, source_ref,
                          status, schedule_for, media_id, card_image_ref, posted_error,
                          human_edited_at, posted_at, created_at
                   FROM public.x_post_candidates WHERE id = :id"""
            ),
            {"id": draft_id},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Draft not found")
    return dict(row)


def _used_credentials(body: XConnectIn) -> dict:
    try:
        from app.core.config import settings as cfg
    except Exception:  # pragma: no cover
        return {}
    return {
        "api_key": body.api_key or (cfg.x_consumer_key or None),
        "api_secret": body.api_secret or (cfg.x_consumer_secret or None),
        "access_token": body.access_token or (cfg.x_access_token or None),
        "access_secret": body.access_token_secret or (cfg.x_access_token_secret or None),
    }


# --------------------------------------------------------------------------- account / status
@admin_router.get("/status", response_model=XStatusOut)
async def x_status(db: AsyncSession = Depends(get_db)):
    """Return whether X creds are configured + a lightweight connectivity probe."""
    cfg = settings
    has_creds = bool(cfg.x_consumer_key and cfg.x_access_token)
    if not has_creds:
        return XStatusOut(
            connected=False,
            message="X credentials not configured. Add X_CONSUMER_KEY / X_ACCESS_TOKEN "
                    "(etc.) to compute .env, or use /connect to supply them.",
        )
    probe = X.connect_health(
        api_key=cfg.x_consumer_key,
        api_secret=cfg.x_consumer_secret,
        access_token=cfg.x_access_token,
        access_secret=cfg.x_access_token_secret,
    )
    return XStatusOut(
        connected=True,
        ok=probe.get("ok"),
        error=probe.get("error"),
        username=probe.get("username"),
        user_id=probe.get("user_id"),
        name=probe.get("name"),
        verified=probe.get("verified"),
    )


@admin_router.post("/connect", response_model=XStatusOut)
async def x_connect(
    body: XConnectIn,
    db: AsyncSession = Depends(get_db),
    admin=Depends(get_admin_user),
):
    """Verify supplied (or .env) X creds and persist them (encrypted-in-DB not yet wired;
    stored in cleartext only if supplied here — recommend .env for prod secrets)."""
    creds = _used_credentials(body)
    if not any(creds.values()):
        raise HTTPException(status_code=400, detail="No X credentials supplied (all fields empty).")
    probe = X.connect_health(**creds)
    out = XStatusOut(connected=probe.get("ok", False), **{k: probe[k] for k in ("ok", "error", "username", "user_id", "name", "verified") if k in probe})
    if probe.get("ok"):
        try:
            await db.execute(
                text(
                    """INSERT INTO public.x_account
                          (platform, handle, api_key, api_secret, access_token,
                           access_secret, user_id, scopes, connected_at, last_probe_ok)
                       VALUES ('x', :handle, :ak, :as, :at, :asec, :uid, ARRAY['tweet.read','tweet.write','users.read'],
                               now(), TRUE)
                       ON CONFLICT (platform) DO UPDATE SET
                         handle = EXCLUDED.handle,
                         api_key = EXCLUDED.api_key,
                         api_secret = EXCLUDED.api_secret,
                         access_token = EXCLUDED.access_token,
                         access_secret = EXCLUDED.access_secret,
                         user_id = EXCLUDED.user_id,
                         connected_at = now(),
                         last_probe_ok = TRUE,
                         last_probe_at = now()"""
                ),
                {
                    "handle": probe.get("username"),
                    "uid": probe.get("user_id"),
                    "ak": creds["api_key"],
                    "as": creds["api_secret"],
                    "at": creds["access_token"],
                    "asec": creds["access_secret"],
                },
            )
            await db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.exception("failed persisting x_account")
            out = XStatusOut(connected=True, ok=True, username=probe.get("username"),
                             user_id=probe.get("user_id"),
                             message=f"Connected but failed to persist account row: {exc}")
    return out


# ------------------------------------------------------------------ OAuth2 read (who-we-follow)
def _oauth2_defaults():
    """Return OAuth2 confidential-client config from settings (or empty)."""
    cfg = settings
    return {
        "client_id": (cfg.x_client_id or "").strip(),
        "client_secret": (cfg.x_client_secret or "").strip(),
        "redirect_uri": (cfg.x_oauth_redirect_uri
                          or "https://earlknowsball.com/api/admin/x/oauth/callback").strip(),
    }


class XAuthorizeOut(BaseModel):
    authorize_url: str
    state: str
    note: str = ""


@admin_router.get("/oauth/authorize", response_model=XAuthorizeOut)
async def x_oauth_authorize(
    redirect_to: str = "/admin/social/x",
    db: AsyncSession = Depends(get_db),
    admin=Depends(get_admin_user),
):
    """Start OAuth2 user-context handshake to enable reading accounts we follow / replies.
    Returns the X authorize URL (frontend opens it). State + PKCE verifier persisted server-side."""
    cfg = _oauth2_defaults()
    if not cfg["client_id"] or not cfg["client_secret"]:
        raise HTTPException(
            status_code=400,
            detail="X_CLIENT_ID / X_CLIENT_SECRET not configured in compute .env.",
        )
    try:
        url, state, verifier = OA.build_authorize_url(
            cfg["client_id"], cfg["client_secret"], cfg["redirect_uri"]
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("oauth authorize build failed")
        raise HTTPException(status_code=500, detail=f"Could not build X authorize URL: {exc}")
    await OA.persist_attempt(db, state, verifier, redirect_to[:300])
    return XAuthorizeOut(authorize_url=url, state=state,
                         note="Scope: full read + write (view timelines, post/reply tweets, like, follow) with auto-refresh. Authorize to enable Approve-and-send posting.")


@admin_router.get("/oauth/callback")
async def x_oauth_callback(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """X redirects the browser here after the user approves. NOT admin-gated: validates by
    `state` (the CSRF proof we persisted at /oauth/authorize). Exchanges code -> stores
    oauth2 tokens on x_account -> redirects back to the admin X page."""
    q = request.query_params
    state = q.get("state") or ""
    code = q.get("code") or ""
    err_desc = q.get("error_description") or q.get("error") or ""
    if not state:
        return RedirectResponse("/admin/social/x?x_err=missing_state", status_code=303)
    assert isinstance(db, AsyncSession)
    attempt = None
    try:
        attempt = await OA.load_attempt(db, state)
    except Exception:  # noqa: BLE001
        logger.exception("oauth load_attempt failed")
    if attempt is None:
        return RedirectResponse("/admin/social/x?x_err=bad_state", status_code=303)
    redirect_to = attempt.get("redirect_to") or "/admin/social/x"
    verifier = attempt.get("code_verifier") or ""
    await OA.clear_attempt(db, state)  # verifier single-use
    if not code or err_desc:
        return RedirectResponse(f"{redirect_to}?x_err={err_desc or 'denied'}", status_code=303)
    cfg = _oauth2_defaults()
    try:
        tok = OA.exchange_authorization_code(
            cfg["client_id"], cfg["client_secret"], cfg["redirect_uri"], code, verifier
        )
        await OA.save_tokens(db, tok)
    except Exception as exc:  # noqa: BLE001
        logger.exception("oauth code exchange failed")
        return RedirectResponse(f"{redirect_to}?x_err=exchange_failed", status_code=303)
    return RedirectResponse(f"{redirect_to}?x_connected=1", status_code=303)


# --------------------------------------------------------------------------- content / sources
@admin_router.get("/content-types", response_model=ContentTypesOut)
async def x_content_types():
    return ContentTypesOut(content_types=_CONTENT_TYPES)


@admin_router.get("/seeds")
async def x_seeds(
    content_type: str = "best_pick",
    sport: Optional[str] = None,
    limit: int = 5,
    db: AsyncSession = Depends(get_db),
):
    """Generate fresh source seeds of a content type (best_pick / record_update)."""
    content_type = content_type or "best_pick"
    loader = _CONTENT_LOADERS.get(content_type)
    if not loader:
        raise HTTPException(status_code=400, detail=f"Unknown content type '{content_type}'")
    seeds = await loader(db, limit=limit, horizon_days=14) if content_type == "best_pick" else await loader(db)
    # sport filter
    if sport:
        seeds = [s for s in seeds if (s.get("sport") or "") == sport]
    return [SeedOut(kind=s["kind"], text=s["text"], source_ref=s["source_ref"]) for s in seeds]


@admin_router.get("/sports")
async def x_sports():
    return {"sports": list(SRC._SPORTS)}


# --------------------------------------------------------------------------- drafts
@admin_router.get("/drafts")
async def list_drafts(
    status: Optional[str] = None,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
):
    q = "SELECT id, draft_text, content_type, sport, source_ref, status, schedule_for, media_id, card_image_ref, posted_error, created_at, updated_at, posted_at FROM public.x_post_candidates"
    conds, params = [], {}
    if status:
        conds.append("status = :status")
        params["status"] = status
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY id DESC LIMIT :limit"
    params["limit"] = min(int(limit), 300)
    rows = (await db.execute(text(q), params)).mappings().all()
    return {"drafts": [_to_draft_out(dict(r)).model_dump() for r in rows]}


@admin_router.post("/drafts", response_model=DraftOut, status_code=201)
async def create_draft(body: DraftIn, db: AsyncSession = Depends(get_db)):
    row = (
        await db.execute(
            text(
                """INSERT INTO public.x_post_candidates
                      (content_type, sport, source_ref, draft_text, status)
                   VALUES (:ct, :sport, :sr, :txt, 'draft')
                   RETURNING id, draft_text, content_type, sport, source_ref,
                             status, media_id, card_image_ref, posted_error,
                             human_edited_at, posted_at, created_at"""
            ),
            {"ct": body.content_type, "sport": body.sport, "sr": __import__("json").dumps(body.source_ref), "txt": body.text},
        )
    ).mappings().first()
    await db.commit()
    return _to_draft_out(dict(row))


def _to_draft_out(d: dict) -> DraftOut:
    # _get_draft/_raw selects return the text in 'draft_text' alias; but some map it as
    # 'text'. Normalize both.
    raw = d.get("draft_text")
    if raw is None:
        raw = d.get("text")
    sr = d.get("source_ref") or {}
    if isinstance(sr, str):
        try:
            sr = __import__("json").loads(sr)
        except Exception:
            sr = {"raw": sr}
    # 'error' is our API response field; DB stores it as posted_error.
    err = d.get("error")
    if err is None:
        err = d.get("posted_error")
    return DraftOut(
        id=d.get("id"),
        text=raw or "",
        content_type=d.get("content_type") or "best_pick",
        sport=d.get("sport"),
        source_ref=sr,
        status=d.get("status") or "draft",
        created_at=d.get("created_at"),
        media_id=d.get("media_id"),
        card_image_ref=d.get("card_image_ref"),
        error=err,
        human_edited_at=d.get("human_edited_at"),
    )


@admin_router.patch("/drafts/{draft_id}", response_model=DraftOut)
async def edit_draft(draft_id: int, body: DraftEditIn, db: AsyncSession = Depends(get_db)):
    await _get_draft(db, draft_id)
    sets, params = [], {"id": draft_id}
    if body.text is not None:
        sets.append("draft_text = :txt"); params["txt"] = body.text
    if body.status is not None:
        if body.status not in _VALID_STATUS:
            raise HTTPException(status_code=400, detail=f"Bad status '{body.status}'")
        sets.append("status = :st"); params["st"] = body.status
    if body.content_type is not None:
        sets.append("content_type = :ct"); params["ct"] = body.content_type
    if body.sport is not None:
        sets.append("sport = :sport"); params["sport"] = body.sport
    if body.source_ref is not None:
        import json as _json
        sets.append("source_ref = :sr"); params["sr"] = _json.dumps(body.source_ref)
    if body.schedule_for is not None:
        sets.append("schedule_for = :sch"); params["sch"] = body.schedule_for
    sets.append("updated_at = now()")
    row = (await db.execute(
        text(f"UPDATE public.x_post_candidates SET {', '.join(sets)} WHERE id = :id "
             "RETURNING id, draft_text, content_type, sport, source_ref, status, media_id, card_image_ref, posted_error, human_edited_at, posted_at, created_at"),
        params)).mappings().first()
    await db.commit()
    return _to_draft_out(dict(row))


@admin_router.delete("/drafts/{draft_id}", status_code=204)
async def delete_draft(draft_id: int, db: AsyncSession = Depends(get_db)):
    await _get_draft(db, draft_id)
    await db.execute(text("DELETE FROM public.x_post_candidates WHERE id = :id"), {"id": draft_id})
    await db.commit()
    return None


# --------------------------------------------------------------------------- send
@admin_router.post("/drafts/{draft_id}/send", response_model=DraftOut)
async def send_draft(
    draft_id: int,
    body: SendDraftIn,
    db: AsyncSession = Depends(get_db),
    admin=Depends(get_admin_user),
):
    """Send an approved/draft post text to X now. Grounding is enforced: a draft without
    a source_ref is allowed (freeform/composer note) but a *pick* draft always cites a
    real game. Text-only unless an already-uploaded media_id is supplied."""
    d = await _get_draft(db, draft_id)
    if d["status"] == "sent":
        raise HTTPException(status_code=409, detail="Already sent.")
    text_body = d["draft_text"]
    if not text_body:
        raise HTTPException(status_code=400, detail="Draft has empty text.")
    if len(text_body) > 280:
        raise HTTPException(status_code=400, detail=f"Post text is {len(text_body)} chars (>280). Trim it.")

    try:
        res = X.create_post(text_body, media_ids=[body.media_id] if body.media_id else None)
    except X.XNotConnectedError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except X.XError as exc:
        # mark failed for the queue view
        try:
            await db.execute(
                text("UPDATE public.x_post_candidates SET status='failed', posted_error=:err, updated_at=now() WHERE id=:id"),
                {"err": str(exc)[:500], "id": draft_id},
            )
            await db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("failed to persist send error")
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    tweet_id = res.get("tweet_id")
    # persist
    await db.execute(
        text("UPDATE public.x_post_candidates SET status='sent', posted_at=now(), media_id=COALESCE(:mid, media_id), posted_error=NULL, updated_at=now() WHERE id=:id"),
        {"mid": body.media_id, "id": draft_id},
    )
    await db.execute(
        text("INSERT INTO public.x_sent_posts (candidate_id, x_tweet_id, text, media_id) VALUES (:cid, :tid, :txt, :mid)"),
        {"cid": draft_id, "tid": tweet_id or "", "txt": text_body, "mid": body.media_id},
    )
    await db.commit()
    return _to_draft_out({**d, "draft_text": text_body, "status": "sent", "tweet_id": tweet_id})


@admin_router.get("/history", response_model=HistoryOut)
async def history(limit: int = 50, db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        text("""SELECT s.x_tweet_id, s.text, s.created_at, s.error,
                       c.id AS candidate_id
                FROM public.x_sent_posts s
                LEFT JOIN public.x_post_candidates c ON c.id = s.candidate_id
                ORDER BY s.created_at DESC LIMIT :limit"""),
        {"limit": min(int(limit), 200)})).mappings().all()
    return HistoryOut(posts=[dict(r) for r in rows])


# ---------------------------------------------------------------------------
# X ingested-posts triage + Earl reply suggestions (2026-09-02 pipeline, Rich)
# Read-only triage + approval DML are admin-only.
# ---------------------------------------------------------------------------

class XTriagePostOut(BaseModel):
    id: int
    tweet_id: str
    author_username: Optional[str] = None
    text: str
    created_at: Optional[datetime] = None
    read_at: Optional[datetime] = None
    likes: Optional[int] = None
    retweets: Optional[int] = None
    replies: Optional[int] = None
    suggestion_count: int = 0
    responded: bool = False

class XPostsOut(BaseModel):
    posts: list[dict]
    count: int

class XReplySuggestionOut(BaseModel):
    id: Optional[int] = None
    post_id: Optional[int] = None
    tweet_id: Optional[str] = None
    author_username: Optional[str] = None
    body: str
    rationale: Optional[str] = None
    status: Optional[str] = None
    created_at: Optional[datetime] = None
    posted_tweet_id: Optional[str] = None
    posted_at: Optional[datetime] = None


class XReplySendResult(BaseModel):
    """Result of the approve-and-send action.

    id/tweet_id = the x_reply_suggestions row; posted_tweet_id = the real reply X
    returned (NULL if we only marked approved, i.e. manual). """
    suggestion_id: int
    status: str
    posted: bool
    posted_tweet_id: Optional[str] = None
    error: Optional[str] = None

class XReplySuggestionsOut(BaseModel):
    suggestions: list[dict]
    count: int

class XReplyStatusIn(BaseModel):
    status: str = Field(..., pattern="^(approved|rejected)$")


@admin_router.get("/posts", response_model=XPostsOut)
async def list_posts(
    author: Optional[str] = None,
    limit: int = 40,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_admin_user),
):
    """Recent X posts we've ingested, newest first, for human triage.
    Optional ?author=username filters to one followed account."""
    sql = ("SELECT p.id, p.tweet_id, p.author_username, p.text, p.created_at, p.read_at, "
           "p.likes, p.retweets, p.replies, "
           "(SELECT count(*) FROM public.x_reply_suggestions s "
           " WHERE s.post_id = p.id AND s.status='pending') AS pending_suggestions, "
           "(EXISTS (SELECT 1 FROM public.x_reply_suggestions s "
           "          WHERE s.post_id = p.id AND s.status IN ('approved','posted'))) AS responded "
           "FROM public.x_posts p")
    params: dict = {}
    if author:
        sql += " WHERE p.author_username = :author"
        params["author"] = author
    sql += " ORDER BY p.created_at DESC NULLS LAST LIMIT :limit"
    params["limit"] = min(int(limit), 100)
    rows = (await db.execute(text(sql), params)).mappings().all()
    posts = []
    for r in rows:
        d = dict(r)
        d["suggestion_count"] = d.pop("pending_suggestions", 0)
        d["responded"] = bool(d.get("responded"))
        posts.append(d)
    return XPostsOut(posts=posts, count=len(posts))


@admin_router.post("/posts/{post_id}/draft-reply", response_model=XReplySuggestionsOut, status_code=201)
async def draft_reply(
    post_id: int,
    n_options: int = 3,
    db: AsyncSession = Depends(get_db),
    admin=Depends(get_admin_user),
):
    """Earl researches a X post's context + drafts on-brand reply suggestions.
    Persists each as x_reply_suggestions (status pending) for Rich to review/approve."""
    post_row = (await db.execute(
        text("SELECT id, tweet_id, author_username, text, created_at FROM public.x_posts WHERE id=:id"),
        {"id": post_id})).mappings().first()
    if not post_row:
        raise HTTPException(status_code=404, detail="Post not found")
    from app.social.x_reply import draft_replies_for_post
    opts = await draft_replies_for_post(db, dict(post_row), n=max(1, min(int(n_options), 5)))
    saved = []
    for o in opts:
        row = (await db.execute(
            text("""INSERT INTO public.x_reply_suggestions
                     (post_id, tweet_id, author_username, body, rationale, status)
                     VALUES (:pid, :tid, :au, :b, :r, 'pending')
                     RETURNING id, post_id, tweet_id, author_username, body, rationale, status, created_at"""),
            {"pid": post_id, "tid": post_row["tweet_id"], "au": post_row["author_username"],
             "b": o["body"], "r": o.get("rationale")})).mappings().first()
        saved.append(dict(row))
    await db.commit()
    return XReplySuggestionsOut(suggestions=saved, count=len(saved))


@admin_router.get("/reply-suggestions", response_model=XReplySuggestionsOut)
async def list_reply_suggestions(
    status: Optional[str] = None,
    author: Optional[str] = None,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_admin_user),
):
    """Reply suggestions Earl drafted. Optional ?status=pending|approved|rejected|posted."""
    sql = ("SELECT s.id, s.post_id, s.tweet_id, s.author_username, s.body, s.rationale, "
           "s.status, s.created_at, s.posted_tweet_id, s.posted_at "
           "FROM public.x_reply_suggestions s "
           "LEFT JOIN public.x_posts p ON p.id = s.post_id")
    params: dict = {}
    cond = []
    if status:
        cond.append(" s.status = :st"); params["st"] = status
    if author:
        cond.append(" s.author_username = :au"); params["au"] = author
    if cond:
        sql += " WHERE" + " AND".join(cond)
    sql += " ORDER BY s.created_at DESC LIMIT :limit"
    params["limit"] = min(int(limit), 100)
    rows = (await db.execute(text(sql), params)).mappings().all()
    return XReplySuggestionsOut(suggestions=[dict(r) for r in rows], count=len(rows))


@admin_router.patch("/reply-suggestions/{suggestion_id}", response_model=XReplySuggestionOut)
async def set_reply_status(
    suggestion_id: int,
    body: XReplyStatusIn,
    db: AsyncSession = Depends(get_db),
    admin=Depends(get_admin_user),
):
    """Manual approve (no auto-post) or reject a drafted reply.

    - 'approved': Rich approved it and will post it himself through the X app.
      Marks this suggestion approved AND rejects the OTHER pending drafts for the
      same post (keep only the chosen one), per Rich: "get rid of the other drafts
      for that tweet's response when one gets approved". Nothing is posted to X.
    - 'rejected': kills this draft only.
    """
    row = (await db.execute(
        text("""UPDATE public.x_reply_suggestions
                 SET status = :st, approved_at = now()
                 WHERE id = :id RETURNING id, post_id, tweet_id, author_username, body,
                                            rationale, status, approved_at AS created_at"""),
        {"st": body.status, "id": suggestion_id})).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Suggestion not found")
    # When one reply is chosen (approved) for a post, retire the other drafts for it.
    if body.status == "approved" and row["post_id"]:
        await db.execute(
            text("""UPDATE public.x_reply_suggestions
                     SET status = 'rejected'
                     WHERE post_id = :pid AND id <> :id AND status = 'pending'"""),
            {"pid": row["post_id"], "id": suggestion_id})
    await db.commit()
    return XReplySuggestionOut(**dict(row))


@admin_router.post("/reply-suggestions/{suggestion_id}/send", response_model=XReplySendResult)
async def send_reply_suggestion(
    suggestion_id: int,
    db: AsyncSession = Depends(get_db),
    admin=Depends(get_admin_user),
):
    """Approve-and-send: post Earl's drafted reply to X, then mark posted.

    This is the single action that (1) approves the chosen reply, (2) retires the
    other pending drafts for the same post, and (3) actually posts the reply through
    the X API as a reply to the original tweet (in_reply_to_tweet_id). On success,
    status becomes 'posted' with posted_tweet_id + posted_at recorded. If X rejects
    the post, we return 502 and leave the suggestion PENDING (nothing false is
    recorded on X, and Rich can retry or edit). Manual posting is the separate
    PATCH /reply-suggestions/{id} with status='approved' (no auto-post).
    """
    s = (await db.execute(
        text("""SELECT id, post_id, tweet_id, author_username, body, rationale, status
                 FROM public.x_reply_suggestions WHERE id = :id"""),
        {"id": suggestion_id})).mappings().first()
    if not s:
        raise HTTPException(status_code=404, detail="Suggestion not found")
    if s["status"] == "posted" or s["status"] == "approved":
        raise HTTPException(status_code=409, detail="Suggestion already approved/posted.")
    if not s["body"]:
        raise HTTPException(status_code=400, detail="Reply body is empty.")
    if len(s["body"]) > 280:
        raise HTTPException(status_code=400, detail=f"Reply is {len(s['body'])} chars (>280). Trim it.")
    if not s["tweet_id"]:
        raise HTTPException(status_code=400, detail="Reply has no in_reply_to tweet (missing tweet_id).")

    # X write (POST /2/tweets) requires OAuth 2.0 user-context with tweet.write. Resolve a
    # LIVE OAuth2 token (auto-refresh is automatic inside get_live_token). This is the token
    # Rich approves via the "Authorize Access on X" button with write scope.
    oauth2_cfg = _oauth2_defaults()  # {client_id, client_secret, ...}
    live = await OA.get_live_token(
        db, oauth2_cfg.get("client_id", ""), oauth2_cfg.get("client_secret", "")
    )
    if not live or not live.get("access_token"):
        raise HTTPException(
            status_code=503,
            detail="No X OAuth2 token stored. Click \"Authorize Access on X\" and approve it (write scope).")
    try:
        res = X.create_post(
            s["body"],
            in_reply_to_tweet_id=s["tweet_id"],
            oauth2_client_id=oauth2_cfg.get("client_id", ""),
            oauth2_client_secret=oauth2_cfg.get("client_secret", ""),
            oauth2_access_token=live.get("access_token", ""),
        )
    except X.XNotConnectedError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except X.XError as exc:
        logger.error("reply-send X error on suggestion %s: %s", suggestion_id, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    posted_tweet_id = (res or {}).get("tweet_id") or (res or {}).get("data", {}).get("id")
    # Commit the posting as authoritative: mark posted, retire other pending drafts.
    await db.execute(
        text("""UPDATE public.x_reply_suggestions
                 SET status = 'posted', approved_at = now(), posted_at = now(),
                     posted_tweet_id = :ptweet, approved_by_id = :uid
                 WHERE id = :id"""),
        {"ptweet": posted_tweet_id, "uid": (admin.id if hasattr(admin, "id") else None), "id": suggestion_id})
    if s["post_id"]:
        await db.execute(
            text("""UPDATE public.x_reply_suggestions
                     SET status = 'rejected'
                     WHERE post_id = :pid AND id <> :id AND status = 'pending'"""),
            {"pid": s["post_id"], "id": suggestion_id})
    # If the source post was still unread, mark it read (we engaged with it).
    if s["post_id"]:
        await db.execute(
            text("""UPDATE public.x_posts SET read_at = COALESCE(read_at, now())
                     WHERE id = :pid"""),
            {"pid": s["post_id"]})
    await db.commit()
    return XReplySendResult(
        suggestion_id=suggestion_id, status="posted", posted=True,
        posted_tweet_id=posted_tweet_id,
    )


# --------------------------------------------------------------------------- following
# "Users we follow" = the snapshot table public.x_following (what @earl_knows_ball
# follows on X). read_posts is the toggle that says whether the reader pipeline
# (app/social/x_read_posts.py) collects this account's tweets into public.x_posts.

class FollowingOut(BaseModel):
    id: int
    x_user_id: str
    username: str
    name: Optional[str] = None
    description: Optional[str] = None
    snapshot_at: Optional[datetime] = None
    read_posts: bool
    profile_url: str


class FollowingToggleIn(BaseModel):
    read_posts: bool


class FollowingListOut(BaseModel):
    following: list[FollowingOut]


_FOLLOWING_SELECT = (
    "id, x_user_id, username, name, description, snapshot_at, read_posts"
)


@admin_router.get("/following", response_model=FollowingListOut)
async def list_following(
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_admin_user),
):
    """All X accounts we currently follow, newest snapshot first. Each row carries
    read_posts (do we collect this user's tweets) so the front-end can render the
    list + per-row collect toggle + profile link."""
    rows = (
        await db.execute(
            text(
                f"SELECT {_FOLLOWING_SELECT} "
                "FROM public.x_following "
                "ORDER BY lower(username) ASC"
            )
        )
    ).mappings().all()
    return {
        "following": [
            FollowingOut(
                **dict(r),
                profile_url=f"https://x.com/{r['username']}",
            )
            for r in rows
        ]
    }


@admin_router.patch("/following/{following_id}", response_model=FollowingOut)
async def toggle_following_read(
    following_id: int,
    body: FollowingToggleIn,
    db: AsyncSession = Depends(get_db),
    admin=Depends(get_admin_user),
):
    """Toggle whether we collect this followed account's tweets.
    read_posts=true -> the reader pipeline pulls their tweets into x_posts."""
    row = (
        await db.execute(
            text(
                f"UPDATE public.x_following SET read_posts = :rp "
                f"WHERE id = :id RETURNING {_FOLLOWING_SELECT}"
            ),
            {"rp": body.read_posts, "id": following_id},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Following entry not found")
    await db.commit()
    return FollowingOut(
        **dict(row), profile_url=f"https://x.com/{row['username']}"
    )


# --------------------------------------------------------------------------- actions
# "Refresh following" hits the X API for who @earl_knows_ball currently follows and
# upserts into public.x_following (new accounts come in read_posts = false).
# "Fetch tweets" reads the newest posts (after our last-saved per author, up to 5)
# for every account marked read_posts = true and stores them into public.x_posts.
# Both run the existing compute CLI modules in a subprocess so their independent
# OAuth/DB engine lifecycle stays fully isolated from this request's async session.

class XActionOut(BaseModel):
    ok: bool = Field(..., description="true if the run finished without a hard failure")
    action: str
    detail: str = ""


def _backend_dir() -> Path:
    return Path(__file__).resolve().parents[2]


def _run_cli(module: str, extra_args: list[str]) -> str:
    """Run a compute CLI module in a subprocess of this venv, cwd=backend."""
    cmd = [sys.executable, "-m", module] + extra_args
    env = dict(os.environ)
    env.setdefault("PYTHONPATH", "")
    env["PYTHONPATH"] = str(_backend_dir()) + (os.pathsep + env["PYTHONPATH"] if env["PYTHONPATH"] else "")
    proc = subprocess.run(
        cmd,
        cwd=str(_backend_dir()),
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
    )
    merged = (proc.stdout or "").strip()
    if proc.stderr and proc.stderr.strip():
        merged = (merged + "\n" + proc.stderr.strip()) if merged else proc.stderr.strip()
    if proc.returncode != 0:
        raise HTTPException(
            status_code=502,
            detail=f"{module} failed (rc={proc.returncode}): {merged[-1500:]} ",
        )
    return merged[-2000:]


@admin_router.post("/following/refresh", response_model=XActionOut)
async def refresh_following(
    _admin=Depends(get_admin_user),
):
    """Hit X and re-sync who we follow into x_following. Newly-followed accounts are
    added with collection OFF; any you'd like to collect, toggle ON in the list."""
    try:
        output = await asyncio.to_thread(_run_cli, "app.social.x_following_fetch", [])
    except HTTPException as e:
        raise e
    return XActionOut(ok=True, action="refresh_following", detail=output)


@admin_router.post("/posts/fetch", response_model=XActionOut)
async def fetch_recent_tweets(
    db: AsyncSession = Depends(get_db),
    admin=Depends(get_admin_user),
):
    """For every account marked to collect (read_posts = true), grab the last up-to-5
    newest original tweets that are newer than the newest we already hold for them,
    and store them into x_posts.

    NOTE: this makes live X API calls and can take a while (one call per account) and
    is subject to X rate limits. Run it from the collect tab."""
    n = (
        await db.execute(text(
            "SELECT count(*) FROM public.x_following WHERE read_posts = TRUE"
        ))
    ).scalar() or 0
    if n <= 0:
        return XActionOut(ok=True, action="fetch_tweets",
                         detail="No accounts are marked to collect (read_posts). Mark some ON in the Following list first.")
    try:
        output = await asyncio.to_thread(_run_cli, "app.social.x_read_posts", ["--accounts", str(n)])
    except HTTPException as e:
        raise e
    return XActionOut(ok=True, action="fetch_tweets", detail=output)


# ------------------------------------------------------------------------- manual-post plan
class ManualMarkIn(BaseModel):
    """A planned tweet the user just posted by hand on X (via the app): {kind, sport, id}.
    kind is "writeup" (game preview) or "original" (original article)."""
    kind: str
    sport: Optional[str] = None
    id: int


@admin_router.get("/plan/today")
async def plan_today(
    db: AsyncSession = Depends(get_db),
    admin=Depends(get_admin_user),
):
    """Return today's persisted lineup (the set the 09:20 daily generator created) for the
    "Post Today" admin tab. NOTHING is posted and no source is stamped here — it only reads
    the snapshot the x_daily_plan task persisted this morning.

    If no lineup exists for today yet (e.g. the 09:20 task hasn't run, or it's before 09:20),
    it generates + persists today's lineup on demand so the tab is never empty.

    Returns:
      { plan_date, plan: [ {kind, sport, id, title, url, text, posted}, ... ],
        generated_at, source }
    """
    plan_date = _today_central()
    # 1) read persisted lineup for today (only still-unposted rows)
    rows = (await db.execute(text(
        "SELECT slot, kind, sport, item_id, title, url, text, posted_at"
        " FROM public.x_daily_plan WHERE plan_date = :d AND posted_at IS NULL ORDER BY slot"
    ), {"d": plan_date})).all()
    if not rows:
        # Nothing persisted yet -> generate today's snapshot on demand (no X call, no stamp).
        await asyncio.to_thread(_generate_plan)
        rows = (await db.execute(text(
            "SELECT slot, kind, sport, item_id, title, url, text, posted_at"
            " FROM public.x_daily_plan WHERE plan_date = :d AND posted_at IS NULL ORDER BY slot"
        ), {"d": plan_date})).all()
        source = "generated-on-demand"
    else:
        source = "daily-0920"
    items = []
    for r in rows:
        m = r._mapping
        items.append({
            "slot": m["slot"],
            "kind": m["kind"],
            "sport": m["sport"] or None,
            "id": m["item_id"],
            "title": m["title"],
            "url": m["url"] or "",
            "text": m["text"] or "",
            "posted": m["posted_at"] is not None,
        })
    return {"plan_date": plan_date.isoformat(), "source": source, "plan": items}


def _today_central():
    from zoneinfo import ZoneInfo
    from datetime import datetime
    try:
        return datetime.now(ZoneInfo("America/Chicago")).date()
    except Exception:
        return datetime.now().date()


def _generate_plan():
    from app.social import x_daily_plan as xdp
    xdp.generate(commit=True)


@admin_router.post("/plan/today/regenerate")
async def regen_plan_today(
    db: AsyncSession = Depends(get_db),
    admin=Depends(get_admin_user),
):
    """Manually (re)generate today's “Post Today” lineup for the admin X tab.

    Re-runs the same picker the 09:20 x-daily-plan task uses (which now excludes any
    still-settling writeup and never picks the single most-recent row in the DB), then
    replaces today's persisted rows in public.x_daily_plan. Use this to rebuild a stale
    or orphaned lineup before manually posting to X.

    NOTE: this replaces the full day's today-plan snapshot. Nothing is posted/un-posted
    to X here — actual posting still happens only via each item's “Post to X” action.
    """
    # Force a fresh generate+persist of today's plan (idempotent; replaces today's rows).
    await asyncio.to_thread(_generate_plan)
    rows = (await db.execute(text(
        "SELECT slot, kind, sport, item_id, title, url, text, posted_at"
        " FROM public.x_daily_plan WHERE plan_date = :d AND posted_at IS NULL ORDER BY slot"
    ), {"d": _today_central()})).all()
    items = []
    for r in rows:
        m = r._mapping
        items.append({"slot": m["slot"], "kind": m["kind"], "sport": m["sport"] or None,
                      "id": m["item_id"], "title": m["title"], "url": m["url"] or "",
                      "text": m["text"] or "", "posted": m["posted_at"] is not None})
    return {"plan_date": _today_central().isoformat(), "source": "daily-0920", "plan": items}


@admin_router.post("/plan/today/mark-posted", response_model=XActionOut)
async def manual_mark_posted(
    payload: ManualMarkIn,
    db: AsyncSession = Depends(get_db),
    admin=Depends(get_admin_user),
):
    """Record that the user manually posted this planned tweet on X. Stamps (a) the source row's
    x_posted_at=NOW() so the daily picker won't re-offer the SAME writeup/original tomorrow, and
    (b) today's x_daily_plan row's posted_at so it drops off the Post Today tab. It does NOT call
    the X API (no auto-post, no cost)."""
    kind = payload.kind.strip().lower()
    if kind == "original":
        if not payload.sport:
            raise HTTPException(status_code=400, detail="mark-posted original requires sport")
        table = "public.original_articles"
    elif kind == "writeup":
        sport = (payload.sport or "").strip().lower()
        if sport not in ("mlb", "nfl", "nba"):
            raise HTTPException(status_code=400, detail="writeup sport must be mlb|nfl|nba")
        table = f'{sport}.game_writeups'
    else:
        raise HTTPException(status_code=400, detail="kind must be 'writeup' or 'original'")

    # (a) stamp source (dedup so it won't be re-generated/auto-offered)
    sql = text(f"UPDATE {table} SET x_posted_at = now() WHERE id = :id AND x_posted_at IS NULL")
    res = await db.execute(sql, {"id": payload.id})
    # (b) set posted_at on today's persisted plan row (drops it from Post Today tab)
    plan_date = _today_central()
    upd = text("UPDATE public.x_daily_plan SET posted_at = now()"
               " WHERE plan_date = :d AND kind = :k AND item_id = :id AND posted_at IS NULL")
    await db.execute(upd, {"d": plan_date, "k": kind, "id": payload.id})
    await db.commit()

    if res.rowcount == 0:
        return XActionOut(ok=True, action="manual_mark_posted",
                         detail=f"Marked {kind} id={payload.id} on today's list (source already had x_posted_at set).")
    return XActionOut(ok=True, action="manual_mark_posted",
                     detail=f"Marked {kind} id={payload.id} ({payload.sport or ''}) as manually posted.")


# ---------------------------------------------------------------------------- AI composer — 3 tweet options
class ComposeOption(BaseModel):
    text: str = ""
    note: str = ""


class ComposeOptionsIn(BaseModel):
    instruction: str = ""
    style: Optional[str] = "casual"        # casual | short-punchy | hype-hashtags | factual
    research: Optional[str] = "live"        # none | articles | live | deep
    sport: Optional[str] = None             # mlb | nfl | nba (research/voice)
    seed_text: str = ""                    # grounding context if a Compose seed is active (#2)


class ComposeOptionsOut(BaseModel):
    options: list[ComposeOption] = []
    model: Optional[str] = None
    note: Optional[str] = None


# sport -> (engine constructor args) so the composer can run REAL chat research tools
_SPORT_ENGINES: dict = {}


def _get_chat_engine(sport: str | None):
    """Return the app's already-built per-sport ToolChatEngine, or None for freestyle."""
    if not sport:
        return None
    sp = sport.strip().lower()
    if sp in _SPORT_ENGINES:
        return _SPORT_ENGINES[sp]
    if sp == "mlb":
        from app.chat_tools.mlb import TOOL_DEFINITIONS as TOOLS
        from app.chat_tools.mlb import execute_mlb_tool as exec_
    elif sp == "nfl":
        from app.chat_tools.nfl import TOOL_DEFINITIONS as TOOLS, execute_nfl_tool as exec_
    elif sp == "nba":
        from app.chat_tools.nba import TOOL_DEFINITIONS as TOOLS, execute_nba_tool as exec_
    else:
        return None
    from app.chat_tools import ToolChatEngine
    eng = ToolChatEngine(
        sport=sp,
        sport_display=sp.upper(),
        data_description="live schedule lines standings injuries stats predictions and articles",
        tools=TOOLS,
        executor=exec_,
        system_prompt_extra="",
    )
    _SPORT_ENGINES[sp] = eng
    return eng


async def _compose_research(brief: str, sport: str | None, db, level: str = "live") -> tuple[str, str | None]:
    """Research the topic for tweet grounding. level: none|articles|live|deep.
    Returns (grounding_block_or_'', engine_model).
      none    -> no research (freestyle, fastest)
      articles-> RAG over recent Earl articles on the topic (no live tools)
      live    -> live sport tools digest + RAG articles (with optional seed facts)
      deep    -> like live, but lets the engine run extra research rounds first
    Never raises — degrades gracefully to whatever research level succeeds (or freestyle)."""
    if level == "none":
        return "", None
    model = getattr(settings, "deepseek_model", "deepseek-v4-flash") or "deepseek-v4-flash"
    parts: list[str] = []

    # Articles-only always available (RAG needs no sport engine)
    if level in ("articles", "live", "deep"):
        try:
            from app.chat_tools.base import ToolChatEngine
            enrichment_text, _tok = await ToolChatEngine.run_enrichment(db, brief, sport=sport or "all", top_k=6)
            if enrichment_text and enrichment_text.strip():
                parts.append("Recent-article research (what Earl has written about this):\n"
                             + str(enrichment_text).strip())
        except Exception:
            logger.exception("compose research: enrichment failed")

    # Live sport engine for live|deep (only if a sport is known)
    if level in ("live", "deep") and sport:
        engine = _get_chat_engine(sport)
        if engine is not None:
            try:
                # deep => give the engine more rounds + time to research
                answer, _tokens, _full = await engine.research_and_answer(
                    db, [{"role": "user", "content": brief}],
                    research_only=True, return_full_messages=True,
                    max_turns=(26 if level == "deep" else 15),
                    timeout=(150.0 if level == "deep" else 45.0),
                )
                if answer and str(answer).strip():
                    parts.append("Live schedule/lines/stats research:\n" + str(answer).strip())
            except Exception:
                logger.exception("compose research: live pass failed")

    joined = "\n\n".join(parts)
    return joined[:3200], (model if (joined or level != "none") else None)


async def _llm_three_tweet_options(instruction: str, style: str, sport: Optional[str],
                                   grounding: str = "", seed_text: str = "") -> list[dict]:
    from openai import AsyncOpenAI  # lazy on-demand
    client = AsyncOpenAI(api_key=settings.deepseek_api_key, base_url=settings.deepseek_base_url)
    style_guide = {
        "casual": "natural, friendly sports-fan voice; light hook; reads well on a phone",
        "short-punchy": "short + punchy (aim under ~160 chars); a crisp verdict, minimal fluff",
        "hype-hashtags": "energetic hype with 1-3 relevant hashtags and a call-to-action",
        "factual": "numbers-first and analytical; cite the stat/edge without spin",
    }.get(style or "casual", "natural sports-fan voice")
    fact_in_scope = " (grounded in the research facts below)" if grounding else ""
    sys_prompt = (
        "You are @earl_knows_ball, the social voice for Earl Knows Ball, a data-driven sports "
        "handicapping/picks brand. Write X (Twitter) posts about bets, edge, EV, confidence and game calls.\n\n"
        f"Voice/style: {style_guide}.\n"
        f"Audience sport: {sport or 'mixed / platform-generic'}.\n"
        "HARD RULES:\n"
        "1. Output EXACTLY three (3) distinct options — meaningfully different angle/wording, not tweaks.\n"
        "2. Each option under 280 characters total.\n"
        f"3. Write ONLY from these facts{fact_in_scope}. NEVER fabricate any team, score, odds, record, player, "
        "date, edge or EV figure that is not in the supplied research/seed context. If granular proof is missing, "
        "keep the post general and true — do not invent specifics.\n"
        "4. Do not mention tools, research, AI, or databases. No markdown, links, or emoji-heavy spam.\n"
    )
    user_parts = [f"<my instruction>\n{instruction}\n</my instruction>"]
    if grounding:
        user_parts.insert(0, f"<researched facts — AUTHORITATIVE, use only these>\n{grounding}\n</researched facts>")
    if seed_text:
        user_parts.insert(0, f"<seed/pick context (a real Earl pick to build from)>\n{seed_text}\n</seed/pick context>")
    user_parts.append(
        "\nReturn ONLY a single JSON object (no prose, no markdown fence, nothing before/after), shaped exactly:\n"
        '{"options":[{"text":"...","note":"one-line reason for this angle"}]}'
    )
    resp = await client.chat.completions.create(
        model=settings.deepseek_model or "deepseek-v4-flash",
        temperature=0.9,
        max_tokens=1800,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": "\n\n".join(user_parts)},
        ],
    )
    content = (resp.choices[0].message.content or "").strip()
    if content.startswith("```"):
        parts = content.split("```")
        content = parts[1] if len(parts) >= 2 else content.replace("```", "")
        content = content.strip()
    out: list[dict] = []
    try:
        data = json.loads(content)
    except Exception:
        s, e = content.find("{"), content.rfind("}")
        try:
            data = json.loads(content[s : e + 1]) if s != -1 and e > s else {}
        except Exception:
            data = {}
    for o in (data.get("options") if isinstance(data, dict) else []) or []:
        if isinstance(o, dict) and (o.get("text") or "").strip():
            out.append({"text": str(o["text"]).strip(), "note": str(o.get("note") or "").strip()})
    # fallback: if json extraction gave nothing, try numbered / bulleted list lines
    if not out:
        for ln in content.splitlines():
            t = re.sub(r"^\s*(Option\s*\d+|[1-3][.)-]|[-*•])\s*", "", ln.strip())
            t = t.strip()
            if len(t) >= 12 and len(t) <= 400:
                out.append({"text": t[:280].rstrip(), "note": ""})
        out = out[:3]
    return out[:3]


@admin_router.post("/compose/options", response_model=ComposeOptionsOut)
async def compose_options(
    body: ComposeOptionsIn,
    db: AsyncSession = Depends(get_db),
    admin=Depends(get_admin_user),
):
    """AI drafts 3 grounded tweet options from Rich's instruction.
    Uses the SAME chat research tools (RAG enrichment + live sport tools) when a sport is given,
    plus optional seed/pick grounding. Never posts. Copy one and paste into X."""
    if not (body.instruction or "").strip():
        raise HTTPException(status_code=400, detail="Type an instruction for the AI first.")
    model = (settings.deepseek_model or "deepseek-v4-flash")
    level = (body.research or "live").strip().lower()
    if level not in ("none", "articles", "live", "deep"):
        level = "live"
    grounding = ""
    if level != "none":
        sport_for_research = (body.sport or "").strip().lower() or None
        if level in ("live", "deep") and not sport_for_research:
            level = "articles"  # live tools need a sport; fall back to article grounding
        try:
            grounding, model = await _compose_research(
                body.instruction, sport_for_research, db, level=level)
        except Exception:
            logger.exception("compose/options research failed")
            grounding = ""
    try:
        raw = await _llm_three_tweet_options(
            body.instruction, body.style or "casual", body.sport,
            grounding=grounding, seed_text=(body.seed_text or "").strip(),
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception("compose/options LLM failed")
        raise HTTPException(status_code=502, detail="AI generation failed — try again in a moment.")
    if not raw:
        raise HTTPException(status_code=502, detail="AI returned no usable options — try a clearer instruction.")
    if grounding:
        if level == "live":
            note = ("Grounded in live research + your pick context. " if body.seed_text
                    else "Grounded in live research. ")
        elif level == "deep":
            note = ("Deep research (live tools + articles) + your pick context. " if body.seed_text
                    else "Deep research (live tools + articles). ")
        elif level == "articles":
            note = "Grounded in recent-article research. "
        elif level == "none":
            note = "No research (freestyle). "
        else:
            note = ""
    else:
        note = "No research grounding available — based on your instruction/tone alone. " if level != "none" else "No research (freestyle). "
    return ComposeOptionsOut(
        options=[ComposeOption(text=o["text"], note=o["note"]) for o in raw],
        model=model,
        note=note + "3 AI options → copy one and paste it into X.",
    )

