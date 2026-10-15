"""
config.py
---------
Centralized, validated configuration for the entire system.

Every other module imports `settings` from here instead of calling
`os.getenv(...)` directly. This gives us:
  * A single source of truth
  * Fail-fast startup if a required secret is missing (pydantic validation)
  * Type-safe access (no "is this a string or an int?" bugs)

Values are loaded from a `.env` file (see .env.example) via python-dotenv,
then validated/typed by pydantic-settings.
"""

from pathlib import Path
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # --- Zoho Books OAuth ---
    zoho_client_id: str
    zoho_client_secret: str
    zoho_refresh_token: str
    zoho_organization_id: str
    zoho_api_domain: str = "www.zohoapis.in"
    zoho_accounts_domain: str = "accounts.zoho.in"
    zoho_invoice_custom_field_api_name: str = "cf_income_category"
    zoho_webhook_shared_secret: str

    # --- Anthropic / Claude ---
    anthropic_api_key: str
    claude_model: str = "claude-sonnet-5"

    # --- Local storage ---
    mis_report_root: str = "~/Documents/Zoho_MIS_Reports"

    # --- Server ---
    webhook_host: str = "127.0.0.1"
    webhook_port: int = 8787
    log_level: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def mis_report_root_path(self) -> Path:
        """Expanded, absolute Path for the MIS report root directory."""
        return Path(self.mis_report_root).expanduser().resolve()

    @property
    def zoho_oauth_token_url(self) -> str:
        return f"https://{self.zoho_accounts_domain}/oauth/v2/token"

    @property
    def zoho_books_base_url(self) -> str:
        return f"https://{self.zoho_api_domain}/books/v3"


@lru_cache
def get_settings() -> "Settings":
    """
    Cached singleton accessor. Using a function (rather than a bare module
    level instance) makes it trivial to mock in tests via
    `get_settings.cache_clear()` + monkeypatched env vars.
    """
    return Settings()


# Convenience import: `from config import settings`
settings = get_settings()
