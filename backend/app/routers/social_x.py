"""X (@earlknowsball) admin router — connect/verify account, view source seeds, manage
drafts, and send a post. Admin-only (compute role + get_admin_user).

Phase 0/1 scope (per Rich): manual composer + approve/send. No auto-scheduler yet.
All routes that touch credentials or POST externally are locked behind get_admin_user.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.database import get_db
from app.routers.admin import get_admin_user
from app.social import x_client as X
from app.social import post_sources as SRC

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
