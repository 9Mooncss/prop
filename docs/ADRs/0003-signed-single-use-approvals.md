# ADR-0003: PreTradeGuard mints HMAC-signed, single-use, short-lived approvals

Status: accepted

Python cannot make in-process bypass physically impossible, so layers are combined: (1) adapters are private
to `ExecutionEngine`; strategies only emit Signals; (2) `BrokerAdapter.submit` is the only public order path,
defined once and protected against override in `__init_subclass__`; (3) it only accepts an `ApprovedOrder`
whose HMAC (per-process random key) covers every order field, decision id, adapter name and expiry;
(4) approvals are single-use (replay → BypassAttempt) and expire after 5 s (stale state); (5) a static AST test
fails the build if protected hooks or the key are referenced elsewhere. Timeouts are resolved by client-id
lookup; re-sending requires a fresh evaluation and a new approval.
