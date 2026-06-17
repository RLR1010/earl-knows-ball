#!/usr/bin/env python3
"""One-time script to create/verify the admin user.

Usage:
    python -m app.seed_admin
"""

import asyncio
import sys
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session, init_db
from app.models import User
from app.models.admin import SubscriptionPlan
from app.core.config import settings

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


async def seed():
    await init_db()

    async with async_session() as db:
        # ── Admin user ─────────────────────────────────────────────
        admin_email = settings.admin_email
        result = await db.execute(select(User).where(User.email == admin_email))
        admin = result.scalar_one_or_none()

        if admin:
            # Ensure admin flag is set
            if not admin.is_admin:
                admin.is_admin = True
                print(f"✓ Updated '{admin_email}' → admin=True")
            else:
                print(f"✓ Admin user already exists: {admin_email}")
        else:
            admin = User(
                email=admin_email,
                password_hash=pwd_context.hash("admin123"),  # CHANGE ME
                display_name="Admin",
                is_admin=True,
                email_verified=True,
                subscription_tier="premium",
            )
            db.add(admin)
            print(f"✓ Created admin user: {admin_email} / admin123")
            print(f"  ⚠ Please change this password after first login!")

        # ── Default subscription plans ─────────────────────────────
        plans_data = [
            {
                "name": "Premium Monthly",
                "slug": "premium_monthly",
                "description": "Full access to AI chat, advanced stats, and handicapping analysis.",
                "price_cents": 999,
                "interval": "month",
                "trial_days": 7,
                "features": [
                    "AI Chat Access (All Sports)",
                    "Advanced Statistics",
                    "Handicapping Analysis",
                    "Player Profiles",
                    "No Ads",
                ],
                "sort_order": 1,
            },
            {
                "name": "Premium Yearly",
                "slug": "premium_yearly",
                "description": "All Premium features plus 2 months free. Best value.",
                "price_cents": 9999,
                "interval": "year",
                "trial_days": 7,
                "features": [
                    "AI Chat Access (All Sports)",
                    "Advanced Statistics",
                    "Handicapping Analysis",
                    "Player Profiles",
                    "No Ads",
                    "2 Months Free",
                    "Priority Support",
                ],
                "sort_order": 2,
            },
        ]

        for plan_data in plans_data:
            existing = await db.execute(
                select(SubscriptionPlan).where(SubscriptionPlan.slug == plan_data["slug"])
            )
            if existing.scalar_one_or_none():
                print(f"  → Plan '{plan_data['name']}' already exists")
            else:
                plan = SubscriptionPlan(**plan_data)
                db.add(plan)
                print(f"✓ Created plan: {plan_data['name']} (${plan_data['price_cents']/100:.2f}/{plan_data['interval']})")

        await db.commit()

    print("\n✅ Seed complete!")


if __name__ == "__main__":
    asyncio.run(seed())
