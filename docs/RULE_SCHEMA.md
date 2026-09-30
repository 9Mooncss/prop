# Rule Schema (Rule Registry)

Every rule of every firm/program/phase is stored as **one versioned row** (`rules` table) holding
separately:

1. `raw_text` — the human-readable rule as stated by the source (near-verbatim);
2. `params` — the normalized machine-readable representation, validated against the typed schema of its `kind`;
3. `raw_params` — what the extractor produced before normalization (kept for review);
4. provenance — `evidence_ids` → `evidence` rows (URL/canonical URL via `sources`, page title, document type,
   retrieval time, fragment + fragment hash, snapshot id/content hash, parser version, model version, confidence,
   verification status, last verified);
5. `confidence` and `interpretation_status` (+ `interpretation_notes`).

`rule_key = firm:program:phase:kind` (`*`/`all` for firm-wide rules). A change never overwrites: it creates
version *n+1* (`is_current` moves) and a `rule_changes` row with old value, new value, source, document
hashes, parser/model version, confidence and approval state.

## Interpretation status

| Status | Meaning | Risk Engine effect (critical kinds) |
|---|---|---|
| `CONFIRMED` | owner confirmed against primary source; no open conflict | allowed if fresh (≤ `rules_max_age_h`) |
| `UNVERIFIED` | extracted (seed research / monitor) but not confirmed | new risk **denied** (`RULE_UNCERTAIN`) |
| `UNCERTAIN` | ambiguous / fields missing / source changed since confirmation | new risk denied |
| `CONFLICT` | official sources disagree; both values stored in `conflicts` | new risk denied (`RULE_CONFLICT`) |

Normalization never guesses: unknown fields, invalid values or critical fields *not stated by the source*
(schema default would apply) make the rule `UNCERTAIN` and are listed in `interpretation_notes`.

## Source priority

`TERMS (10) < TRADING_RULES (20) < RESTRICTED_COUNTRIES (25) < FAQ (30) < KYC_POLICY / PAYOUT_POLICY (35) <
PLATFORM_RULES (40) < MARKETING (80) < COMMUNITY (95, non-primary, never monitored for rules)`.
A marketing page can never outrank legal/rules documents; community sources are risk signals only.

## Firm status

Computed deterministically (`registry/service.py:classify_firm`), never higher than evidence supports:

* `EXCLUDED` — Ukraine prohibited (citizenship and residence) or research marked it excluded.
* `VERIFIED` — evidence VERIFIED for TERMS, TRADING_RULES, FAQ, RESTRICTED_COUNTRIES, KYC_POLICY, PAYOUT_POLICY,
  PLATFORM_RULES; programs present; all critical rules CONFIRMED; no open conflicts.
* `WATCHLIST` — official site + programs + Ukraine policy known, but verification incomplete.
* `INSUFFICIENT_DATA` — otherwise.

## Payout classification

`DIRECT_CRYPTO` (official payout straight to the owner's wallet) · `CRYPTO_VIA_PROVIDER` (Rise/Deel/etc., never
reported as direct) · `FIAT_ONLY` · `UNKNOWN`. Methods keep currencies, networks, min/max, fees, processing time,
KYC requirement and evidence.

## Rule kinds

Add a kind = add a pydantic model decorated with `@register_rule` in `propguard/rules/types.py`. Unknown kinds are
stored as `custom` (never confirmable, excluded from RuleSets) so nothing else must change.

| kind | criticality | params |
|---|---|---|
| `api_trading` | CRITICAL | `allowed`, `conditions` |
| `consistency_rule` | HIGH | `max_single_day_pct_of_total`, `applies_to` |
| `copy_trading` | HIGH | `allowed`, `conditions` |
| `cost_treatment` | NORMAL | `commissions_count`, `swaps_count`, `spread_in_equity` |
| `custom` | NORMAL | `name` |
| `daily_loss_limit` | CRITICAL | `enabled`, `pct`, `pct_of`, `reference`, `includes_floating`, `counts_commissions`, `counts_swaps`, `reset_time`, `reset_tz` |
| `ea_policy` | CRITICAL | `allowed`, `conditions` |
| `hft_restriction` | HIGH | `allowed`, `min_hold_seconds`, `max_orders_per_minute` |
| `inactivity_limit` | HIGH | `days` |
| `instrument_restrictions` | HIGH | `allowed_classes`, `prohibited_symbols` |
| `ip_vps_restriction` | HIGH | `vps_allowed`, `vpn_allowed`, `notes` |
| `latency_arbitrage` | HIGH | `allowed` |
| `leverage` | HIGH | `max_by_class` |
| `max_duration` | HIGH | `days` |
| `max_loss` | CRITICAL | `pct`, `mode`, `basis` |
| `max_lot_size` | HIGH | `max_lots`, `scope` |
| `max_open_positions` | NORMAL | `max_positions` |
| `max_risk_per_trade` | HIGH | `pct` |
| `min_trading_days` | NORMAL | `days` |
| `news_trading` | CRITICAL | `allowed`, `blackout_before_min`, `blackout_after_min`, `impact_levels`, `applies_to` |
| `overnight_holding` | CRITICAL | `allowed` |
| `payout_eligibility` | NORMAL | `min_days_before_first`, `min_profit_pct`, `notes` |
| `payout_frequency` | NORMAL | `every_days`, `on_demand` |
| `platform_requirement` | NORMAL | `platforms`, `notes` |
| `profit_split` | NORMAL | `pct` |
| `profit_target` | NORMAL | `pct`, `basis`, `measured_on` |
| `prohibited_strategies` | HIGH | `strategies` |
| `refund_policy` | NORMAL | `refundable`, `condition` |
| `scaling` | NORMAL | `description` |
| `stop_loss_required` | HIGH | `required`, `within_seconds` |
| `trading_day` | NORMAL | `rollover_time`, `tz`, `counts_if` |
| `weekend_holding` | CRITICAL | `allowed`, `close_by`, `tz` |

### Drawdown semantics

* `daily_loss_limit`: violated when *measured* ≤ `reference − pct × base`, where `base` = initial balance
  (`pct_of=initial_balance`) or the reference itself; `reference` ∈ initial balance, day-start balance, day-start
  equity, max(day-start balance, equity); `includes_floating` chooses equity vs balance; reset at
  `reset_time` in IANA `reset_tz` (DST-aware). `enabled=false` records an explicitly confirmed "no daily limit".
* `max_loss`: `static` floor = initial × (1 − pct); `trailing` = intraday HWM − pct × initial;
  `eod_trailing` = end-of-day HWM − pct × initial; `trailing_lock_at_initial` = trailing but never above the
  initial balance. `basis` equity/balance selects which HWM and measured value.
* Costs: commissions and swaps are included in balance/equity as the platform reports them (`cost_treatment`).

## Seed file format

See `seed/SEED_SCHEMA.md`.
