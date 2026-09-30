# Risk Engine

`propguard/risk/` is the most critical component. It is **deterministic**: a pure function of a
`RiskContext` (time is an input), `Decimal` arithmetic, no database, network, clock or LLM access.
`ENGINE_VERSION` is recorded with every decision.

## Decision output

`RiskDecision`: `action` (ALLOW / DENY / MODIFY), machine-readable `reasons` (all failing checks, not just the
first), human `explanation`, `rule_ids` (rule_key@version), `limits` (per limit: measured value, external floor,
internal floor, allowance, buffer fraction and amount, external/internal headroom, projected worst value),
`requested/approved/max_allowed_lots`, `new_order_risk`, `open_risk`, `freshness` (snapshot age, quote age,
rules verified at, reconciliation, day-start known, active kill switches), `ruleset_id`, `evaluated_at`.

## Checks for new risk (OPEN) — every one must pass

1. **System state**: no active kill switch · challenge active · snapshot present and ≤ `max_snapshot_age_s` ·
   reconciliation OK · equity = balance + floating (tolerance) · our floating PnL = platform floating PnL ·
   no unknown/manual positions · day-start reference known **and** for the current trading day.
2. **Rules**: daily_loss_limit + max_loss present · no pending critical change · every CRITICAL rule CONFIRMED
   (UNVERIFIED/UNCERTAIN/CONFLICT deny) · oldest critical verification ≤ `max_rules_age_h`.
3. **Market data**: instrument known · market open · quote present, sane (ask ≥ bid > 0), ≤ `max_quote_age_s`.
4. **Conduct**: automation allowed for the channel (`api_trading` or `ea_policy`; UNKNOWN/missing → deny,
   CONDITIONAL only with explicit acknowledgement) · instrument allowed · not within `rollover_guard_min` of the
   daily reset · weekend/overnight holding rules (UNKNOWN treated as prohibited) · news blackout (firm window +
   `news_extra_buffer_min`; UNKNOWN policy → default blackout; no calendar → deny).
5. **Numbers** (below).

## Numerical envelope

```
per-lot worst loss  = |entry_worst − stop_exit_worst| × contract × fx + round-turn commission
   entry_worst      = ask + slip (buy) / bid − slip (sell);  slip = max(spread × mult, mid × min_frac)
   stop_exit_worst  = stop ∓ (slip + spread × stop_gap_mult);   no stop → gap move (per asset class)
open risk           = Σ positions: current mark → stop (or gap) incl. exit commission
                    + Σ pending orders: as if filled, stop-to-stop
worst_now           = equity − open risk
internal floor      = external floor + buffer_frac × allowance
capacity(limit)     = worst_now − internal floor(limit)          for daily_loss and max_loss
max lots            = min( capacity/per-lot, risk-per-trade cap/per-lot, max_lot rule, leverage rule )
```

Floors: see docs/RULE_SCHEMA.md (static / trailing / EOD trailing / lock-at-initial; the four daily references).
Headroom is always measured on the **lower** of the rule's measured value and equity (a balance-based rule is
breached as soon as floating losses are realised).

**Buffer** (`compute_buffer_frac`): `base 0.30` + `0.20` if drawdown-rule confidence < 0.95 + `0.10` if quote
older than half the max age + `0.10 × (volatility_ratio − 1)` (cap 0.25); total capped at 0.90. The external
limit is never a target. Risk per trade default 1 % of initial; after the profit target is reached 0.1 %.

Invariant (property-tested with Hypothesis, 400 random states): for every ALLOW,
`equity − open_risk − new_order_risk ≥ internal_floor` for every limit and `approved ≤ max_allowed`.

## Risk reduction is always possible

REDUCE / CLOSE / CANCEL are allowed under kill switches, stale rules, stale data and pending rule changes, but
only if they truly reduce exposure (opposite side, size ≤ position, CLOSE = full size; cancel of an existing
order). MODIFY_SL is allowed only if the stop is tightened. Exception: if the firm bans *closing* inside a news
window, a close there is denied unless the deterministic supervisor marks it `emergency` (internal floor hit).

## Supervisor (required_actions)

