# ADR-0002: Deterministic, pure, fail-closed Risk Engine with internal buffers

Status: accepted

The Risk Engine is a pure function of `RiskContext` (time injected, Decimal math, no I/O). Any uncertainty —
stale/unconfirmed/conflicting rules, stale data, unknown day-start reference, reconciliation issues, kill
switches, unknown automation policy, missing news calendar — denies **new** risk. Risk-reducing actions stay
allowed. Internal floors sit `buffer_frac × allowance` above the firm's floors; the buffer grows with
uncertainty, stale data and volatility. Worst-case sizing assumes slippage on entry and on stop, stop gaps,
commissions and all open + pending exposure.

Consequences: more denials (especially with unverified seed data) — accepted, priority 1 is account survival.
