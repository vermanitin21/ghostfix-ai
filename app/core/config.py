"""Environment-backed application configuration."""

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "GhostFix AI"
    environment: str = "development"
    log_level: str = "INFO"

    nebius_api_key: SecretStr | None = None
    nebius_base_url: str = "https://api.tokenfactory.nebius.com/v1/"
    nebius_model: str = ""

    sandbox_image: str = "ghostfix-pytest-sandbox:latest"
    sandbox_timeout_seconds: int = 120
    patch_max_files: int = Field(default=2, ge=1, le=4)
    patch_max_changed_lines: int = Field(default=100, ge=1, le=200)
    patch_max_diff_bytes: int = Field(default=12000, ge=1, le=32000)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
