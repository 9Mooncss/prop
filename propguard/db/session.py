"""Engine/session factory."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

_engines: dict[str, Engine] = {}


def get_engine(url: str) -> Engine:
    if url not in _engines:
        kw = {}
        if url.startswith("sqlite"):
            path = url.split("sqlite:///", 1)[-1]
            if path and path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            kw["connect_args"] = {"check_same_thread": False}
        eng = create_engine(url, pool_pre_ping=True, future=True, **kw)
        if url.startswith("sqlite"):
            @event.listens_for(eng, "connect")
            def _fk(dbapi_conn, _):  # pragma: no cover - trivial
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA foreign_keys=ON")
                cur.execute("PRAGMA journal_mode=WAL")
                cur.close()
        _engines[url] = eng
    return _engines[url]


def session_factory(url: str) -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(url), expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    s = factory()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def aware(dt: datetime | None) -> datetime | None:
    """SQLite drops tzinfo; all stored timestamps are UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
