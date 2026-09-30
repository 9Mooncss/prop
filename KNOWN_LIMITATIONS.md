# Known limitations (honest status)

## Not implemented / not verified
* **No live trading adapter.** Only the simulated broker exists. LIVE is structurally impossible until an
  adapter declaring `is_live=True` is written, accepted and approved (ADR-0005). The live gate itself is
  tested; a live order path against a real platform is **unverified**.
* **Paper trading runs in the CLI process** (`propguard paper run`, accelerated simulated clock); there is no
  long-running paper/live session daemon inside the worker yet.
* **No news calendar provider.** With a firm news policy other than ALLOWED, the engine denies new risk
  (`NEWS_CALENDAR_UNAVAILABLE`).
* **No firm is VERIFIED.** Seed research used a summarizing fetch tool and some official pages returned 403;
  every rule starts UNVERIFIED and must be confirmed by the owner. Consequently real firms are filtered out
  by the recommender and trading on them is blocked until verification — by design.
* The LLM path was tested with a fake client; a real Anthropic call was **not executed** (no API key in this
  environment).
* Live monitoring run (2026-09-30, 23 seeded primary sources): 16 baselined, 9 research fragments found
  verbatim in raw pages (→ SOURCE_MATCHED), 7 returned HTTP 403 (FundedNext, FundingPips, E8 help, Breakout)
  and were recorded as BLOCKED, not bypassed. Several pages (e.g. some FTMO/HyroTrader FAQs) are
  JavaScript-rendered and yield only a few hundred characters over plain HTTP; their evidence stays
  UNVERIFIED (fail-safe). Headless-browser rendering for such pages is not implemented.
* The automated test suite uses mocked HTTP for monitoring.
* Intel Mac / Linux ARM64: image is multi-arch-compatible (pure Python, official multi-arch bases) but was only
  built and run on linux/amd64 here.
* OS keychain storage for secrets: env/`.env` only today; `keyring` extra reserved.

## Modelling limits
* Monte Carlo resamples historical days (i.i.d.); it ignores regime changes and autocorrelation. It is a
  proximity-to-limits indicator, **not** a pass probability.
* Replay without MAE cannot see intraday floating drawdown (reported in assumptions).
* Currency conversion uses the quote's `quote_to_account` factor supplied by the market-data provider; wrong
  factors are caught only by the calculation-mismatch check when the platform reports floating PnL.
* Consistency, scaling, payout-eligibility rules are stored and scored but only consistency is enforced in replay,
  not pre-trade.
* HFT/latency-arbitrage rules are stored; pre-trade enforcement of order-rate limits is not implemented
  (min-hold is checked in replay only).
* Python cannot make in-process guard bypass physically impossible; see ADR-0003 for the layered controls.
