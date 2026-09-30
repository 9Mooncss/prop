# ADR-0001: Stack — Python, FastAPI, SQLAlchemy/Alembic, PostgreSQL (+SQLite local), Docker Compose

Status: accepted (2026-09-29)

Decision: keep the suggested default stack. Python 3.11, FastAPI + Jinja server-rendered dashboard (no SPA
build chain), SQLAlchemy 2 ORM with portable JSON columns, Alembic migrations, PostgreSQL 16 for servers and
SQLite (WAL) for local single-user mode, one image used for `api` and `worker`, Docker Compose.

Rejected: Kubernetes, Kafka, Elasticsearch, Celery/Redis (a single-owner system with an hourly monitor loop
and in-process trading sessions does not need a broker; a sleep loop + DB is simpler and restart-safe).

Trade-offs: SQLite ignores VARCHAR lengths — the suite is also run on PostgreSQL
(`PROPGUARD_TEST_DATABASE_URL`), which already caught one bug.
