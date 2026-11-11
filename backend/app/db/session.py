import os
from collections.abc import Generator
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings


class Base(DeclarativeBase):
    pass


def assert_safe_isolated_database_url(
    target_url: str | None,
    primary_url: str | None = None,
) -> str:
    """Validate via SQLAlchemy URL parsing that target_url points only to an isolated test/sandbox database."""
    if not target_url or not target_url.strip():
        raise ValueError("Target database URL is required and cannot be empty.")

    cleaned_target = target_url.strip()
    try:
        parsed_target = make_url(cleaned_target)
    except ArgumentError as exc:
        raise ValueError("Malformed target database URL.") from exc

    db_name = (parsed_target.database or "").strip()
    if not db_name:
        raise ValueError("Target database URL must specify a database name.")

    effective_primary = (primary_url or os.environ.get("DATABASE_URL") or settings.database_url).strip()
    try:
        parsed_primary = make_url(effective_primary)
        primary_db_name = (parsed_primary.database or "journeyiq").strip().lower()
    except ArgumentError:
        primary_db_name = "journeyiq"

    db_lower = db_name.lower()
    if db_lower == "journeyiq" or db_lower == primary_db_name:
        raise ValueError(
            f"Unsafe target database '{db_name}': isolated CLI/test operations cannot target primary database '{primary_db_name}'."
        )

    if not ("test" in db_lower or "sandbox" in db_lower):
        raise ValueError(
            f"Unsafe target database name '{db_name}': database name must explicitly include 'test' or 'sandbox' (e.g., 'journeyiq_test')."
        )
    return cleaned_target


def resolve_runtime_database_url(
    target: str | None = None,
    *,
    primary_url: str | None = None,
    sandbox_url: str | None = None,
) -> tuple[str, str]:
    """Explicitly select 'primary' (DATABASE_URL) or 'sandbox' (SANDBOX_DATABASE_URL) without silent redirects."""
    selected = (target if target is not None else settings.runtime_db_target).strip().lower()
    eff_primary = (primary_url if primary_url is not None else settings.database_url).strip()
    eff_sandbox = (
        sandbox_url if sandbox_url is not None else (settings.sandbox_database_url or "")
    ).strip()

    if selected == "primary":
        if not eff_primary:
            raise ValueError("RUNTIME_DB_TARGET='primary' requires DATABASE_URL to be configured.")
        return ("primary", eff_primary)

    if selected == "sandbox":
        if not eff_sandbox:
            raise ValueError(
                "RUNTIME_DB_TARGET='sandbox' requires SANDBOX_DATABASE_URL to be configured; refusing to fall back to primary DATABASE_URL."
            )
        validated_sandbox = assert_safe_isolated_database_url(eff_sandbox, primary_url=eff_primary)
        return ("sandbox", validated_sandbox)

    raise ValueError(
        f"Invalid RUNTIME_DB_TARGET '{selected}'. Expected 'primary' or 'sandbox'."
    )


def get_runtime_database_info(
    url: str | None = None,
    *,
    target: str | None = None,
) -> dict[str, Any]:
    """Return a credential-redacted summary of the configured runtime database target."""
    resolved_target = (target or RUNTIME_DB_TARGET).strip().lower()
    resolved_url = url or RUNTIME_DATABASE_URL
    parsed = make_url(resolved_url)
    return {
        "target": resolved_target,
        "driver": parsed.drivername,
        "host": parsed.host,
        "port": parsed.port,
        "database": parsed.database,
        "username": parsed.username,
        "url_redacted": parsed.render_as_string(hide_password=True),
    }


RUNTIME_DB_TARGET, RUNTIME_DATABASE_URL = resolve_runtime_database_url()
engine = create_engine(RUNTIME_DATABASE_URL, future=True, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def resolve_isolated_database_url(
    *,
    database_url: str | None = None,
    db_env_var: str | None = None,
    primary_url: str | None = None,
) -> str:
    """Resolve and validate an isolated database URL from explicit CLI flags without falling back to primary DB."""
    has_direct = bool(database_url and database_url.strip())
    has_env = bool(db_env_var and db_env_var.strip())

    if has_direct and has_env:
        raise ValueError("Specify either --database-url or --db-env-var, not both.")
    if not has_direct and not has_env:
        raise ValueError(
            "Explicit isolated database configuration is required: pass --database-url or --db-env-var <ENV_VAR>."
        )

    if has_env:
        assert db_env_var is not None
        env_name = db_env_var.strip()
        resolved = os.environ.get(env_name)
        if not resolved or not resolved.strip():
            raise ValueError(
                f"Environment variable '{env_name}' is not set or empty. Refusing to default to the primary database."
            )
    else:
        assert database_url is not None
        resolved = database_url

    return assert_safe_isolated_database_url(resolved, primary_url=primary_url)


def create_isolated_session_factory(
    target_url: str,
    *,
    primary_url: str | None = None,
) -> sessionmaker[Session]:
    """Create a SQLAlchemy sessionmaker only after validating target_url points to an isolated DB."""
    validated_url = assert_safe_isolated_database_url(target_url, primary_url=primary_url)
    isolated_engine = create_engine(validated_url, future=True, pool_pre_ping=True)
    return sessionmaker(bind=isolated_engine, autoflush=False, autocommit=False, expire_on_commit=False)

