from datetime import datetime, timezone
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[3]
LIVE_DEMO_OUTPUT_DIR = ROOT_DIR / "data-generation" / "output"


class Settings(BaseSettings):
    app_name: str = "JourneyIQ"
    database_url: str = "postgresql+psycopg://postgres@127.0.0.1:5439/journeyiq"
    test_database_url: str | None = None
    sandbox_database_url: str | None = None
    runtime_db_target: str = "primary"
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


def resolve_safe_output_dir(
    output_dir: str | Path | None,
    *,
    allow_live_demo_overwrite: bool = False,
) -> Path:
    """Validate artifact output directory so live demo outputs are never overwritten implicitly."""
    if output_dir is None or (isinstance(output_dir, str) and not output_dir.strip()):
        raise ValueError(
            "An explicit output directory is required when writing artifacts "
            "(pass --output-dir or disable artifact writing)."
        )
    resolved_target = Path(output_dir).expanduser().resolve()
    resolved_live = LIVE_DEMO_OUTPUT_DIR.resolve()
    if resolved_target == resolved_live and not allow_live_demo_overwrite:
        raise ValueError(
            f"Refusing to write to live demo artifact directory '{resolved_live}'. "
            "Use an isolated output directory or explicitly pass --allow-live-demo-artifacts."
        )
    return resolved_target
