"""Programmatic Alembic runner (used by CLI, container entrypoint and tests)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

MIGRATIONS = Path(__file__).resolve().parent / "migrations"


def alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def upgrade(url: str, revision: str = "head") -> None:
    from propguard.db.session import get_engine
    get_engine(url)  # creates sqlite parent dir
    command.upgrade(alembic_config(url), revision)


def downgrade(url: str, revision: str) -> None:
    command.downgrade(alembic_config(url), revision)


def current(url: str) -> str | None:
    from alembic.runtime.migration import MigrationContext
    from propguard.db.session import get_engine
    with get_engine(url).connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()