Flatten + cancel pending `flatten_lead_min` before a prohibited weekend/overnight hold; flatten as emergency when
current value is at/below the internal floor (e.g. after a gap or when the buffer grows with volatility). It never
closes merely because rule *text* changed — a rule change blocks new risk and alerts; closing is chosen only by
the numeric/holding logic above.

## State (`risk/state.py`)

`RiskState` persists initial balance, trading date, day-start balance/equity (+ known flag), intraday and EOD
HWMs, min equity today, last snapshot ts/sequence, trading days, daily closed PnL, breaches. Snapshots with
non-increasing sequence or older timestamp are rejected. On rollover the reference is taken from the first
snapshot if within 120 s of the reset, otherwise reconstructed from platform deal history, otherwise marked
**unknown** (blocks new risk; owner can enter it from the firm's dashboard via
`AccountSession.set_day_start_manual`, audited).

## Kill switches

Kinds: `MANUAL` `STALE_MARKET_DATA` `CONTRADICTORY_BALANCES` `STALE_RULES` `RULE_VERIFICATION_FAILURE` `INTERNAL_DRAWDOWN_LIMIT` `UNKNOWN_POSITION` `UNEXPECTED_MANUAL_TRADE` `RECONNECT_UNCERTAIN_STATE` `RECONCILIATION_MISMATCH` `ACCOUNT_STATE_INCONSISTENT` `CALCULATION_MISMATCH` `AMBIGUOUS_EXECUTION_EVENT` `EXTERNAL_LIMIT_BREACHED`

Raised automatically by the session (reconciliation, assessment, reconnect, ambiguous execution, unconfirmed
critical rules) or manually. They block new risk only. Clearing is a manual owner action with a note (API/CLI),
audited; if the condition persists the next tick raises it again. No LLM path can reach it.

## Reason codes

`OK` `KILL_SWITCH_ACTIVE` `DUPLICATE_ORDER` `STATE_STALE` `STATE_MISSING` `OUT_OF_ORDER_STATE` `BALANCE_INCONSISTENT` `CALC_MISMATCH` `RECONCILIATION_NOT_OK` `UNKNOWN_POSITION` `DAY_STATE_UNCERTAIN` `ROLLOVER_GUARD` `NO_QUOTE` `STALE_MARKET_DATA` `INVALID_QUOTE` `MARKET_CLOSED` `UNKNOWN_INSTRUMENT` `RULES_STALE` `RULES_INCOMPLETE` `RULE_UNCERTAIN` `RULE_CONFLICT` `RULE_CHANGE_PENDING` `CHALLENGE_NOT_ACTIVE` `EXTERNAL_DAILY_LIMIT_BREACHED` `EXTERNAL_MAX_LOSS_BREACHED` `INTERNAL_DAILY_LIMIT` `INTERNAL_MAX_LOSS` `RISK_PER_TRADE` `STOP_LOSS_REQUIRED` `INVALID_STOP_LOSS` `MAX_LOT_SIZE` `MAX_OPEN_POSITIONS` `LEVERAGE` `SIZE_BELOW_MIN` `INVALID_SIZE` `INSTRUMENT_NOT_ALLOWED` `AUTOMATION_NOT_PERMITTED` `NEWS_BLACKOUT` `NEWS_CALENDAR_UNAVAILABLE` `WEEKEND_HOLDING` `OVERNIGHT_HOLDING` `TARGET_REACHED_RISK_CAP` `NOT_RISK_REDUCING` `POSITION_NOT_FOUND` `ORDER_NOT_FOUND`

## Tests

`pytest -m risk` = acceptance suite (required by the LIVE gate): unit tests of every check, DST/rollover (Prague,
New York, DST gaps, 23/25-hour days), trailing/EOD/lock modes, daily reference variants, equity vs balance,
open-risk & pending orders, consistency checks, property-based invariants, simulator integration (timeouts before/
after send, partial fills, rejects, duplicate events, gap through stop, manual trade, silent position loss,
corrupted equity, stale feed, restart mid-session, missed reset, reconnect, rule change pending, weekend flatten,
buffer growth emergency exit), bypass tests (forged/tampered/replayed/expired approvals, override prevention,
static AST scan).
