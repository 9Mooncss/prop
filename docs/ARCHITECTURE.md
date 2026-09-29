# Architecture

## Processes (docker compose)

```
            ┌────────────── api (FastAPI) ──────────────┐      ┌──── worker ────┐
 browser ──▶│ dashboard, JSON API, /health, /metrics    │      │ rule monitoring │──▶ prop-firm sites (polite GET)
            │ owner-token mutations, recommendations,   │      │ freshness alerts│──▶ webhook / Telegram (optional)
            │ simulations                               │      │ heartbeat       │──▶ Anthropic API (optional, fragments only)
            └──────────────────────┬────────────────────┘      └───────┬────────┘
                                   └──────────── PostgreSQL ◀──────────┘   (SQLite in local mode)
```

Trading sessions (`AccountSession`) run in-process in the CLI (`propguard paper run`) today; a live
adapter-backed session daemon is future work (KNOWN_LIMITATIONS.md). All sessions share the same
persistent stores (orders, risk state, kill switches, ledger, audit) so the dashboard shows them.

## Layers and dependency rules

```
risk/          pure, deterministic. imports only rules/ (types) and stdlib. NO db, network, llm.
rules/         typed rule kinds (pydantic) + RuleSet snapshot. NO db.
execution/     interfaces, PreTradeGuard, ExecutionEngine, AccountSession, reconciliation, simulator.
               depends on risk/ + rules/; persistence via protocols (execution/stores.py).
registry/      DB-backed Rule Registry, provenance, eligibility, RuleSet assembly.
monitor/       fetch → extract → hash → diff → classify; may call llm/ for *proposals only*.
llm/           optional; output only reaches RuleChange.new_value (PENDING). Never imported by risk/ or execution/.
recommender/   replay, Monte Carlo, scoring, persisted recommendations.
api/, cli.py, worker.py   composition roots.
```

`tests/integration/test_no_bypass.py` enforces the critical ones statically (no calls to adapter hooks
outside the adapter base class, no access to the guard's signing key, strategies/recommender never import
adapters).

## Order path (the only one)

```
Strategy.on_bar → Signal ─▶ AccountSession.submit_signal → PositionSizer
   ─▶ ExecutionEngine.execute(order)
        1. OrderStore.reserve(client_order_id)      (idempotency; duplicate → returned, never resent)
        2. context = fresh snapshot + RiskState + RuleSet + quotes + kill switches + recon status + news
        3. PreTradeGuard.authorize → RiskEngine.evaluate → RiskDecision (audited, with state snapshot)
        4. ALLOW/MODIFY → ApprovedOrder (HMAC over exact fields, adapter-bound, TTL 5 s, single use)
        5. LiveGate.check (again) → BrokerAdapter.submit(approved)  (base class verifies & consumes token)
        6. timeout → find_order(client id) → adopt | re-evaluate & resend (fresh approval) | kill switch
        7. report → order store + ledger + audit; snapshot refresh
```

Supervisor (`AccountSession.tick`): snapshot → dedup/ordered broker events → RiskState update (rollover,
HWM; out-of-order rejected) → reconciliation → assessment → kill switches → deterministic protective
actions (flatten before prohibited holding periods, emergency exit at internal floor). Protective actions
also go through the guard (they are risk-reducing so they pass even under kill switches).

## Rule lifecycle

```
seed research / monitor ──▶ Rule v1 (UNVERIFIED, conf 0.6, evidence rows with fragments)
   raw page fetched: fragment found (≥85% tokens) → evidence SOURCE_MATCHED (0.8)
   owner compares with source → verify_rule → Rule v2 CONFIRMED (verified_at, verified_by)
   monitor re-check unchanged → effective_verified_at advances (freshness)
   monitor detects HIGH/CRITICAL change → rules citing that source + topic rules → UNCERTAIN,
       RuleChange PENDING (+ optional LLM proposal), alert → Risk Engine: RULE_UNCERTAIN / RULE_CHANGE_PENDING
       → no new risk, reductions still allowed
   conflicting official sources → Conflict OPEN, rule CONFLICT; must be resolved, then re-verified
```

## Data model (main tables)

firms, sources, document_snapshots, evidence, programs, challenges, rules (versioned, is_current),
rule_changes, conflicts, alerts, user_profiles (versioned), wallet_addresses, trade_histories,
recommendations, trading_accounts, risk_states, orders, kill_switches, ledger_positions,
processed_events, audit_log (hash-chained), llm_calls, settings. Migrations: `propguard/db/migrations`.

## Model usage (cost)

| Task | Default | Why |
|---|---|---|
| unchanged page | nothing (304 / equal hash) | zero cost |
| extraction, canonicalization, diff, criticality | deterministic code | reliable & free |
| changed fragment interpretation | cheap tier (`PROPGUARD_LLM_MODEL_CHEAP`) | small structured task |
| ambiguous / low confidence / invalid params | strong tier (`PROPGUARD_LLM_MODEL_STRONG`) | escalation only |
| confirmation of rules | **human owner** | LLM output is never authoritative |

Model ids are configuration; prices are overridable (`PROPGUARD_LLM_PRICES`). Monthly budget cap
fails closed (rule stays UNCERTAIN). Results are cached by input hash.
