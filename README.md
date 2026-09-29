# PropGuard

Self-hosted system for one owner that **researches prop trading firms**, checks whether a **Ukrainian
citizen** can use them (citizenship, residence, tax residency, KYC documents and IP location are
separate inputs), tracks and **normalizes challenge rules with provenance**, recommends challenges with a
transparent score breakdown, and wraps any user-selected strategy in a **deterministic, fail-closed risk
envelope** that keeps trading strictly inside the firm's rules plus an internal safety buffer.

> Not financial advice. PropGuard never buys challenges, never pays, never signs blockchain
> transactions, and never tries to bypass KYC/AML, sanctions, country/residency restrictions, geoblocks,
> VPN/VPS, EA/API/copy-trading or platform rules. New installations run **PAPER_ONLY**.

## Quick start (Docker, macOS Apple Silicon/Intel or Linux x86_64/ARM64)

```bash
cp .env.example .env          # set POSTGRES_PASSWORD and PROPGUARD_API_TOKEN
docker compose up -d          # postgres + api (migrations + seed on start) + worker
open http://127.0.0.1:8000    # dashboard;  API docs at /api/docs
```

Behind a TLS-intercepting proxy build with `docker compose build --secret ...` — see docs/DEPLOYMENT.md.

## Quick start (local, no Docker; SQLite)

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,llm]"
propguard db upgrade && propguard seed load
propguard profile set residence_country=PL ip_location_country=PL 'kyc_documents=[{"type":"passport","country":"UA"}]'
propguard history import examples/sample_trades.csv --account-size 100000
propguard recommend --history 1
propguard simulate --firm ftmo --program two-step-100k --days 20            # fails closed: rules unverified
propguard simulate --firm ftmo --program two-step-100k --days 20 --assume-confirmed   # SIMULATION ONLY
propguard serve                  # dashboard on 127.0.0.1:8000
propguard worker                 # rule monitoring loop
python -m pytest -q              # full test suite (SQLite; set PROPGUARD_TEST_DATABASE_URL for Postgres)
```

## What is in the box

| Area | Where | Notes |
|---|---|---|
| Deterministic Risk Engine | `propguard/risk/` | pure functions, Decimal math, no I/O/LLM; see docs/RISK_ENGINE.md |
| PreTradeGuard + execution | `propguard/execution/` | HMAC-signed single-use approvals; idempotent client ids; timeout resolution; reconciliation; kill switches |
| Simulated broker | `propguard/execution/simulator.py` | spread, slippage, gaps, partial fills, rejects, timeouts, duplicate events, manual trades |
| Rule Registry | `propguard/rules/`, `propguard/registry/` | typed rule kinds, versions, provenance, conflicts, owner verification |
| Eligibility | `propguard/registry/eligibility.py` | separate citizenship / residence(+region) / tax / KYC / IP checks |
| Monitoring | `propguard/monitor/` | ETag → extraction → hash → block diff → keyword criticality → optional LLM on changed fragment only |
| Recommender | `propguard/recommender/` | hard filters, weighted breakdown, trade-history replay, Monte Carlo |
| API / dashboard | `propguard/api/` | FastAPI + Jinja; owner token for mutations |
| Research seed | `seed/firms/*.json`, docs/PROP_FIRM_RESEARCH.md | point-in-time; nothing VERIFIED from seed alone |

Documentation: [ARCHITECTURE](docs/ARCHITECTURE.md) · [RULE_SCHEMA](docs/RULE_SCHEMA.md) ·
[RISK_ENGINE](docs/RISK_ENGINE.md) · [SECURITY](docs/SECURITY.md) · [PROP_FIRM_RESEARCH](docs/PROP_FIRM_RESEARCH.md) ·
[DEPLOYMENT](docs/DEPLOYMENT.md) · [RUNBOOK](docs/RUNBOOK.md) · [ADRs](docs/ADRs/) · [ASSUMPTIONS](docs/ASSUMPTIONS.md) ·
[TASKS](docs/TASKS.md) · [KNOWN_LIMITATIONS](KNOWN_LIMITATIONS.md) · [CHANGELOG](CHANGELOG.md)

## Typical owner workflow

1. Fill the profile (residence, region, tax residency, KYC documents, actual IP country, payout requirement — default `DIRECT_CRYPTO`).
2. Let the worker capture baselines of the firms' primary pages; review each rule on `/firms/<slug>` against its
   source and confirm it (`POST /api/rules/{id}/verify` or `propguard rules verify --id N`). Only confirmed and fresh
   (≤ 24 h re-checked) critical rules allow new risk.
3. Upload your trade history, run a recommendation, read the hard-filter results and the Rule Compatibility replay.
4. Buy a challenge yourself on the firm's website (PropGuard never pays).
5. Run the account in PAPER mode; LIVE requires a live adapter, `propguard acceptance`, env opt-in and an explicit
   per-account confirmation phrase (see docs/SECURITY.md).
