# Runbook

## Daily (before any trading session)
1. Dashboard: firm row shows `Last verified` within 24 h; no pending critical changes / open conflicts for the
   firm you trade; account card shows **no kill switches**, reconciliation OK, day-start known.
2. `curl 127.0.0.1:8000/metrics` → `propguard_kill_switches_active 0`, `propguard_rule_changes_pending 0`.
If any is not true the Risk Engine already blocks new risk; fix the cause, don't work around it.

## Alerts and what to do

| Alert / kill switch | Meaning | Action |
|---|---|---|
| `rule_source_changed` CRITICAL/HIGH | a monitored primary page changed on a rule topic | open `/changes`, read diff + source, compare rules on `/firms/<slug>`, re-verify affected rules (`POST /api/rules/{id}/verify` with corrected params if needed), then decide the change |
| `source_unavailable` | 3 consecutive fetch failures / blocked | open the URL yourself in a browser; if it moved, update the seed/source; rules citing it go stale after 24 h |
| `rules_not_fresh` | active account's critical rules stale/unconfirmed | verify rules; new risk blocked meanwhile |
| `RULE_VERIFICATION_FAILURE` | a critical rule isn't CONFIRMED | verify / resolve conflict, then clear with a note |
| `INTERNAL_DRAWDOWN_LIMIT` | internal floor reached; supervisor flattened | stop for the day; review; clear next trading day |
| `EXTERNAL_LIMIT_BREACHED` | firm's limit reached | challenge likely failed; contact firm; do not clear lightly |
| `UNEXPECTED_MANUAL_TRADE` / `UNKNOWN_POSITION` | position not opened by PropGuard | close or explain it on the platform; clear with note |
| `RECONCILIATION_MISMATCH` | ledger position missing on platform | check platform history (stop-out? liquidation?) |
| `CONTRADICTORY_BALANCES` / `CALCULATION_MISMATCH` | platform numbers inconsistent / differ from ours | check instrument specs, conversion rates, platform status |
| `STALE_MARKET_DATA` | quotes stale for open positions while market open | check feed/connection |
| `RECONNECT_UNCERTAIN_STATE` | adapter reconnected | review positions/orders, then clear |
| `AMBIGUOUS_EXECUTION_EVENT` | order outcome unknown after timeout | check platform for client id; reconcile; clear |
| `DAY_STATE_UNCERTAIN` (decision reason) | daily reset missed while down | read start-of-day balance/equity from firm dashboard → `AccountSession.set_day_start_manual` (audited) or wait for next reset |

Clear: `propguard killswitch clear --account ID --kind KIND --note "what you checked"` or
`POST /api/accounts/{id}/kill-switch/{kind}/clear`.

## Emergency stop
`POST /api/accounts/{id}/kill-switch` or `propguard killswitch activate --account ID` — blocks all new risk,
reductions remain possible. To stop everything: `docker compose stop worker` (monitoring) — trading sessions
must be stopped where they run. Close positions on the platform directly if PropGuard is unavailable.

## Restart / crash
Restart is safe: state, orders (idempotency keys), ledger and processed events are persisted. On start the
session reconciles; if a daily reset was missed while down, new risk is blocked until the reference is
reconstructed or entered.

## Health
`docker compose ps`; api healthcheck = `/health`; worker healthcheck = heartbeat < 180 s; logs:
`docker compose logs -f api worker` (JSON, redacted).

## Verifying integrity
`propguard audit verify` (hash chain) · `propguard db current` · `pytest -m risk`.
