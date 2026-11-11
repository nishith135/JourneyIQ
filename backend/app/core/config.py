from datetime import datetime, timezone
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    app_name: str = "JourneyIQ"
    database_url: str = "postgresql+psycopg://postgres@127.0.0.1:5439/journeyiq"
    test_database_url: str | None = None
    test_database_url: str | None = None
    app_env: str = "development"
    default_random_seed: int = 42
    default_as_of_timestamp: datetime = datetime(2026, 11, 10, 0, 0, 0, tzinfo=timezone.utc)
    sessionization_version: str = "ses-v1.0"
    classification_version: str = "bot-v1.0"
    calculation_version: str = "out-v1.0"

    model_config = SettingsConfigDict(
        env_file=str(ROOT_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
