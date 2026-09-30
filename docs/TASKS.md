# Task registry (orchestrator source of truth)

| ID | Task | Owner (model tier) | Status |
|---|---|---|---|
| T1 | Prop-firm primary-source research → seed/firms, PROP_FIRM_RESEARCH.md | Research subagent (Sonnet tier) | done (8 firms, none VERIFIED) |
| T2 | Rule schema + RuleSet | orchestrator (Opus tier) | done |
| T3 | Deterministic Risk Engine + tests (property, DST, trailing) | orchestrator | done |
| T4 | PreTradeGuard, execution engine, reconciliation, kill switches | orchestrator | done |
| T5 | Simulated broker + failure injection | orchestrator | done |
| T6 | DB models, Alembic, SQL stores, audit chain | orchestrator | done (SQLite + PostgreSQL) |
| T7 | Registry: seed ingest, normalization, verification, conflicts, freshness | orchestrator | done |
| T8 | Eligibility (citizenship/residence/region/tax/KYC/IP) | orchestrator | done |
| T9 | Monitoring pipeline + optional LLM tiering | orchestrator | done |
| T10 | Replay, Monte Carlo, scoring, recommendations | orchestrator | done |
| T11 | API, dashboard, CLI, worker | orchestrator | done |
| T12 | Docker Compose deployment verified | orchestrator | done |
| T13 | Independent security / risk review | reviewer subagent (Fable tier) | see CHANGELOG |
| T14 | Live adapter (cTrader Open API first) | — | open, blocked on U2/U6 |
| T15 | News calendar provider | — | open |
| T16 | Persistent paper-trading daemon in worker | — | open |
