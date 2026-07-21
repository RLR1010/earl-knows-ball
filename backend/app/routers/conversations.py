"""Conversation management endpoints — list, get, delete chat conversations per user per sport."""

import logging
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func, delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.database import get_db
from app.models import User
from app.models.chat_history import ChatHistory

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/chat/conversations/{sport}")
async def list_conversations(
    sport: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all conversations for a user + sport, with the first user message as a summary."""
    if sport not in ("nfl", "nba", "mlb"):
        raise HTTPException(status_code=400, detail="Invalid sport")

    # Get all unique conversation_ids for this user + sport
    subq = (
        select(
            ChatHistory.conversation_id,
            func.min(ChatHistory.created_at).label("started_at"),
        )
        .where(
            ChatHistory.user_id == current_user.id,
            ChatHistory.sport == sport,
        )
        .group_by(ChatHistory.conversation_id)
        .subquery()
    )

    # Get the first user message for each conversation as the title
    first_msgs = (
        select(
            ChatHistory.conversation_id,
            ChatHistory.message,
            ChatHistory.created_at,
        )
        .where(
            ChatHistory.conversation_id == subq.c.conversation_id,
            ChatHistory.role == "user",
        )
        .order_by(ChatHistory.created_at.asc())
        .limit(1)
    )

    # Simplified: get conversation_id, first user message, and message count
    # We'll use a two-step approach
    conv_ids_result = await db.execute(
        select(
            ChatHistory.conversation_id,
            func.min(ChatHistory.created_at).label("started_at"),
            func.count(ChatHistory.id).label("message_count"),
        )
        .where(
            ChatHistory.user_id == current_user.id,
            ChatHistory.sport == sport,
        )
        .group_by(ChatHistory.conversation_id)
        .order_by(func.min(ChatHistory.created_at).desc())
    )
    conv_rows = conv_ids_result.all()

    conversations = []
    for conv_id, started_at, msg_count in conv_rows:
        # Get first user message as title
        title_result = await db.execute(
            select(ChatHistory.message)
            .where(
                ChatHistory.conversation_id == conv_id,
                ChatHistory.role == "user",
            )
            .order_by(ChatHistory.created_at.asc())
            .limit(1)
        )
        first_msg = title_result.scalar_one_or_none() or ""

        # Truncate to a nice summary
        title = first_msg[:120].strip()
        if len(first_msg) > 120:
            title += "…"

        conversations.append({
            "id": conv_id,
            "title": title,
            "message_count": msg_count,
            "started_at": started_at.isoformat() if started_at else None,
        })

    return {"conversations": conversations}


@router.get("/chat/conversations/{sport}/{conversation_id}")
async def get_conversation(
    sport: str,
    conversation_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get all messages for a conversation."""
    if sport not in ("nfl", "nba", "mlb"):
        raise HTTPException(status_code=400, detail="Invalid sport")

    result = await db.execute(
        select(ChatHistory)
        .where(
            ChatHistory.user_id == current_user.id,
            ChatHistory.sport == sport,
            ChatHistory.conversation_id == conversation_id,
        )
        .order_by(ChatHistory.created_at.asc())
    )
    messages = result.scalars().all()

    if not messages:
        raise HTTPException(status_code=404, detail="Conversation not found")

    return {
        "conversation_id": conversation_id,
        "messages": [
            {
                "role": m.role,
                "content": m.message,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in messages
        ],
    }


@router.delete("/chat/conversations/{sport}/{conversation_id}")
async def delete_conversation(
    sport: str,
    conversation_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete all messages for a conversation."""
    if sport not in ("nfl", "nba", "mlb"):
        raise HTTPException(status_code=400, detail="Invalid sport")

    result = await db.execute(
        sa_delete(ChatHistory).where(
            ChatHistory.user_id == current_user.id,
            ChatHistory.sport == sport,
            ChatHistory.conversation_id == conversation_id,
        )
    )
    await db.commit()

    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Conversation not found")

    return {"deleted": True, "messages_removed": result.rowcount}
