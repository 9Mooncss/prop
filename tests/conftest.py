import os

import pytest

from propguard.db.migrate import upgrade
from propguard.db.session import session_factory


@pytest.fixture
def db_url(tmp_path):
    url = os.environ.get("PROPGUARD_TEST_DATABASE_URL")
    if url:  # e.g. postgresql+psycopg://... ; schema reset per test
        from sqlalchemy import create_engine, text
        eng = create_engine(url)
        with eng.begin() as c:
            c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
        eng.dispose()
    else:
        url = f"sqlite:///{tmp_path}/test.db"
    upgrade(url)
    return url


@pytest.fixture
def sf(db_url):
    return session_factory(db_url)
