# Changelog

## 0.1.0 — 2026-09-30 (MVP)
* Deterministic Risk Engine: daily/max loss (static, trailing, EOD trailing, lock-at-initial), four daily
  references, equity/balance basis, worst-case sizing incl. slippage/stop gaps/commissions/open & pending risk,
  adaptive safety buffer, conduct rules (automation, instruments, weekend, overnight, news, rollover guard),
  profit-target risk cap, kill-switch triggers, protective supervisor actions.
* PreTradeGuard with HMAC single-use approvals; idempotent ExecutionEngine with timeout resolution; reconciliation;
  14 kill-switch kinds; PAPER_ONLY default and LIVE gate.
* Simulated broker with failure injection; DEMONSTRATION ONLY SMA strategy; end-to-end simulated challenge.
* Rule Registry (36 rule kinds), versioning, provenance, conflicts, owner verification, freshness.
* Eligibility with separate citizenship / residence (+region) / tax residency / KYC / IP checks.
* Monitoring: conditional GET → deterministic extraction → hash → block diff → criticality → optional tiered LLM.
* Recommender: hard filters, weighted breakdown, trade-history replay, Monte Carlo, persisted traceable records.
* FastAPI API + dashboard, CLI, worker, Alembic migrations, Docker Compose (verified healthy on PostgreSQL 16).
* Research seed for 8 firms (none VERIFIED; E8 Markets EXCLUDED for Ukraine).
* Independent security/risk review (most capable model tier) — all findings fixed with regression tests:
  HIGH resend after a failed order lookup (could triple-execute); HIGH state-less REDUCE accepted any size;
  MEDIUM reconciliation trusted size increases; read endpoints exposed PII without token; loopback fallback
  vs proxy headers; eligibility passed unset tax residency / unconfirmed KYC; LOW redaction of DB URLs,
  live-gate fingerprint scope, unbounded consumed-approval set, max-lot total ignored pending orders;
  `emergency` flag settable by callers.
* Fixes found by testing: day-state from previous trading day not detected; weekend day-start loss; stale-quote
  kill switch while market closed; UA header lost with injected client; audit write deadlock on SQLite;
  cache-key length overflow on PostgreSQL.
