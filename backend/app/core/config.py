from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "EarlKnowsBall"
    database_url: str = "postgresql+asyncpg://earl:earl@localhost:5432/earl_knows_football"

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
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 1440  # 24h

    # Resend (email)
    resend_api_key: str = ""

    # Stripe
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_publishable_key: str = ""

    # App
    base_url: str = "http://localhost:3000"
    admin_email: str = "admin@earlknowsball.com"

    model_config = {"env_file": ".env"}


settings = Settings()
