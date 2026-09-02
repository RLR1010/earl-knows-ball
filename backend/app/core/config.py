from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator, ValidationError


class Settings(BaseSettings):
    app_name: str = "EarlKnowsBall"
    # 🔴 REQUIRED (no weak default). We intentionally do NOT ship a fallback
    # password/DSN here: if it's missing, Settings() raises at import time so a
    # misconfigured deploy fails loudly instead of silently connecting with a
    # publicly-knowable credential (hardening, 2026-08-24).
    database_url: str
    # Admin/DDL connection (superuser) used ONLY by ingestion/backfill/migration
    # scripts that legitimately run TRUNCATE/DROP/CREATE. Web/API traffic uses
    # `database_url` (least-privilege `earl_web` role). Falls back to
    # `database_url` for backward compatibility if ADMIN_DATABASE_URL is unset.
    admin_database_url: str | None = None

    @property
    def database_url_sync(self) -> str:
        """Sync PostgreSQL URL for SQLAlchemy."""
        return self.database_url.replace("+asyncpg", "+psycopg2")

    def get_sync_url(self) -> str:
        """Plain sync URL (no driver suffix)."""
        return self.database_url.replace("+asyncpg", "")

    # The Odds API
    odds_api_key: str = ""                                          # Paid tier (Professional, 20k/mo)
    odds_api_key_free: str = ""                                     # Free tier (500/mo)

    # DeepSeek
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"

    # Cognee (OpenClaw memory)
    cognee_url: str = "http://localhost:8000"

    # JWT
    jwt_secret: str  # REQUIRED — set JWT_SECRET in .env; no weak default (hardened 2026-08-24)
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 1440  # 24h

    # Resend (email)
    resend_api_key: str = ""

    # Stripe
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_publishable_key: str = ""

    # One-time token top-up (Stripe price id + token grant)
    stripe_token_topup_price_id: str = ""
    token_topup_grant: int = 2_000_000  # tokens credited per purchase

    # App
    base_url: str = "http://localhost:3000"
    admin_email: str = "admin@earlknowsball.com"

    # X (@earl_knows_ball) social — OAuth1 "acting as ourselves". Optional: if unset,
    # the X admin pages show a "connect" prompt instead of failing import.
    x_consumer_key: str = ""        # API Key
    x_consumer_secret: str = ""     # API Secret
    x_access_token: str = ""        # Access Token
    x_access_token_secret: str = "" # Access Token Secret

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @field_validator("jwt_secret")
    @classmethod
    def _jwt_secret_not_weak(cls, v: str) -> str:
        """Reject missing/known-weak JWT secrets so a misconfig fails fast."""
        weak = {str(), "change-me-in-production", "secret", "changeme", "your-secret-key"}
        if v in weak or len(v) < 32:
            raise ValueError(
                "jwt_secret must be >=32 chars and not a known default — set a strong "
                "JWT_SECRET in .env (generate with: openssl rand -base64 48)"
            )
        return v

    @field_validator("database_url")
    @classmethod
    def _database_url_not_weak_default(cls, v: str) -> str:
        """Reject the known dev fallback DSN so it can never run in prod."""
        weak = ("earl:earl_dev_pass",)
        if any(w in v for w in weak):
            raise ValueError(
                "database_url must not use the dev default credential (earl:earl_dev_pass) — "
                "set DATABASE_URL in .env to a real role (e.g. earl_web)."
            )
        return v


settings = Settings()
